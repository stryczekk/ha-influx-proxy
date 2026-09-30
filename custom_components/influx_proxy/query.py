"""InfluxQL query building - pure Python, no Home Assistant imports.

Kept separate so it can be unit tested without a Home Assistant instance.
The proxy never forwards InfluxQL from the browser: every query is built
here from an entity_id and a time range, with proper InfluxQL quoting.
"""

from __future__ import annotations

import re
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


def series_query(entity_id: str, measurement: str, days: float) -> str:
    """Mean/min/max of the `value` field, grouped for the requested range.

    Filters on both tags Home Assistant writes: `domain` and `entity_id`
    (the object id, without the domain) - otherwise sensor.x and
    binary_sensor.x would be merged into one series.
    """
    domain, object_id = entity_id.split(".", 1)
    # InfluxQL duration literals must be integers ("30.0d" is a syntax
    # error); counting in hours also allows ranges shorter than a day.
    hours = max(1, int(round(days * 24)))
    return (
        'SELECT mean("value"), min("value"), max("value") '
        f"FROM {quote_identifier(measurement)} "
        f"WHERE {quote_identifier('domain')} = {quote_string(domain)} "
        f"AND {quote_identifier('entity_id')} = {quote_string(object_id)} "
        f"AND time > now() - {hours}h "
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
