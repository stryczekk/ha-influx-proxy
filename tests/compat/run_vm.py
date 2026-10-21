"""Compatibility test against a real VictoriaMetrics.

Writes points through VictoriaMetrics' InfluxDB line protocol endpoint,
shaped exactly like Home Assistant's `influxdb` integration writes them, then
reads them back with the SAME query code the integration uses (vm.py).
Standard library only.

    VM_URL=http://victoriametrics:8428 python tests/compat/run_vm.py

Exit code 0 = every check passed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import time
import types
import urllib.error
import urllib.parse
import urllib.request

_DIR = pathlib.Path(__file__).parents[2] / "custom_components" / "influx_proxy"
_pkg = types.ModuleType("influx_proxy_compat")
_pkg.__path__ = [str(_DIR)]
sys.modules[_pkg.__name__] = _pkg
for _name in ("query", "vm"):
    _spec = importlib.util.spec_from_file_location(f"{_pkg.__name__}.{_name}", _DIR / f"{_name}.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _mod
    _spec.loader.exec_module(_mod)
q = sys.modules[f"{_pkg.__name__}.query"]
vm = sys.modules[f"{_pkg.__name__}.vm"]

URL = os.environ["VM_URL"].rstrip("/")
DB = os.environ.get("VM_DB", "homeassistant")
WAIT = int(os.environ.get("VM_WAIT", "90"))


def get(path: str, params) -> bytes:
    with urllib.request.urlopen(f"{URL}{path}?{urllib.parse.urlencode(params)}", timeout=30) as r:
        return r.read()


def wait_until_up() -> None:
    deadline = time.time() + WAIT
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(f"{URL}/health", timeout=5).status == 200:
                return
        except OSError:
            pass
        time.sleep(2)
    raise SystemExit(f"FAIL {URL} did not come up within {WAIT} s")


def main() -> None:
    wait_until_up()
    now_ns = time.time_ns()
    minute, hour = 60 * 10**9, 3600 * 10**9
    # same shapes as tests/compat/run.py; string fields are what HA writes too
    lines = [f"°C,domain=sensor,entity_id=compat_room value={20 + i}.0 {now_ns - (5 - i) * minute}" for i in range(5)]
    lines += [f"°C,domain=number,entity_id=compat_room value=99 {now_ns - minute}"]
    lines += [
        f'state,domain=cover,entity_id=compat_blind state="closed",value=0,current_position=0 {now_ns - 3 * hour}',
        f'state,domain=cover,entity_id=compat_blind state="open",value=1,current_position=100 {now_ns - 10 * minute}',
        f'state,domain=switch,entity_id=compat_blind state="on",value=1 {now_ns - 5 * minute}',
        f'state,domain=light,entity_id=compat_quiet state="on",value=1 {now_ns - 72 * hour}',
        f'state,domain=media_player,entity_id=compat_tv state="playing" {now_ns - 5 * minute}',
        f'state,domain=media_player,entity_id=compat_old state="off",value=0 {now_ns - 96 * hour}',
    ]
    req = urllib.request.Request(f"{URL}/write?db={DB}", data="\n".join(lines).encode(), method="POST")
    urllib.request.urlopen(req, timeout=30).read()
    # what the proxy writes with "Store text states": the dictionary and the codes, the TV point at the same ms
    now_ms = now_ns // 10**6
    tv_ms = (now_ns - 5 * minute) // 10**6
    written = [
        vm.label_line("playing", 1, tv_ms, DB), vm.label_line('pranie "eco"', 2, tv_ms, DB),
        vm.code_line("media_player.compat_tv", 1, tv_ms, DB),
        vm.code_line("sensor.compat_program", 2, now_ms - 2 * 60_000, DB),
        # off (a number) 4 days ago, then playing (a code only) 3 days ago: the row before is the code
        vm.code_line("media_player.compat_old", 1, (now_ns - 72 * hour) // 10**6, DB),
    ]
    req = urllib.request.Request(f"{URL}/api/v1/import/prometheus", data=("\n".join(written) + "\n").encode(), method="POST")
    urllib.request.urlopen(req, timeout=30).read()
    time.sleep(2)
    try:  # make the new samples visible to queries at once (the default is up to a few seconds)
        urllib.request.urlopen(f"{URL}/internal/force_flush", timeout=10).read()
    except urllib.error.URLError:
        pass

    checks = []

    p = vm.series_params("sensor.compat_room", "°C", 1, None, now_ms + 60_000, DB)
    points = vm.parse_series(json.loads(get("/api/v1/query_range", p)), p["step"])
    checks.append(("series found", bool(points)))
    checks.append(("number.* with same object id excluded (max < 99)", bool(points) and max(x["max"] for x in points) < 99))
    checks.append(("all written values present (min 20, max 24)",
                   bool(points) and min(x["min"] for x in points) == 20 and max(x["max"] for x in points) == 24))
    p = vm.series_params("sensor.compat_room", "°C", 1, None, now_ms + 60_000, "other_db")
    checks.append(("db label filters", not vm.parse_series(json.loads(get("/api/v1/query_range", p)), p["step"])))
    hostile = 'x"} or vector(1) #'
    p = vm.series_params("sensor.compat_room", hostile, 1, None, now_ms + 60_000, DB)
    checks.append(("injection attempt is a harmless empty query",
                   not vm.parse_series(json.loads(get("/api/v1/query_range", p)), p["step"])))

    def states(entity_id, measurement, start, end, attrs=(), limit=250000, labels=None):
        lookback = start - q.BEFORE_LOOKBACK_HOURS * 3600_000
        rows = vm.parse_export(get("/api/v1/export", vm.export_params(entity_id, measurement, attrs, lookback, end, DB)),
                               entity_id, measurement, attrs, labels)
        if not any(r[0] <= start for r in rows):
            t = vm.parse_last_before(json.loads(get("/api/v1/query", vm.last_before_params(entity_id, measurement, start, DB))))
            if t is not None:
                older = vm.parse_export(get("/api/v1/export", vm.export_params(entity_id, measurement, attrs, t, t, DB)),
                                        entity_id, measurement, attrs, labels)
                rows = older[-1:] + rows
        return vm.states_rows(rows, rows, start, limit)

    rows = states("cover.compat_blind", "state", now_ms - 3600_000, now_ms + 1000, ("current_position",))
    checks.append(("states: row before range + change inside, text from value",
                   [r[1:] for r in rows] == [["closed", 0], ["open", 100]]))
    checks.append(("states: switch.* with same object id excluded", all(r[1] != "on" for r in rows)))
    rows = states("light.compat_quiet", "state", now_ms - 3600_000, now_ms + 1000)
    checks.append(("states: row before found beyond the lookback", [r[1] for r in rows] == ["on"]))
    rows = states("sensor.compat_room", "°C", now_ms - 3600_000, now_ms + 1000)
    checks.append(("states: numeric state", [r[1] for r in rows] == [20, 21, 22, 23, 24]))
    rows = states("media_player.compat_tv", "state", now_ms - 3600_000, now_ms + 1000)
    checks.append(("states: a text-only state is not stored by the influxdb integration", rows == []))
    dictionary = vm.parse_dictionary(json.loads(get("/api/v1/series", vm.dictionary_params(DB))))
    checks.append(("text states: dictionary read back", dictionary.codes == {"playing": 1, 'pranie "eco"': 2}))
    labels = dictionary.labels
    rows = states("media_player.compat_tv", "state", now_ms - 3600_000, now_ms + 1000, labels=labels)
    checks.append(("text states: written code read back as text", [r[1] for r in rows] == ["playing"]))
    rows = states("media_player.compat_old", "state", now_ms - 3600_000, now_ms + 1000, labels=labels)
    checks.append(("text states: the row before is a code from days ago, not the older number",
                   [r[1] for r in rows] == ["playing"]))
    rows = states("sensor.compat_program", "state", now_ms - 3600_000, now_ms + 1000, labels=labels)
    checks.append(("text states: quotes in a label survive", [r[1] for r in rows] == ['pranie "eco"']))
    rows = states("cover.compat_blind", "state", now_ms - 2 * 3600_000, now_ms - 3600_000, ("current_position",))
    checks.append(("states: fixed window in the past - only the row before", [r[1:] for r in rows] == [["closed", 0]]))
    payload = json.loads(get("/api/v1/label/db/values", {}))
    checks.append(("config-flow connection test: db listed", DB in payload.get("data", [])))

    width = max(len(name) for name, _ in checks)
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}")
    if not all(ok for _, ok in checks):
        raise SystemExit(1)
    print(f"OK  {URL}  all {len(checks)} checks passed")


if __name__ == "__main__":
    main()
