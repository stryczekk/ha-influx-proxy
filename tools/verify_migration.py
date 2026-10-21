"""Check a migration from InfluxDB: will the cards see the same history?

Compares the source InfluxDB with the target - VictoriaMetrics (after
`vmctl influx` and tools/migrate_text_states.py) or another InfluxDB
(InfluxDB 3 after an export/import):

1. every series: number of numeric points and the time of the last one;
   for VictoriaMetrics also the number of text states;
2. for a sample of entities, what the proxy answers from both databases:
   `/states` rows (last 7 days and a random past day) and `/series` points
   (last 30 days) - computed with the integration's own code.

    INFLUX_USERNAME=... INFLUX_PASSWORD=... \\
    python tools/verify_migration.py --influx http://influx:8086 --vm http://victoriametrics:8428
    python tools/verify_migration.py --influx http://influx:8086 --target-influx http://influx3:8181

Credentials: INFLUX_USERNAME/INFLUX_PASSWORD or INFLUX_TOKEN for the source,
VM_USERNAME/VM_PASSWORD or VM_TOKEN for VictoriaMetrics, TARGET_INFLUX_TOKEN for
a target InfluxDB. Exit code 0 = no differences. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

from _common import Influx, VictoriaMetrics, q, vm

ALL_TIME = "36500d"


def _numeric(state) -> bool:
    try:
        float(state)
    except (TypeError, ValueError):
        return False
    return True


def source_counts(source: Influx, days: int | None) -> dict[tuple[str, str, str], tuple[int, int]]:
    """{(measurement, domain, entity_id): (count of value, ms of the last one)}"""
    out = {}
    since = f" WHERE time > now() - {days}d" if days else ""
    # GROUP BY the two tags only: series that differ in other tags (an imported history with a
    # `source` tag, say) are counted together - the proxy reads them together too
    for item in source.query(f'SELECT count("value") FROM /.*/{since} GROUP BY "domain", "entity_id"')["results"][0].get("series") or []:
        tags = item.get("tags") or {}
        count = item["values"][0][1]
        if count:
            last = source.values(
                f'SELECT last("value") FROM {q.quote_identifier(item["name"])} WHERE "domain" = '
                f'{q.quote_string(tags["domain"])} AND "entity_id" = {q.quote_string(tags["entity_id"])}'
                + (f" AND time > now() - {days}d" if days else "")
            )
            out[(item["name"], tags["domain"], tags["entity_id"])] = (count, last[0][0] if last else 0)
    return out


def vm_counts(target: VictoriaMetrics, days: int | None) -> dict[tuple[str, str, str], tuple[int, int]]:
    """The same from VictoriaMetrics: `{measurement}_value` series."""
    out: dict[tuple[str, str, str], list[int]] = {}
    db = f',db={vm.quote_label(target.db)}' if target.db else ""
    for fn, slot in (("count_over_time", 0), ("tlast_over_time", 1)):
        payload = json.loads(target.get("/api/v1/query", {
            "query": f'{fn}({{__name__=~".+_value"{db}}}[{f"{days}d" if days else ALL_TIME}]) keep_metric_names',
            "time": str(int(time.time()))}))
        for item in payload["data"]["result"]:
            m = item["metric"]
            key = (m["__name__"][: -len("_value")], m.get("domain", ""), m.get("entity_id", ""))
            value = int(float(item["value"][1]) * (1000 if slot else 1))
            pair = out.setdefault(key, [0, 0])
            pair[slot] = pair[slot] + value if slot == 0 else max(pair[slot], value)
    return {k: (v[0], v[1]) for k, v in out.items()}


def vm_text_counts(target: VictoriaMetrics, days: int | None) -> dict[tuple[str, str], int]:
    db = f'{{db={vm.quote_label(target.db)}}}' if target.db else ""
    payload = json.loads(target.get("/api/v1/query", {
        "query": f"count_over_time({vm.CODE_METRIC}{db}[{f'{days}d' if days else ALL_TIME}])", "time": str(int(time.time()))}))
    return {(i["metric"].get("domain", ""), i["metric"].get("entity_id", "")): int(float(i["value"][1]))
            for i in payload["data"]["result"]}


def source_text_counts(source: Influx, series, days: int | None) -> dict[tuple[str, str], int]:
    """Text states the migration writes: the same guards as migrate_text_states.py."""
    out: dict[tuple[str, str], int] = {}
    for measurement, domain, object_id in series:
        rows = source.values(
            f'SELECT "state" FROM {q.quote_identifier(measurement)} '
            f'WHERE "domain" = {q.quote_string(domain)} AND "entity_id" = {q.quote_string(object_id)}'
            + (f" AND time > now() - {days}d" if days else "")
        )
        states = [s for _, s in rows if isinstance(s, str) and s not in ("", "unknown", "unavailable") and not _numeric(s)]
        labels = set(states)
        if states and len(labels) <= 64 and all(vm.is_state_label(s) for s in labels):
            out[(domain, object_id)] = out.get((domain, object_id), 0) + len(states)
    return out


def states_influx(db: Influx, entity_id: str, measurement: str, window: tuple[int, int]) -> list[list]:
    days = (window[1] - window[0]) / 86400000
    payload = db.query(";".join(q.states_queries(entity_id, measurement, days, window=window)))
    missing = q.missing_before(payload, [entity_id])
    rows = q.parse_states(payload, [entity_id])
    if missing:
        rows = q.merge_before(rows, db.query(q.before_query(entity_id, measurement, days, window=window)), missing)
    return rows.get(entity_id, [])


def states_vm(target: VictoriaMetrics, entity_id: str, measurement: str, window: tuple[int, int], labels) -> list[list]:
    start, end = window
    rows = vm.parse_export(target.get("/api/v1/export", vm.export_params(
        entity_id, measurement, (), start - q.BEFORE_LOOKBACK_HOURS * 3600_000, end, target.db)), entity_id, measurement, (), labels)
    if not any(r[0] <= start for r in rows) and (t := target.last_before(entity_id, measurement, start)) is not None:
        older = vm.parse_export(target.get("/api/v1/export", vm.export_params(
            entity_id, measurement, (), t, t, target.db)), entity_id, measurement, (), labels)
        rows = older[-1:] + rows
    return vm.states_rows(rows, rows, start, 250000)


def series_influx(db: Influx, entity_id: str, measurement: str, window: tuple[int, int]) -> list[dict]:
    days = (window[1] - window[0]) / 86400000
    return q.parse_results(db.query(q.series_query(entity_id, measurement, days, window)), [entity_id]).get(entity_id, [])


def series_vm(target: VictoriaMetrics, entity_id: str, measurement: str, window: tuple[int, int]) -> list[dict]:
    days = (window[1] - window[0]) / 86400000
    params = vm.series_params(entity_id, measurement, days, window, 0, target.db)
    return vm.parse_series(json.loads(target.get("/api/v1/query_range", params)), params["step"], window[0])


def _first_difference(a: list, b: list) -> str:
    for x, y in zip(a, b):
        if x != y:
            return f"source {x} / target {y}"
    return f"source has {len(a)}, target {len(b)}; extra: {(a[len(b):] or b[len(a):])[:2]}"


def _norm(rows: list[list]) -> list[list]:
    """Numbers to 6 decimals (VictoriaMetrics keeps about 12 significant digits); rows in the same
    millisecond with the same state as one (InfluxDB keeps nanoseconds, VictoriaMetrics milliseconds)."""
    out: list[list] = []
    for r in rows:
        row = [r[0], round(r[1], 6) if isinstance(r[1], (int, float)) else r[1]]
        if not out or out[-1] != row:
            out.append(row)
    return out


def _comparable(a: list[list], b: list[list], vm_target: bool, free_text: bool) -> tuple[list[list], list[list]]:
    """For VictoriaMetrics the text rows of an entity the migration skipped as free text are missing
    by design; any other missing row stays a difference."""
    a, b = _norm(a), _norm(b)
    if vm_target and free_text:
        kept = {x[0] for x in b}
        a = [r for r in a if r[0] in kept or not isinstance(r[1], str)]
    return a, b


_free_text: dict[tuple[str, str, str], bool] = {}


def is_free_text(source: Influx, measurement: str, domain: str, object_id: str) -> bool:
    """Whether migrate_text_states.py skips this entity: the same guards over its whole history."""
    key = (measurement, domain, object_id)
    if key not in _free_text:
        rows = source.values(
            f'SELECT "state" FROM {q.quote_identifier(measurement)} '
            f'WHERE "domain" = {q.quote_string(domain)} AND "entity_id" = {q.quote_string(object_id)}'
        )
        labels = {s for _, s in rows if isinstance(s, str) and s not in ("", "unknown", "unavailable") and not _numeric(s)}
        _free_text[key] = len(labels) > 64 or not all(vm.is_state_label(s) for s in labels)
    return _free_text[key]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--influx", required=True, help="source InfluxDB URL")
    parser.add_argument("--database", default="homeassistant")
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--vm", help="target VictoriaMetrics URL")
    target_group.add_argument("--target-influx", help="target InfluxDB URL (e.g. InfluxDB 3)")
    parser.add_argument("--target-database", help="target database / db label (default: --database)")
    parser.add_argument("--sample", type=int, default=30, help="entities compared through the proxy's queries")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--days", type=int, help="compare only the last N days (a partial migration)")
    parser.add_argument("--no-counts", action="store_true",
                        help="skip step 1 - counting the whole database is slow on InfluxDB 3 Core")
    args = parser.parse_args()

    source = Influx(args.influx, args.database)
    target_db = args.target_database or args.database
    if args.vm:
        target = VictoriaMetrics(args.vm, target_db)
    else:
        target = Influx(args.target_influx, target_db)
        if os.environ.get("TARGET_INFLUX_TOKEN"):
            target.headers = {"Authorization": f"Token {os.environ['TARGET_INFLUX_TOKEN']}"}
    problems: list[str] = []

    print("1. every series" + (" - skipped" if args.no_counts else ""))
    src = source_counts(source, args.days)
    dst = src if args.no_counts else vm_counts(target, args.days) if args.vm else source_counts(target, args.days)
    for key, (count, last) in sorted(src.items()):
        got = dst.get(key)
        if got is None:
            problems.append(f"missing series {key}")
        elif got != (count, last):
            problems.append(f"{key}: source {count} points, last {last}; target {got[0]}, last {got[1]}")
    print(f"   numeric series: {len(src)} in the source, {len(dst)} in the target")
    if args.vm and not args.no_counts:
        series = source.series()
        want = source_text_counts(source, series, args.days)
        have = vm_text_counts(target, args.days)
        for key, count in sorted(want.items()):
            if have.get(key) != count:
                problems.append(f"text states of {key[0]}.{key[1]}: source {count}, target {have.get(key, 0)}")
        print(f"   entities with text states: {len(want)} expected, {len(have)} in the target")

    print(f"2. what the proxy answers, {args.sample} entities")
    rng = random.Random(args.seed)
    keys = sorted({(m, d, e) for (m, d, e) in src})
    sample = rng.sample(keys, min(args.sample, len(keys)))
    now = int(time.time() * 1000)
    labels = target.dictionary().labels if args.vm else {}
    checked = 0
    for measurement, domain, object_id in sample:
        entity_id = f"{domain}.{object_id}"
        last = src[(measurement, domain, object_id)][1]
        day = last - rng.randint(0, max(0, min(30, (args.days or 32) - 2))) * 86400000
        day -= rng.randint(1, 3599) * 1000  # not on a sample: InfluxDB has nanoseconds, the window milliseconds
        for name, window in (("last 7 days", (now - 7 * 86400000, now)), ("a past day", (day - 86400000, day))):
            a = states_influx(source, entity_id, measurement, window)
            b = states_vm(target, entity_id, measurement, window, labels) if args.vm else states_influx(target, entity_id, measurement, window)
            a, b = _comparable(a, b, bool(args.vm), bool(args.vm) and is_free_text(source, measurement, domain, object_id))
            if a != b:
                problems.append(f"/states {entity_id} ({name}, {window}): {_first_difference(a, b)}")
        window = (now - 30 * 86400000, now)
        a = series_influx(source, entity_id, measurement, window)
        b = series_vm(target, entity_id, measurement, window) if args.vm else series_influx(target, entity_id, measurement, window)
        # the step holding the range start may have samples from just before it in VictoriaMetrics
        step = 7200000
        first = window[0] // step * step
        pa = [(p["start"], round(p["min"], 6), round(p["max"], 6)) for p in a if p["start"] > first]
        pb = [(p["start"], round(p["min"], 6), round(p["max"], 6)) for p in b if p["start"] > first]
        if pa != pb:
            problems.append(f"/series {entity_id} (30 days): {_first_difference(pa, pb)}")
        checked += 1
    print(f"   {checked} entities: /states last 7 days and a past day, /series 30 days")

    for line in problems[:50]:
        print("   DIFF", line)
    if len(problems) > 50:
        print(f"   ... and {len(problems) - 50} more")
    print("OK - the cards will see the same history" if not problems else f"{len(problems)} differences")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
