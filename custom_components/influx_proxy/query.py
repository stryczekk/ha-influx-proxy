"""InfluxQL query building - pure Python, no Home Assistant imports.

Kept separate so it can be unit tested without a Home Assistant instance.
The proxy never forwards InfluxQL from the browser: every query is built
here from an entity_id and a time range, with proper InfluxQL quoting.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

# Option VALUES as stored in the config entry. hassfest rejects "__" in
# translation keys, so the domain/device-class mode is stored as
# "domain_device_class" and means HA's measurement_attr "domain__device_class".
MEASUREMENT_UNIT = "unit_of_measurement"
MEASUREMENT_DOMAIN_DEVICE_CLASS = "domain_device_class"
MEASUREMENT_ENTITY_ID = "entity_id"
MEASUREMENT_MODES = (
    MEASUREMENT_UNIT,
    MEASUREMENT_DOMAIN_DEVICE_CLASS,
    MEASUREMENT_ENTITY_ID,
)

_ENTITY_ID = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")


@dataclass(frozen=True)
class MeasurementNaming:
    """Mirror of the measurement options of Home Assistant's `influxdb` integration."""

    mode: str = MEASUREMENT_UNIT
    default_measurement: str = ""
    override_measurement: str = ""


def valid_entity_id(entity_id: str) -> bool:
    """Same rule as homeassistant.core.valid_entity_id, but fullmatch.

    `match` with `$` would accept a trailing newline ("sensor.x\n").
    """
    return bool(_ENTITY_ID.fullmatch(entity_id))


def quote_identifier(name: str) -> str:
    """Double-quoted InfluxQL identifier (measurement, field, tag key)."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def quote_string(value: str) -> str:
    """Single-quoted InfluxQL string literal (tag value)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def measurement_for(
    entity_id: str,
    unit: str | None,
    device_class: str | None,
    naming: MeasurementNaming,
) -> str:
    """Name the measurement exactly the way Home Assistant writes it.

    Follows homeassistant/components/influxdb (_generate_event_to_json): an
    override wins; otherwise the configured attribute - in domain/device-class
    mode an entity WITHOUT a device class goes to a measurement named after
    its domain alone; if the attribute is empty, the default measurement or,
    failing that, the full entity_id. (HA's per-entity override_measurement
    from `component_config` cannot be mirrored - use the global override.)
    """
    if naming.override_measurement:
        return naming.override_measurement
    domain = entity_id.split(".", 1)[0]
    if naming.mode == MEASUREMENT_ENTITY_ID:
        measurement = entity_id
    elif naming.mode == MEASUREMENT_DOMAIN_DEVICE_CLASS:
        measurement = f"{domain}__{device_class}" if device_class else domain
    else:
        measurement = unit or ""
    if not measurement:
        measurement = naming.default_measurement or entity_id
    return measurement


def group_interval(days: float) -> str:
    """Aggregation step that yields a few hundred points for the range.

    A month of raw data is hundreds of thousands of points that cannot be
    told apart on a chart a few hundred pixels wide anyway.
    """
    if days <= 1:
        return "5m"
    if days <= 7:
        return "30m"
    if days <= 31:
        return "2h"
    if days <= 120:
        return "6h"
    return "1d"


def _rfc3339(ms: int) -> str:
    """Epoch milliseconds as an RFC 3339 UTC timestamp with milliseconds."""
    secs, milli = divmod(int(ms), 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(secs)) + f".{milli:03d}Z"


def range_clauses(days: float, window: tuple[int, int] | None = None) -> tuple[str, str]:
    """WHERE clauses for the rows inside the range and for the row before it.

    Either the last `days` up to now, or a `window` of two epoch milliseconds
    (start exclusive, end inclusive) - a given day, for example.
    """
    if window is not None:
        # RFC 3339 string literals: understood by every InfluxQL (1.x, 2.x's v1 API, 3.x)
        start, end = (_rfc3339(window[0]), _rfc3339(window[1]))
        return f"AND time > '{start}' AND time <= '{end}' ", f"AND time <= '{start}' "
    # InfluxQL duration literals must be integers ("30.0d" is a syntax
    # error); counting in hours also allows ranges shorter than a day.
    hours = max(1, int(round(days * 24)))
    return f"AND time > now() - {hours}h ", f"AND time <= now() - {hours}h "


def series_query(
    entity_id: str, measurement: str, days: float, window: tuple[int, int] | None = None
) -> str:
    """Mean/min/max of the `value` field, grouped for the requested range.

    Filters on both tags Home Assistant writes: `domain` and `entity_id`
    (the object id, without the domain) - otherwise sensor.x and
    binary_sensor.x would be merged into one series. `days` is the length of
    the range (it picks the grouping), `window` an optional fixed range.
    """
    domain, object_id = entity_id.split(".", 1)
    inside, _ = range_clauses(days, window)
    return (
        'SELECT mean("value"), min("value"), max("value") '
        f"FROM {quote_identifier(measurement)} "
        f"WHERE {quote_identifier('domain')} = {quote_string(domain)} "
        f"AND {quote_identifier('entity_id')} = {quote_string(object_id)} "
        f"{inside}"
        f"GROUP BY time({group_interval(days)}) fill(none)"
    )


def parse_results(payload: dict, order: list[str]) -> dict[str, list[dict]]:
    """Turn an InfluxDB /query response into {entity_id: [points]}.

    Point shape matches Home Assistant long-term statistics
    (start/end/mean/min/max, epoch ms), so a card can treat both sources
    the same way.
    """
    result: dict[str, list[dict]] = {}
    for index, item in enumerate(payload.get("results") or []):
        if index >= len(order):
            break
        series = item.get("series") or []
        if not series:
            continue
        points = [
            {"start": row[0], "end": row[0], "mean": row[1], "min": row[2], "max": row[3]}
            for row in (series[0].get("values") or [])
            if row[1] is not None
        ]
        if points:
            result[order[index]] = points
    return result


def unsafe_measurement(name: str) -> bool:
    """Control characters cannot come from real data (line protocol forbids
    them) and make InfluxQL reject the whole `;`-joined request."""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in name)


def first_error(payload: dict) -> str | None:
    """InfluxDB reports per-statement errors inside a 200 response."""
    if not isinstance(payload, dict):
        return "unexpected response"
    if payload.get("error"):
        return str(payload["error"])
    for item in payload.get("results") or []:
        if item.get("error"):
            return str(item["error"])
    return None


# --- raw state changes (/states) -------------------------------------------

# Attribute fields a client may ask for next to the state. Home Assistant
# writes numeric attributes as fields under their own name (strings get a
# `_str` suffix), e.g. `current_position` of a cover or `brightness`.
MAX_STATE_ATTRIBUTES = 4
_ATTRIBUTE = re.compile(r"[a-z0-9_]{1,64}")


def valid_attribute(name: str) -> bool:
    return bool(_ATTRIBUTE.fullmatch(name))


def states_queries(
    entity_id: str,
    measurement: str,
    days: float,
    attributes: tuple[str, ...] = (),
    limit: int = 250000,
    window: tuple[int, int] | None = None,
) -> list[str]:
    """Two statements per entity: the last row BEFORE the range (the state the
    range starts in) and the rows inside it, newest first so that a hit limit
    cuts off the oldest rows, not the most recent ones.

    `state` holds non-numeric states ("on", "open"), `value` numeric ones -
    Home Assistant writes one or both, so both are read.
    """
    domain, object_id = entity_id.split(".", 1)
    inside, before = range_clauses(days, window)
    fields = ", ".join(quote_identifier(f) for f in ("state", "value", *attributes))
    source = (
        f"SELECT {fields} FROM {quote_identifier(measurement)} "
        f"WHERE {quote_identifier('domain')} = {quote_string(domain)} "
        f"AND {quote_identifier('entity_id')} = {quote_string(object_id)} "
    )
    return [
        source + before + "ORDER BY time DESC LIMIT 1",
        source + inside + f"ORDER BY time DESC LIMIT {int(limit)}",
    ]


def _state_rows(item: dict, attributes: tuple[str, ...]) -> list[list]:
    """Rows of one statement as [epoch_ms, state, *attributes].

    Columns are read by name: InfluxDB returns them as asked, but a missing
    field may be left out entirely by some versions.
    """
    rows = []
    for series in item.get("series") or []:
        columns = series.get("columns") or []
        for values in series.get("values") or []:
            row = dict(zip(columns, values))
            state = row.get("state")
            if state is None:
                state = row.get("value")
            if state is None:
                continue
            rows.append([row.get("time"), state, *(row.get(a) for a in attributes)])
    return rows


def parse_states(
    payload: dict, order: list[str], attributes: tuple[str, ...] = (), limit: int = 250000
) -> dict[str, list[list]]:
    """Turn the paired statements of states_queries into
    {entity_id: [[epoch_ms, state, *attributes], ...]}, oldest first.

    When the range hit the row limit, the row before the range is dropped:
    the data then starts at the first returned row, not at the range start.
    """
    results = payload.get("results") or []
    out: dict[str, list[list]] = {}
    for index, entity_id in enumerate(order):
        pair = results[2 * index:2 * index + 2]
        if len(pair) < 2:
            break
        before = _state_rows(pair[0], attributes)
        inside = _state_rows(pair[1], attributes)
        inside.sort(key=lambda r: r[0])
        rows = inside if len(inside) >= limit else before[:1] + inside
        if rows:
            out[entity_id] = rows
    return out
