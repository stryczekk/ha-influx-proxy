"""End-to-end test against a RUNNING Home Assistant.

Drives the config flow through the same REST calls the UI makes, then
exercises the options flow and the endpoint, and cleans up after itself.
Needs InfluxDB with data written by tests/compat/run.py and a read-only
account. Standard library only.

    E2E_TEST_INSTANCE=1 HA_API=http://homeassistant.local:8123/api HA_TOKEN=... \\
    INFLUX_URL=http://192.168.1.10:18086 INFLUX_USER=reader INFLUX_PASS=... \\
    python tests/e2e/flow.py

HA_TOKEN_FILE may point to a file instead of HA_TOKEN (e.g. the Supervisor
token of an add-on with Home Assistant API access, with
HA_API=http://supervisor/core/api).

Note: the "no session -> 401" check is a failed login from Home
Assistant's point of view and is logged by `http.ban`. With
`ip_ban_enabled` and a low `login_attempts_threshold`, running this test
repeatedly can ban the machine it runs from - use a test instance.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

if os.environ.get("E2E_TEST_INSTANCE") != "1":
    sys.exit("Refusing to run: this test deletes InfluxDB Proxy entries (and yaml_import.py "
             "edits configuration.yaml and restarts). Run it against a TEST instance only, "
             "with E2E_TEST_INSTANCE=1.")

API = os.environ["HA_API"].rstrip("/")
# for the no-session check: must hit Home Assistant itself, not a proxy in front
DIRECT = os.environ.get("HA_API_DIRECT", API).rstrip("/")
TOKEN = os.environ.get("HA_TOKEN") or open(os.environ["HA_TOKEN_FILE"]).read().strip()
INFLUX = {"url": os.environ["INFLUX_URL"], "user": os.environ["INFLUX_USER"], "pass": os.environ["INFLUX_PASS"]}
DOMAIN = "influx_proxy"

results: list[tuple[str, bool, str]] = []


def call(method: str, path: str, body: dict | None = None, auth: bool = True, base: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(
        (base or API) + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,  # explicit: urllib turns a body-less request into GET
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode()
            return r.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, {"raw": text[:200]}


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def entries() -> list[dict]:
    _, data = call("GET", "/config/config_entries/entry?domain=" + DOMAIN)
    return data if isinstance(data, list) else []


def remove_all() -> None:
    for e in entries():
        call("DELETE", f"/config/config_entries/entry/{e['entry_id']}")


def series(query: str, auth: bool = True) -> tuple[int, dict]:
    return call("GET", f"/influx_proxy/series?{query}", auth=auth, base=None if auth else DIRECT)


def main() -> int:
    remove_all()

    # -- endpoint without configuration
    # Before the first setup Home Assistant has not loaded the integration at
    # all (no YAML, no entry), so the view does not exist yet: 404. After it
    # was loaded once, the view stays and answers 503 while unconfigured.
    status, _ = series("entities=sensor.compat_room&days=1")
    check("endpoint 404 or 503 while not configured", status in (404, 503), f"HTTP {status}")

    # -- config flow: step 1
    status, flow = call("POST", "/config/config_entries/flow", {"handler": DOMAIN})
    fields = [f["name"] for f in flow.get("data_schema", [])]
    check("flow starts with the 'user' form", flow.get("step_id") == "user", str(flow.get("step_id")))
    check("user form fields", fields == ["url", "database", "auth", "verify_ssl"], str(fields))
    fid = flow.get("flow_id")

    _, flow = call("POST", f"/config/config_entries/flow/{fid}",
                   {"url": INFLUX["url"], "database": "homeassistant", "auth": "basic", "verify_ssl": True})
    fields = [f["name"] for f in flow.get("data_schema", [])]
    check("basic auth leads to 'credentials' form", flow.get("step_id") == "credentials", str(flow.get("step_id")))
    check("credentials form fields", fields == ["username", "password"], str(fields))

    # -- credentials in the URL and a URL without scheme are refused on the first form
    _, f2 = call("POST", "/config/config_entries/flow", {"handler": DOMAIN})
    _, f2 = call("POST", f"/config/config_entries/flow/{f2['flow_id']}",
                 {"url": INFLUX["url"].replace("://", "://reader:secret@"), "database": "homeassistant",
                  "auth": "basic", "verify_ssl": True})
    check("credentials in URL -> errors.url = url_credentials",
          (f2.get("errors") or {}).get("url") == "url_credentials", str(f2.get("errors")))
    _, f2 = call("POST", f"/config/config_entries/flow/{f2['flow_id']}",
                 {"url": "192.168.0.63:18086", "database": "homeassistant", "auth": "basic", "verify_ssl": True})
    check("URL without scheme -> errors.url = invalid_url",
          (f2.get("errors") or {}).get("url") == "invalid_url", str(f2.get("errors")))
    call("DELETE", f"/config/config_entries/flow/{f2['flow_id']}")

    # -- wrong password must be rejected with a translated error key
    _, flow = call("POST", f"/config/config_entries/flow/{fid}", {"username": INFLUX["user"], "password": "wrong"})
    check("wrong password -> errors.base = invalid_auth",
          (flow.get("errors") or {}).get("base") == "invalid_auth", str(flow.get("errors")))

    # -- right password creates the entry
    _, flow = call("POST", f"/config/config_entries/flow/{fid}", {"username": INFLUX["user"], "password": INFLUX["pass"]})
    check("right password creates the entry", flow.get("type") == "create_entry", str(flow.get("type")))
    entry_id = (flow.get("result") or {}).get("entry_id")

    # -- only one instance
    _, again = call("POST", "/config/config_entries/flow", {"handler": DOMAIN})
    check("second instance is refused", again.get("type") == "abort", f"{again.get('type')} {again.get('reason')}")

    # -- unreachable database
    remove_all()
    _, flow = call("POST", "/config/config_entries/flow", {"handler": DOMAIN})
    _, flow = call("POST", f"/config/config_entries/flow/{flow['flow_id']}",
                   {"url": "http://127.0.0.1:9", "database": "homeassistant", "auth": "none", "verify_ssl": True})
    check("unreachable URL -> errors.base = cannot_connect",
          (flow.get("errors") or {}).get("base") == "cannot_connect", str(flow.get("errors")))
    call("DELETE", f"/config/config_entries/flow/{flow.get('flow_id')}")

    # recreate the good entry for the rest of the test
    _, flow = call("POST", "/config/config_entries/flow", {"handler": DOMAIN})
    fid = flow["flow_id"]
    call("POST", f"/config/config_entries/flow/{fid}",
         {"url": INFLUX["url"], "database": "homeassistant", "auth": "basic", "verify_ssl": True})
    _, flow = call("POST", f"/config/config_entries/flow/{fid}", {"username": INFLUX["user"], "password": INFLUX["pass"]})
    entry_id = (flow.get("result") or {}).get("entry_id")

    # -- endpoint with Home Assistant defaults: entity not in the state machine
    #    -> measurement = entity_id -> nothing found, but a clean 200
    status, data = series("entities=sensor.compat_room&days=1")
    check("endpoint 200 with defaults", status == 200, f"HTTP {status} {str(data)[:80]}")

    # -- options flow: default measurement 'state'
    _, opt = call("POST", "/config/config_entries/options/flow", {"handler": entry_id})
    ofields = [f["name"] for f in opt.get("data_schema", [])]
    check("options form fields", ofields == ["measurement_mode", "default_measurement",
          "override_measurement", "max_entities", "max_days"], str(ofields))
    _, opt = call("POST", f"/config/config_entries/options/flow/{opt['flow_id']}",
                  {"measurement_mode": "unit_of_measurement", "default_measurement": "state",
                   "override_measurement": "", "max_entities": 3, "max_days": 30})
    check("options saved", opt.get("type") == "create_entry", str(opt.get("type")))

    # the options listener reloads the entry in the background - wait for it
    for _ in range(30):
        status, data = series("entities=sensor.compat_no_unit&days=1")
        if status == 200 and data:
            break
        time.sleep(0.5)
    check("default measurement 'state' applied after reload",
          status == 200 and bool(data.get("sensor.compat_no_unit")), f"HTTP {status} {str(data)[:80]}")

    # -- override measurement: finds the °C series, number.* excluded
    _, opt = call("POST", "/config/config_entries/options/flow", {"handler": entry_id})
    call("POST", f"/config/config_entries/options/flow/{opt['flow_id']}",
         {"measurement_mode": "unit_of_measurement", "default_measurement": "state",
          "override_measurement": "°C", "max_entities": 3, "max_days": 30})
    for _ in range(30):
        status, data = series("entities=sensor.compat_room&days=1")
        if status == 200 and data.get("sensor.compat_room"):
            break
        time.sleep(0.5)
    pts = data.get("sensor.compat_room") or []
    check("override '°C' returns the sensor series", status == 200 and bool(pts), f"HTTP {status}")
    check("number.* with the same object id not mixed in", bool(pts) and max(p["max"] for p in pts) < 99,
          str([p["max"] for p in pts]))
    check("point shape start/end/mean/min/max",
          bool(pts) and set(pts[0]) == {"start", "end", "mean", "min", "max"}, str(pts[:1]))

    status, data = series("entities=sensor.compat_room,sensor.compat_room,sensor.compat_room,sensor.compat_room&days=1")
    check("duplicated entities are merged (4 x same id is not over max_entities=3)",
          status == 200 and list(data) == ["sensor.compat_room"], f"HTTP {status} {list(data)}")

    # -- request validation and limits (max_entities=3, max_days=30 from options)
    for name, query, want in [
        ("missing entities -> 400", "days=1", 400),
        ("invalid entity_id -> 400", "entities=sensor.x%27y&days=1", 400),
        ("over max_entities -> 400", "entities=sensor.a,sensor.b,sensor.c,sensor.d&days=1", 400),
        ("over max_days -> 400", "entities=sensor.a&days=31", 400),
        ("days not a number -> 400", "entities=sensor.a&days=abc", 400),
    ]:
        status, data = series(query)
        check(name, status == want, f"HTTP {status} {data.get('message', '')}")
    status, _ = series("entities=sensor.compat_room&days=1", auth=False)
    check("no Home Assistant session -> 401", status == 401, f"HTTP {status}")

    # -- unload: endpoint goes back to 503
    remove_all()
    status, _ = series("entities=sensor.compat_room&days=1")
    check("after removing the entry -> 503", status == 503, f"HTTP {status}")

    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {'' if ok else detail}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"{'OK' if not failed else 'FAILED'}  {len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
