"""End-to-end test of the VictoriaMetrics backend against a RUNNING Home Assistant.

Drives the config flow with the VictoriaMetrics backend through the same REST
calls the UI makes and reads both endpoints. Needs VictoriaMetrics with data
written by tests/compat/run_vm.py. Standard library only.

    E2E_TEST_INSTANCE=1 HA_API=http://homeassistant.local:8123/api HA_TOKEN=... \\
    VM_URL=http://192.168.1.10:8428 python tests/e2e/flow_vm.py

VM_URL is the address as Home Assistant sees it.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

if os.environ.get("E2E_TEST_INSTANCE") != "1":
    sys.exit("Refusing to run: this test deletes InfluxDB Proxy entries. Run it against a TEST "
             "instance only, with E2E_TEST_INSTANCE=1.")

API = os.environ["HA_API"].rstrip("/")
TOKEN = os.environ.get("HA_TOKEN") or open(os.environ["HA_TOKEN_FILE"]).read().strip()
VM_URL = os.environ["VM_URL"]
DOMAIN = "influx_proxy"

results: list[tuple[str, bool, str]] = []


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
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


def remove_all() -> None:
    _, data = call("GET", "/config/config_entries/entry?domain=" + DOMAIN)
    for e in data if isinstance(data, list) else []:
        call("DELETE", f"/config/config_entries/entry/{e['entry_id']}")


def main() -> int:
    remove_all()
    form = {"backend": "victoriametrics", "url": VM_URL, "auth": "none", "verify_ssl": True}

    _, flow = call("POST", "/config/config_entries/flow", {"handler": DOMAIN})
    _, step = call("POST", f"/config/config_entries/flow/{flow.get('flow_id')}", {**form, "database": "no_such_db"})
    check("unknown database -> errors.base = database_not_found",
          (step.get("errors") or {}).get("base") == "database_not_found", str(step.get("errors")))
    _, step = call("POST", f"/config/config_entries/flow/{flow.get('flow_id')}", {**form, "database": "homeassistant"})
    check("entry created", step.get("type") == "create_entry", str(step.get("type")))
    entry_id = (step.get("result") or {}).get("entry_id")

    # entities without a unit are in the measurement "state" (as written by run_vm.py)
    _, opt = call("POST", "/config/config_entries/options/flow", {"handler": entry_id})
    call("POST", f"/config/config_entries/options/flow/{opt.get('flow_id')}",
         {"measurement_mode": "unit_of_measurement", "default_measurement": "state",
          "override_measurement": "", "max_entities": 12, "max_days": 800, "max_state_rows": 250000})

    # the proxy names the measurement from the entity's unit: make sure Home Assistant knows it
    call("POST", "/states/sensor.compat_room", {"state": "22", "attributes": {"unit_of_measurement": "°C"}})
    for _ in range(30):
        status, data = call("GET", "/influx_proxy/series?entities=sensor.compat_room&days=1")
        if status == 200 and data.get("sensor.compat_room"):
            break
        time.sleep(0.5)
    pts = data.get("sensor.compat_room") or []
    check("series: found, all values (min 20, max 24)",
          bool(pts) and min(p["min"] for p in pts) == 20 and max(p["max"] for p in pts) == 24, f"HTTP {status} {str(data)[:120]}")
    check("series: point shape start/end/mean/min/max",
          bool(pts) and set(pts[0]) == {"start", "end", "mean", "min", "max"}, str(pts[:1]))

    status, data = call("GET", "/influx_proxy/states?entities=cover.compat_blind,light.compat_quiet,"
                        "media_player.compat_tv&days=1&attributes=current_position")
    check("states: row before + change inside, text from value, attribute",
          status == 200 and [r[1:] for r in data.get("cover.compat_blind", [])] == [["closed", 0], ["open", 100]],
          f"HTTP {status} {str(data)[:120]}")
    check("states: row before found beyond the first lookback",
          [r[1] for r in data.get("light.compat_quiet", [])] == ["on"], str(data.get("light.compat_quiet")))
    # run_vm.py writes the TV's "playing" the way "Store text states" does: read back as text
    check("states: text state from a written code", [r[1] for r in data.get("media_player.compat_tv", [])] == ["playing"],
          str(data.get("media_player.compat_tv")))

    # -- "Store text states": a state set through the API is written as a code and read back as text
    _, opt = call("POST", "/config/config_entries/options/flow", {"handler": entry_id})
    call("POST", f"/config/config_entries/options/flow/{opt.get('flow_id')}",
         {"measurement_mode": "unit_of_measurement", "default_measurement": "state",
          "override_measurement": "", "max_entities": 12, "max_days": 800, "max_state_rows": 250000,
          "write_states": True})
    time.sleep(3)  # reload, dictionary loaded
    tag = str(int(time.time()))
    for state in (f"eco 40 {tag}", f'pranie "extra" {tag}'):
        call("POST", "/states/sensor.e2e_program", {"state": state})
        time.sleep(1)
    call("POST", "/states/sensor.e2e_freetext", {"state": "x" * 150})
    for _ in range(30):
        status, data = call("GET", "/influx_proxy/states?entities=sensor.e2e_program,sensor.e2e_freetext&days=1")
        rows = data.get("sensor.e2e_program", []) if status == 200 else []
        if rows and rows[-1][1] == f'pranie "extra" {tag}':  # this run's rows, not a previous run's
            break
        time.sleep(1)
    check("text states: written and read back as text, quotes kept",
          [r[1] for r in data.get("sensor.e2e_program", [])][-2:] == [f"eco 40 {tag}", f'pranie "extra" {tag}'],
          f"HTTP {status} {str(data)[:160]}")
    check("text states: free text (150 characters) not written", "sensor.e2e_freetext" not in data, str(list(data)))

    remove_all()
    status, _ = call("GET", "/influx_proxy/series?entities=sensor.compat_room&days=1")
    check("after removing the entry -> 503", status == 503, f"HTTP {status}")

    width = max(len(name) for name, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {'' if ok else detail}")
    passed = sum(ok for _, ok, _ in results)
    print(f"{'OK' if passed == len(results) else 'FAILED'}  {passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
