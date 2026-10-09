"""Compatibility test against a real InfluxDB - 1.8, 2.x or 3.x.

Writes a few points shaped exactly like Home Assistant's `influxdb`
integration writes them, then reads them back with the SAME query code the
integration uses (query.py). Standard library only, so it runs in a plain
python image next to the database service.

    INFLUX_URL=http://influx18:8086 INFLUX_DB=homeassistant INFLUX_CREATE_DB=1 python tests/compat/run.py
    INFLUX_URL=http://influx2:8086  INFLUX_DB=homeassistant INFLUX_TOKEN=... python tests/compat/run.py
    INFLUX_URL=http://influx3:8181  INFLUX_DB=homeassistant python tests/compat/run.py   # --without-auth

Exit code 0 = every check passed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_PATH = pathlib.Path(__file__).parents[2] / "custom_components" / "influx_proxy" / "query.py"
_spec = importlib.util.spec_from_file_location("influx_proxy_query", _PATH)
q = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = q
_spec.loader.exec_module(q)

URL = os.environ["INFLUX_URL"].rstrip("/")
DB = os.environ.get("INFLUX_DB", "homeassistant")
TOKEN = os.environ.get("INFLUX_TOKEN", "")
WAIT = int(os.environ.get("INFLUX_WAIT", "90"))


def request(path: str, params: dict, body: bytes | None = None) -> tuple[int, str]:
    headers = {"Authorization": f"Token {TOKEN}"} if TOKEN else {}
    req = urllib.request.Request(
        f"{URL}{path}?{urllib.parse.urlencode(params)}",
        data=body,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def influxql(query: str) -> dict:
    status, text = request("/query", {"db": DB, "epoch": "ms", "q": query})
    if status != 200:
        raise SystemExit(f"FAIL /query HTTP {status}: {text[:300]}")
    payload = json.loads(text)
    error = q.first_error(payload)
    if error:
        raise SystemExit(f"FAIL InfluxQL error: {error}")
    return payload


def wait_until_up() -> None:
    deadline = time.time() + WAIT
    while time.time() < deadline:
        try:
            status, _ = request("/ping", {})
            if status in (200, 204):
                return
        except OSError:
            pass
        time.sleep(2)
    raise SystemExit(f"FAIL {URL} did not come up within {WAIT} s")


def main() -> None:
    wait_until_up()
    if os.environ.get("INFLUX_CREATE_DB"):  # 1.x: the database must exist before writes
        influxql(f"CREATE DATABASE {q.quote_identifier(DB)}")

    now_ns = time.time_ns()
    step = 60 * 10**9
    # Same shape as Home Assistant: measurement = unit, tags domain + entity_id
    # (object id only), field "value". A number entity with the same object id
    # in the same measurement must NOT leak into the sensor series.
    lines = [f"°C,domain=sensor,entity_id=compat_room value={20 + i}.0 {now_ns - (5 - i) * step}" for i in range(5)]
    lines += [f"°C,domain=number,entity_id=compat_room value=99 {now_ns - step}"]
    lines += [f"state,domain=sensor,entity_id=compat_no_unit value=1 {now_ns - step}"]
    # A cover as HA writes it: string state, numeric value, attributes as
    # fields. One change before the 1 h range, one inside it.
    hour = 3600 * 10**9
    lines += [
        f'state,domain=cover,entity_id=compat_blind state="closed",value=0,current_position=0 {now_ns - 3 * hour}',
        f'state,domain=cover,entity_id=compat_blind state="open",value=1,current_position=100 {now_ns - 10 * step}',
        f'state,domain=switch,entity_id=compat_blind state="on",value=1 {now_ns - 5 * step}',
    ]
    status, text = request("/write", {"db": DB, "precision": "ns"}, "\n".join(lines).encode())
    if status not in (200, 204):
        raise SystemExit(f"FAIL /write HTTP {status}: {text[:300]}")
    time.sleep(2)  # 3.x flushes writes asynchronously

    checks = []

    payload = influxql(q.series_query("sensor.compat_room", "°C", 1))
    points = q.parse_results(payload, ["sensor.compat_room"]).get("sensor.compat_room", [])
    lo = min((p["min"] for p in points), default=None)
    hi = max((p["max"] for p in points), default=None)
    checks.append(("sensor series found", bool(points)))
    checks.append(("number.* with same object id excluded (max < 99)", hi is not None and hi < 99))
    # points are 1 min apart and grouped by 5 min, so compare the extremes,
    # not a sum of bucket means
    checks.append(("all written values present (min 20, max 24)", lo == 20 and hi == 24))

    naming = q.MeasurementNaming(default_measurement="state")
    measurement = q.measurement_for("sensor.compat_no_unit", None, None, naming)
    payload = influxql(q.series_query("sensor.compat_no_unit", measurement, 1))
    checks.append(("default measurement 'state' for entity without unit",
                   bool(q.parse_results(payload, ["sensor.compat_no_unit"]))))

    hostile = 'x" ; DROP DATABASE ' + DB + ' ; "'
    influxql(q.series_query("sensor.compat_room", hostile, 1))
    payload = influxql(q.series_query("sensor.compat_room", "°C", 1))
    checks.append(("injection attempt is a harmless empty query", bool(q.parse_results(payload, ["sensor.compat_room"]))))

    # /states: row before the range + rows inside, oldest first, attributes
    attrs = ("current_position",)
    payload = influxql(";".join(q.states_queries("cover.compat_blind", "state", 1, attrs)))
    rows = q.parse_states(payload, ["cover.compat_blind"], attrs).get("cover.compat_blind", [])
    checks.append(("states: row before range + change inside, oldest first",
                   [r[1:] for r in rows] == [["closed", 0], ["open", 100]] and rows[0][0] < rows[1][0]))
    checks.append(("states: switch.* with same object id excluded", all(r[1] != "on" for r in rows)))
    payload = influxql(";".join(q.states_queries("cover.compat_blind", "state", 1, ("no_such_attribute",))))
    rows = q.parse_states(payload, ["cover.compat_blind"], ("no_such_attribute",)).get("cover.compat_blind", [])
    checks.append(("states: unknown attribute is null, not an error", [r[1:] for r in rows] == [["closed", None], ["open", None]]))
    payload = influxql(";".join(q.states_queries("sensor.compat_room", "°C", 1)))
    rows = q.parse_states(payload, ["sensor.compat_room"]).get("sensor.compat_room", [])
    checks.append(("states: numeric state read from value", [r[1] for r in rows] == [20.0, 21.0, 22.0, 23.0, 24.0]))
    payload = influxql(";".join(q.states_queries("cover.compat_blind", "state", 1, (), limit=1)))
    rows = q.parse_states(payload, ["cover.compat_blind"], (), limit=1).get("cover.compat_blind", [])
    checks.append(("states: row limit keeps the newest rows", [r[1] for r in rows] == ["open"]))

    # a fixed window (start/end, 1.2.0): the closed row 3 h ago is before a window of the last 2 h,
    # the open one 10 min ago inside; a window 4 h..2 h ago holds only the closed one
    now_ms = now_ns // 10**6
    win = (now_ms - 2 * 3600 * 1000, now_ms)
    payload = influxql(";".join(q.states_queries("cover.compat_blind", "state", 2 / 24, attrs, window=win)))
    rows = q.parse_states(payload, ["cover.compat_blind"], attrs).get("cover.compat_blind", [])
    checks.append(("states: fixed window - row before + change inside",
                   [r[1:] for r in rows] == [["closed", 0], ["open", 100]]))
    win = (now_ms - 4 * 3600 * 1000, now_ms - 2 * 3600 * 1000)
    payload = influxql(";".join(q.states_queries("cover.compat_blind", "state", 2 / 24, attrs, window=win)))
    rows = q.parse_states(payload, ["cover.compat_blind"], attrs).get("cover.compat_blind", [])
    checks.append(("states: fixed window in the past - only what was inside", [r[1:] for r in rows] == [["closed", 0]]))
    payload = influxql(q.series_query("sensor.compat_room", "°C", 1 / 24, (now_ms - 3600 * 1000, now_ms + 1000)))
    points = q.parse_results(payload, ["sensor.compat_room"]).get("sensor.compat_room", [])
    checks.append(("series: fixed window", bool(points) and max(p["max"] for p in points) == 24))

    status, _ = request("/query", {"db": DB, "q": "SHOW MEASUREMENTS LIMIT 1"})
    checks.append(("config-flow connection test query works", status == 200))

    width = max(len(name) for name, _ in checks)
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}")
    if not all(ok for _, ok in checks):
        raise SystemExit(1)
    print(f"OK  {URL}  all {len(checks)} checks passed")


if __name__ == "__main__":
    main()
