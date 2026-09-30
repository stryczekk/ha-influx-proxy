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
