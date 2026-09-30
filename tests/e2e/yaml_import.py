"""End-to-end test of the legacy YAML import (`influx_proxy:` in configuration.yaml).

Needs file access to the Home Assistant config directory and the right to
restart Home Assistant, so it is meant to run inside an add-on with
Home Assistant API access (Supervisor token), on a test instance.

    HA_API=http://supervisor/core/api SUPERVISOR=http://supervisor \\
    HA_TOKEN_FILE=/run/s6/container_environment/SUPERVISOR_TOKEN \\
    HA_CONFIG=/homeassistant INFLUX_URL=... INFLUX_USER=... INFLUX_PASS=... \\
    python tests/e2e/yaml_import.py

It appends an `influx_proxy:` section, restarts, checks the imported entry,
then restores configuration.yaml, removes the entry and restarts again.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if os.environ.get("E2E_TEST_INSTANCE") != "1":
    sys.exit("Refusing to run: this test deletes InfluxDB Proxy entries (and yaml_import.py "
             "edits configuration.yaml and restarts). Run it against a TEST instance only, "
             "with E2E_TEST_INSTANCE=1.")

API = os.environ["HA_API"].rstrip("/")
SUP = os.environ["SUPERVISOR"].rstrip("/")
TOKEN = os.environ.get("HA_TOKEN") or open(os.environ["HA_TOKEN_FILE"]).read().strip()
CONF = os.path.join(os.environ["HA_CONFIG"], "configuration.yaml")
BACKUP = CONF + ".before-influx-proxy-e2e"
YAML = f"""
# --- influx_proxy e2e test (temporary) ---
influx_proxy:
  url: {os.environ["INFLUX_URL"]}
  database: homeassistant
  username: {os.environ["INFLUX_USER"]}
  password: {os.environ["INFLUX_PASS"]}
  max_entities: 5
"""
results: list[tuple[str, bool, str]] = []


def call(method, url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            t = r.read().decode()
            return r.status, (json.loads(t) if t else {})
    except urllib.error.HTTPError as e:
        return e.code, {}
    except OSError:
        return 0, {}


def restart_and_wait():
    call("POST", SUP + "/core/restart")
    time.sleep(10)
    deadline = time.time() + 300
    while time.time() < deadline:
        status, data = call("GET", API + "/")
        if status == 200 and data.get("message") == "API running.":
            status, cfg = call("GET", API + "/config")
            if status == 200 and cfg.get("state") == "RUNNING":
                return True
        time.sleep(5)
    return False


def entries():
    _, data = call("GET", API + "/config/config_entries/entry?domain=influx_proxy")
    return data if isinstance(data, list) else []


def _ws_command(command: dict) -> dict:
    """One WebSocket command against the running instance (stdlib only).

    The registry FILE cannot answer "is the notice shown": non-persistent
    issues are stored without an `active` flag and come back inactive after
    a restart. Only the running instance knows, so ask it.
    """
    url = urllib.parse.urlsplit(API.replace("/api", "/websocket", 1))
    sock = socket.create_connection((url.hostname, url.port or 80), timeout=30)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f"GET {url.path} HTTP/1.1\r\nHost: {url.hostname}\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)
    buf = buf.split(b"\r\n\r\n", 1)[1]

    def recv():
        nonlocal buf
        def need(n):
            nonlocal buf
            while len(buf) < n:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("closed")
                buf += chunk
            out, buf = buf[:n], buf[n:]
            return out
        while True:
            b0, b1 = need(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", need(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", need(8))[0]
            payload = need(n)
            if b0 & 0x0F in (1, 2):
                return json.loads(payload)

    def send(obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        head = struct.pack("!B", 0x81) + (struct.pack("!B", 0x80 | len(data)) if len(data) < 126
                                          else struct.pack("!BH", 0x80 | 126, len(data)))
        sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    recv()                                           # auth_required
    send({"type": "auth", "access_token": TOKEN})
    if recv().get("type") != "auth_ok":
        raise RuntimeError("websocket auth failed")
    send({"id": 1, **command})
    while True:
        msg = recv()
        if msg.get("id") == 1:
            sock.close()
            return msg


def _issue_present() -> bool:
    """Is the 'remove YAML' notice shown in the repairs panel right now?"""
    msg = _ws_command({"type": "repairs/list_issues"})
    return any(i.get("issue_id") == "deprecated_yaml_influx_proxy"
               for i in (msg.get("result") or {}).get("issues", []))


def main():
    for e in entries():
        call("DELETE", API + f"/config/config_entries/entry/{e['entry_id']}")
    shutil.copy2(CONF, BACKUP)
    try:
        with open(CONF, "a") as f:
            f.write(YAML)
        results.append(("restart with YAML", restart_and_wait(), ""))
        time.sleep(5)  # the import runs as a background task after setup
        found = entries()
        e = found[0] if found else {}
        results.append(("exactly one entry after import", len(found) == 1, str(len(found))))
        results.append(("entry source is 'import'", e.get("source") == "import", str(e.get("source"))))
        status, data = call("GET", API + "/influx_proxy/series?entities=sensor.compat_no_unit&days=1")
        results.append(("YAML behaviour kept: default measurement 'state'",
                        status == 200 and bool(data.get("sensor.compat_no_unit")), f"HTTP {status}"))
        status, _ = call("GET", API + "/influx_proxy/series?entities=" + ",".join(f"sensor.e{i}" for i in range(6)) + "&days=1")
        results.append(("max_entities from YAML (5) enforced", status == 400, f"HTTP {status}"))
        # a second restart with YAML still present must not create a duplicate
        results.append(("second restart with YAML", restart_and_wait(), ""))
        time.sleep(5)
        results.append(("no duplicate entry on next restart", len(entries()) == 1, str(len(entries()))))
        deadline = time.time() + 120
        while not _issue_present() and time.time() < deadline:
            time.sleep(5)
        results.append(("repair issue 'remove YAML' registered", _issue_present(), ""))
        # user removes the YAML and restarts: the entry stays, the notice must go
        shutil.move(BACKUP, CONF)
        results.append(("restart after removing YAML", restart_and_wait(), ""))
        time.sleep(15)  # issue registry is saved with a delay
        results.append(("entry survives removing YAML", len(entries()) == 1, str(len(entries()))))
        status, data = call("GET", API + "/influx_proxy/series?entities=sensor.compat_no_unit&days=1")
        results.append(("endpoint still works from the imported entry", status == 200 and bool(data), f"HTTP {status}"))
        # the issue registry is written with a delay - poll the file
        deadline = time.time() + 120
        while _issue_present() and time.time() < deadline:
            time.sleep(5)
        results.append(("repair issue withdrawn after removing YAML", not _issue_present(), ""))
    finally:
        if os.path.exists(BACKUP):
            shutil.move(BACKUP, CONF)
        for e in entries():
            call("DELETE", API + f"/config/config_entries/entry/{e['entry_id']}")
        cleaned = restart_and_wait()
        results.append(("cleanup: YAML restored, entry removed, restarted", cleaned and not entries(), ""))

    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {'' if ok else detail}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"{'OK' if not failed else 'FAILED'}  {len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
