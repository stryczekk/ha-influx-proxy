"""VictoriaMetrics: reading back what Home Assistant's `influxdb` integration
writes to it - pure Python, no Home Assistant imports.

VictoriaMetrics takes the InfluxDB line protocol and turns every field into
its own series named `{measurement}_{field}`, with the tags (`domain`,
`entity_id`) and the database (`db`) as labels:

    °C_value{domain="sensor",entity_id="living_room",db="homeassistant"}
    cover.bedroom_current_position{domain="cover",entity_id="bedroom",...}

Only numbers survive: a string field (`state="playing"`) is stored as 0. The
`value` field carries the numeric state, which Home Assistant also writes for
on/off-like states (on/open/home/locked = 1, off/closed/not_home/unlocked = 0),
so those can be turned back into text. Free-text states are lost - unless
the proxy writes them itself (option "Store text states"): every non-numeric
state becomes a number from a dictionary kept in VictoriaMetrics too:

    ha_state_code{domain="media_player",entity_id="tv",db="homeassistant"} 7
    ha_state_label{label="playing",code="7",db="homeassistant"} 7

Codes never change; new states get the next free number.

Queries are built here from an entity_id and a time range, never forwarded
from the browser; label values are quoted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

from .query import group_interval

_STEP_SECONDS = {"5m": 300, "30m": 1800, "2h": 7200, "6h": 21600, "1d": 86400}

# The text of a 0/1 `value`, by domain - the reverse of Home Assistant's
# state_as_number for the states it turns into numbers.
_STATES = {
    "cover": ("closed", "open"),
    "valve": ("closed", "open"),
    "lock": ("unlocked", "locked"),
    "device_tracker": ("not_home", "home"),
    "person": ("not_home", "home"),
    "sun": ("below_horizon", "above_horizon"),
}
# Domains whose 1/0 means on/off. Elsewhere (select, sensor, climate...) a 1 may just be the state "1".
_ON_OFF = {
    "automation", "binary_sensor", "fan", "group", "humidifier", "input_boolean", "light",
    "remote", "schedule", "script", "siren", "switch", "update",
}

CODE_METRIC = "ha_state_code"
LABEL_METRIC = "ha_state_label"
ALL_TIME = "36500d"
# A label longer than this is free text, not a state.
MAX_LABEL_LENGTH = 100
# Dates and times are values, not a list of states (sensor.sun_next_rising, a completion time).
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2})?|^\d{1,2}:\d{2}(:\d{2})?$")


def quote_label(value: str) -> str:
    """A MetricsQL label value: a double-quoted string, escaped like JSON."""
    return json.dumps(value, ensure_ascii=False)


def selector(metric: str, entity_id: str, db: str = "") -> str:
    domain, object_id = entity_id.split(".", 1)
    labels = [
        f"__name__={quote_label(metric)}",
        f"domain={quote_label(domain)}",
        f"entity_id={quote_label(object_id)}",
    ]
    if db:
        labels.append(f"db={quote_label(db)}")
    return "{" + ",".join(labels) + "}"


def prometheus_label(value: str) -> str:
    """A label value in the Prometheus text format: backslash, quote and
    newline escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def code_line(entity_id: str, code: int, ts_ms: int, db: str = "") -> str:
    """One state change for /api/v1/import/prometheus."""
    domain, object_id = entity_id.split(".", 1)
    labels = f"domain={prometheus_label(domain)},entity_id={prometheus_label(object_id)}"
    if db:
        labels += f",db={prometheus_label(db)}"
    return f"{CODE_METRIC}{{{labels}}} {code} {ts_ms}"


def is_state_label(state: str) -> bool:
    """Whether a non-numeric state belongs in the dictionary: not free text,
    not a date or time."""
    return 0 < len(state) <= MAX_LABEL_LENGTH and not _DATE_TIME.match(state)


def label_line(label: str, code: int, ts_ms: int, db: str = "") -> str:
    """One dictionary entry for /api/v1/import/prometheus. The code is a
    label too, so the dictionary can be read from the series list, which
    shows new series at once (queries hide the last ~30 s of samples)."""
    labels = f"label={prometheus_label(label)},code=\"{int(code)}\""
    if db:
        labels += f",db={prometheus_label(db)}"
    return f"{LABEL_METRIC}{{{labels}}} {code} {ts_ms}"


def dictionary_params(db: str = "") -> list[tuple[str, str]]:
    """/api/v1/series parameters: every dictionary entry ever written."""
    sel = f"{LABEL_METRIC}{{db={quote_label(db)}}}" if db else LABEL_METRIC
    return [("match[]", sel), ("start", "0")]


@dataclass
class Dictionary:
    """The text-state dictionary as stored in VictoriaMetrics.

    `codes` is what a writer uses (label -> its lowest code), `labels` what a
    reader uses (code -> label). A label may have several codes (harmless:
    all read back as the label); a code with several labels means two
    writers assigned it at once - its rows cannot be told apart, so it is
    left out of `labels` and listed in `ambiguous`.
    """

    codes: dict[str, int] = field(default_factory=dict)
    labels: dict[int, str] = field(default_factory=dict)
    ambiguous: dict[int, list[str]] = field(default_factory=dict)


def parse_dictionary(payload: dict) -> Dictionary:
    """A /api/v1/series answer as a Dictionary."""
    seen: dict[int, set[str]] = {}
    for labels in payload.get("data") or []:
        label, code = labels.get("label"), labels.get("code")
        if label is not None and code is not None and code.isdigit():
            seen.setdefault(int(code), set()).add(label)
    out = Dictionary()
    for code, names in sorted(seen.items()):
        if len(names) > 1:
            out.ambiguous[code] = sorted(names)
            continue
        label = next(iter(names))
        out.labels[code] = label
        out.codes.setdefault(label, code)
    # a code shared by two labels is taken for both: never hand it out again
    for code, names in out.ambiguous.items():
        for label in names:
            out.codes.setdefault(label, code)
    return out


def last_before_params(entity_id: str, measurement: str, start_ms: int, db: str = "") -> dict[str, str]:
    """/api/v1/query parameters: the time of the last state change at or
    before `start_ms`, however long ago - VictoriaMetrics finds it from its
    index, the history itself is not sent. Then export just that moment."""
    value = selector(f"{measurement}_value", entity_id, db)
    code = selector(CODE_METRIC, entity_id, db)
    return {
        # `or` matches series by their labels without the name: label_set keeps the two apart
        "query": f'max(label_set(tlast_over_time({value}[{ALL_TIME}]), "f", "value") '
                 f'or label_set(tlast_over_time({code}[{ALL_TIME}]), "f", "code"))',
        "time": f"{start_ms / 1000:.3f}",
    }


def parse_last_before(payload: dict) -> int | None:
    """Epoch ms from a last_before_params answer, None without data."""
    for item in (payload.get("data") or {}).get("result") or []:
        value = item.get("value") or []
        if len(value) == 2:
            return round(float(value[1]) * 1000)
    return None


def time_range(days: float, window: tuple[int, int] | None, now_ms: int) -> tuple[int, int]:
    """(start, end) in epoch ms: the fixed window, or the last `days`."""
    if window is not None:
        return window
    return now_ms - int(days * 86400000), now_ms


def series_params(
    entity_id: str,
    measurement: str,
    days: float,
    window: tuple[int, int] | None,
    now_ms: int,
    db: str = "",
) -> dict[str, str]:
    """`/api/v1/query_range` parameters: mean/min/max of `value` per step,
    the same grouping as the InfluxDB queries.

    A point at time t covers (t - step, t]; the parser labels it with its
    start, as InfluxDB's GROUP BY time() does. VictoriaMetrics puts the points
    on multiples of the step, so the one holding the newest samples lies
    after `end`: the query goes one step further to include it. For a fixed
    window that last point may also hold samples up to the end of its step,
    just after `end` (the first one, samples just before the start).
    """
    step = group_interval(days)
    sel = selector(f"{measurement}_value", entity_id, db)
    query = "union(" + ", ".join(
        f'label_set({fn}({sel}[{step}]), "agg", "{agg}")'
        for fn, agg in (("avg_over_time", "mean"), ("min_over_time", "min"), ("max_over_time", "max"))
    ) + ")"
    start, end = time_range(days, window, now_ms)
    seconds = _STEP_SECONDS[step]
    return {
        "query": query,
        "start": str(start // 1000 + seconds),
        "end": str(-(-end // 1000) + seconds),
        "step": step,
    }


def parse_series(payload: dict, step: str, start_ms: int | None = None) -> list[dict]:
    """A query_range answer as points in the shape of HA long-term statistics.

    VictoriaMetrics may answer a point for the step before the range: points
    starting before the step that holds `start_ms` are dropped. The first
    point can still include samples from just before the range start.
    """
    shift = _STEP_SECONDS[step] * 1000
    first = (start_ms // shift) * shift if start_ms is not None else None
    points: dict[int, dict] = {}
    for item in (payload.get("data") or {}).get("result") or []:
        agg = (item.get("metric") or {}).get("agg")
        if agg not in ("mean", "min", "max"):
            continue
        for ts, value in item.get("values") or []:
            start = int(float(ts) * 1000) - shift
            if first is not None and start < first:
                continue
            point = points.setdefault(start, {"start": start, "end": start})
            point[agg] = float(value)
    return [p for _, p in sorted(points.items()) if {"mean", "min", "max"} <= p.keys()]


def export_params(
    entity_id: str,
    measurement: str,
    attributes: tuple[str, ...],
    start_ms: int | None,
    end_ms: int,
    db: str = "",
) -> list[tuple[str, str]]:
    """`/api/v1/export` parameters for the raw samples of `value` and the
    attributes; `start_ms` None = from the beginning."""
    params = [
        ("match[]", selector(f"{measurement}_{field}", entity_id, db))
        for field in ("value", *attributes)
    ]
    params.append(("match[]", selector(CODE_METRIC, entity_id, db)))
    if start_ms is not None:
        params.append(("start", f"{start_ms / 1000:.3f}"))
    params.append(("end", f"{end_ms / 1000:.3f}"))
    return params


def state_text(entity_id: str, value: float) -> str | float:
    """The state as Home Assistant shows it, from the stored number."""
    domain = entity_id.split(".", 1)[0]
    if value in (0, 1) and (domain in _STATES or domain in _ON_OFF):
        off, on = _STATES.get(domain, ("off", "on"))
        return on if value == 1 else off
    return int(value) if value.is_integer() else value


def parse_export(
    body: bytes,
    entity_id: str,
    measurement: str,
    attributes: tuple[str, ...] = (),
    labels: dict[int, str] | None = None,
) -> list[list]:
    """Export lines (one JSON object per series) as rows
    [epoch_ms, state, *attributes], oldest first.

    A row is a sample of `value` or of the state code the proxy wrote; at the
    same moment the code wins (it holds the exact text). Attributes are
    matched by timestamp, as Home Assistant writes all fields of a state
    change in one point - and the proxy stamps its codes with the same time.
    """
    fields: dict[str, dict[int, float]] = {}
    codes: dict[int, float] = {}
    prefix = f"{measurement}_"
    for line in body.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        name = (item.get("metric") or {}).get("__name__", "")
        pairs = zip(item.get("timestamps") or [], item.get("values") or [])
        if name == CODE_METRIC:
            codes.update(pairs)
        elif name.startswith(prefix):
            fields.setdefault(name[len(prefix):], {}).update(pairs)
    states: dict[int, str | float] = {
        ts: state_text(entity_id, float(v)) for ts, v in fields.get("value", {}).items()
    }
    for ts, code in codes.items():
        if (label := (labels or {}).get(int(code))) is not None:
            states[ts] = label
    return [[ts, states[ts], *(fields.get(a, {}).get(ts) for a in attributes)] for ts in sorted(states)]


def states_rows(
    before: list[list], inside: list[list], start_ms: int, limit: int
) -> list[list]:
    """The last row at or before the range start, then the rows inside
    (start, end], newest `limit` kept; the row before is dropped when the
    limit cut the range, as for InfluxDB."""
    inside = [r for r in inside if r[0] > start_ms]
    if len(inside) >= limit:
        return inside[-limit:]
    last = [r for r in before if r[0] <= start_ms][-1:]
    return last + inside
