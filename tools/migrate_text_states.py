"""Copy the TEXT states of an InfluxDB to VictoriaMetrics, as the proxy's
"Store text states" option writes them.

`vmctl influx` moves every number (values, numeric attributes) but has to
skip string fields - so `playing`, `heat` or a washing machine program would
be lost. This writes them as dictionary codes, on the same milliseconds as
the points vmctl copied, with the same free-text guards as the integration.
Run it once, after vmctl and before turning on "Store text states": it
refuses to run while text states are being written to the target, and
right after another run (VictoriaMetrics shows new series with a delay).

    INFLUX_USERNAME=... INFLUX_PASSWORD=... \\
    python tools/migrate_text_states.py --influx http://influx:8086 --vm http://victoriametrics:8428 [--dry-run]

Credentials come from the environment: INFLUX_USERNAME/INFLUX_PASSWORD or
INFLUX_TOKEN, VM_USERNAME/VM_PASSWORD or VM_TOKEN. Standard library only.
"""

from __future__ import annotations

import argparse
import sys
import time

from _common import Influx, VictoriaMetrics, q, vm

# the same guards as writer.py
MAX_ENTITY_LABELS = 64
MAX_LABELS = 5000
BATCH = 20000


def _numeric(state: str) -> bool:
    try:
        float(state)
    except ValueError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--influx", required=True, help="InfluxDB URL")
    parser.add_argument("--database", default="homeassistant", help="InfluxDB database (default homeassistant)")
    parser.add_argument("--vm", required=True, help="VictoriaMetrics URL")
    parser.add_argument("--vm-db", help="the db label in VictoriaMetrics (default: the database name, as vmctl sets it)")
    parser.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    parser.add_argument("--force", action="store_true", help="run although text states were written in the last 3 minutes")
    args = parser.parse_args()

    source = Influx(args.influx, args.database)
    target = VictoriaMetrics(args.vm, args.vm_db if args.vm_db is not None else args.database)
    if not args.dry_run and not args.force and target.recent_text_writes():
        sys.exit("Text states were written to VictoriaMetrics in the last 3 minutes - \"Store text states\" is on, "
                 "or a migration has just run. Two writers can give one code to two states: turn the option off "
                 "and wait 3 minutes (or use --force if you are sure).")
    dictionary = target.dictionary()
    if dictionary.ambiguous:
        sys.exit(f"The dictionary already has codes with two states: {dictionary.ambiguous} - fix that first.")
    codes = dict(dictionary.codes)
    taken = set(dictionary.labels) | set(dictionary.ambiguous)
    print(f"dictionary in VictoriaMetrics: {len(codes)} labels")

    lines: list[str] = []
    written = skipped_entities = 0
    started = time.time()

    def flush() -> None:
        nonlocal written
        if lines and not args.dry_run:
            target.post("/api/v1/import/prometheus", "\n".join(lines) + "\n")
        written += sum(1 for line in lines if line.startswith(vm.CODE_METRIC))
        lines.clear()

    for measurement, domain, object_id in source.series():
        entity_id = f"{domain}.{object_id}"
        rows = source.values(
            f'SELECT "state" FROM {q.quote_identifier(measurement)} '
            f'WHERE "domain" = {q.quote_string(domain)} AND "entity_id" = {q.quote_string(object_id)}'
        )
        rows = [(ts, s) for ts, s in rows if isinstance(s, str) and s not in ("", "unknown", "unavailable") and not _numeric(s)]
        if not rows:
            continue
        labels = {s for _, s in rows}
        if len(labels) > MAX_ENTITY_LABELS or not all(vm.is_state_label(s) for s in labels):
            skipped_entities += 1
            print(f"  skipped (free text): {entity_id} - {len(labels)} different states")
            continue
        if len(codes) + len(labels - codes.keys()) > MAX_LABELS:
            flush()
            print(f"dictionary full ({MAX_LABELS} labels) at {entity_id}: stopped; "
                  f"{'would have written' if args.dry_run else 'wrote'} {written} text states before it")
            return 1
        for label in sorted(labels - codes.keys()):
            codes[label] = max(taken | set(codes.values()), default=0) + 1
            # stamped now, not at its first use: an old sample would age out of the retention
            # period while newer codes still need it; before any code, in the same or an earlier request
            lines.append(vm.label_line(label, codes[label], int(time.time() * 1000), target.db))
        for ts, state in rows:
            lines.append(vm.code_line(entity_id, codes[state], ts, target.db))
            if len(lines) >= BATCH:
                flush()
        print(f"  {entity_id}: {len(rows)} text states")
    flush()
    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {written} text states, dictionary {len(codes)} labels, {skipped_entities} entities skipped "
          f"as free text, {time.time() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
