"""InfluxDB Proxy - long-term history from InfluxDB for Lovelace cards.

A card runs in the browser, so querying InfluxDB directly only works on
the local network: through a remote-access tunnel that points at Home
Assistant the browser cannot reach a separate database host. This
integration exposes an endpoint INSIDE Home Assistant instead, so cards
only ever talk to HA - with the user's HA session, and without the
database password ever reaching the browser.

    GET /api/influx_proxy/series?entities=sensor.a,sensor.b&days=30
    GET /api/influx_proxy/states?entities=light.a,cover.b&days=90&attributes=current_position
    GET /api/influx_proxy/states?entities=light.a&start=1790000000000&end=1790086400000

The endpoints build every query themselves from entity ids; they do not
accept InfluxQL from the client.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiohttp import web
import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import DOMAIN as HA_DOMAIN, HomeAssistant
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .client import InfluxError, run_query
from .const import (
    API_PATH,
    API_STATES_PATH,
    CONF_DATABASE,
    CONF_DEFAULT_MEASUREMENT,
    CONF_MAX_DAYS,
    CONF_MAX_ENTITIES,
    CONF_MAX_STATE_ROWS,
    CONF_MEASUREMENT_MODE,
    CONF_OVERRIDE_MEASUREMENT,
    DEFAULT_DATABASE,
    DEFAULT_MAX_DAYS,
    DEFAULT_MAX_ENTITIES,
    DEFAULT_MAX_STATE_ROWS,
    DOMAIN,
    STATES_MAX_ENTITIES,
)
from .query import (
    MAX_STATE_ATTRIBUTES,
    MEASUREMENT_UNIT,
    MeasurementNaming,
    measurement_for,
    parse_results,
    parse_states,
    series_query,
    states_queries,
    unsafe_measurement,
    valid_attribute,
    valid_entity_id,
)

_LOGGER = logging.getLogger(__name__)

# Concurrent queries sent to InfluxDB. A logged-in user cannot pile up
# unbounded long-range queries; extra requests simply wait their turn.
MAX_CONCURRENT_QUERIES = 4

# Legacy YAML configuration (from before the config flow). Still accepted
# and imported into a config entry once, with a repair issue asking to
# remove it.
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_URL): cv.string,
                vol.Optional(CONF_DATABASE, default=DEFAULT_DATABASE): cv.string,
                vol.Optional(CONF_USERNAME, default=""): cv.string,
                vol.Optional(CONF_PASSWORD, default=""): cv.string,
                vol.Optional(CONF_MAX_ENTITIES, default=DEFAULT_MAX_ENTITIES): cv.positive_int,
                vol.Optional(CONF_MAX_DAYS, default=DEFAULT_MAX_DAYS): cv.positive_int,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the endpoint once; import legacy YAML if present."""
    hass.data.setdefault(DOMAIN, {})
    # A view cannot be unregistered, so it is registered once here and
    # answers 503 while no config entry is loaded.
    slots = asyncio.Semaphore(MAX_CONCURRENT_QUERIES)
    hass.http.register_view(SeriesView(hass, slots))
    hass.http.register_view(StatesView(hass, slots))

    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=dict(config[DOMAIN])
            )
        )
        ir.async_create_issue(
            hass,
            HA_DOMAIN,
            f"deprecated_yaml_{DOMAIN}",
            is_fixable=False,
            issue_domain=DOMAIN,
            severity=ir.IssueSeverity.WARNING,
            translation_key="deprecated_yaml",
            translation_placeholders={"domain": DOMAIN, "integration_title": "InfluxDB Proxy"},
        )
    else:
        # YAML already removed: withdraw the notice, otherwise it would stay
        # in the repairs panel forever.
        ir.async_delete_issue(hass, HA_DOMAIN, f"deprecated_yaml_{DOMAIN}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data[DOMAIN]["settings"] = {**entry.data, **entry.options}
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.debug("Endpoint %s serving %s", API_PATH, entry.data[CONF_URL])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data[DOMAIN].pop("settings", None)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


class _InfluxView(HomeAssistantView):
    """Shared request validation and query execution."""

    # requires_auth defaults to True: the endpoints are protected like the
    # rest of the HA API, cards need no credentials of their own.

    def __init__(self, hass: HomeAssistant, slots: asyncio.Semaphore) -> None:
        self._hass = hass
        # shared by both endpoints: at most MAX_CONCURRENT_QUERIES in total
        self._slots = slots

    def _parse(
        self, request: web.Request, settings: dict[str, Any], max_entities: int, default_days: str
    ) -> tuple[list[str], float, tuple[int, int] | None] | web.Response:
        raw = (request.query.get("entities") or "").strip()
        # de-duplicated, order kept
        entity_ids = list(dict.fromkeys(e.strip() for e in raw.split(",") if e.strip()))
        if not entity_ids:
            return self.json_message("missing parameter: entities", 400)
        if len(entity_ids) > max_entities:
            return self.json_message(f"too many entities (limit {max_entities})", 400)
        if not all(valid_entity_id(e) for e in entity_ids):
            return self.json_message("invalid entity_id", 400)
        # Same entity permissions as the rest of the HA API (non-admin users
        # have read access to all entities today, but policies may restrict it).
        user = request.get("hass_user")
        if user is not None and not all(user.permissions.check_entity(e, POLICY_READ) for e in entity_ids):
            return self.json_message("not allowed", 403)

        max_days = int(settings.get(CONF_MAX_DAYS, DEFAULT_MAX_DAYS))
        # A fixed range: start (and optionally end, default now) in epoch
        # milliseconds - e.g. one day months ago without fetching everything since.
        if "start" in request.query:
            now_ms = int(time.time() * 1000)
            try:
                start = int(request.query["start"])
                end = int(request.query.get("end", now_ms))
            except ValueError:
                return self.json_message("start and end must be epoch milliseconds", 400)
            if not 0 < start < end:
                return self.json_message("start must be positive and before end", 400)
            days = (end - start) / 86400000
            if days > max_days:
                return self.json_message(f"range longer than {max_days} days", 400)
            return entity_ids, days, (start, end)
        if "end" in request.query:
            return self.json_message("end needs start", 400)
        try:
            days = float(request.query.get("days", default_days))
        except ValueError:
            return self.json_message("days must be a number", 400)
        if not 0 < days <= max_days:
            return self.json_message(f"days out of range (0, {max_days}]", 400)
        return entity_ids, days, None

    def _measurements(self, settings: dict[str, Any], entity_ids: list[str]) -> list[tuple[str, str]]:
        """(entity_id, measurement) for every entity that can be queried."""
        naming = MeasurementNaming(
            mode=settings.get(CONF_MEASUREMENT_MODE, MEASUREMENT_UNIT),
            default_measurement=settings.get(CONF_DEFAULT_MEASUREMENT, ""),
            override_measurement=settings.get(CONF_OVERRIDE_MEASUREMENT, ""),
        )
        out = []
        for entity_id in entity_ids:
            state = self._hass.states.get(entity_id)
            attrs = state.attributes if state is not None else {}
            measurement = measurement_for(
                entity_id,
                attrs.get("unit_of_measurement"),
                attrs.get("device_class"),
                naming,
            )
            if unsafe_measurement(measurement):
                # cannot exist in InfluxDB and would break the whole request
                continue
            out.append((entity_id, measurement))
        return out

    async def _query(self, settings: dict[str, Any], queries: list[str]) -> dict | web.Response:
        try:
            async with self._slots:
                return await run_query(
                    async_get_clientsession(self._hass, settings.get("verify_ssl", True)),
                    settings,
                    ";".join(queries),
                )
        except InfluxError as err:
            # detail (host, port, InfluxDB error text) only in the log -
            # any logged-in user, admin or not, can call these endpoints
            _LOGGER.warning("InfluxDB query failed: %s", err)
            return self.json_message(f"InfluxDB query failed ({err.kind})", 502)

    def _settings(self) -> dict[str, Any] | None:
        return self._hass.data.get(DOMAIN, {}).get("settings")


class SeriesView(_InfluxView):
    """Return time series in the shape of HA long-term statistics."""

    url = API_PATH
    name = "api:influx_proxy:series"

    async def get(self, request: web.Request) -> web.Response:
        settings = self._settings()
        if settings is None:
            return self.json_message("InfluxDB Proxy is not configured", 503)
        parsed = self._parse(
            request, settings, int(settings.get(CONF_MAX_ENTITIES, DEFAULT_MAX_ENTITIES)), "7"
        )
        if isinstance(parsed, web.Response):
            return parsed
        entity_ids, days, window = parsed

        targets = self._measurements(settings, entity_ids)
        if not targets:
            return self.json({})
        payload = await self._query(settings, [series_query(e, m, days, window) for e, m in targets])
        if isinstance(payload, web.Response):
            return payload
        return self.json(parse_results(payload, [e for e, _ in targets]))


class StatesView(_InfluxView):
    """Raw state changes, for counting how long something was on, open or
    closed over a range longer than the recorder keeps."""

    url = API_STATES_PATH
    name = "api:influx_proxy:states"

    async def get(self, request: web.Request) -> web.Response:
        settings = self._settings()
        if settings is None:
            return self.json_message("InfluxDB Proxy is not configured", 503)
        parsed = self._parse(request, settings, STATES_MAX_ENTITIES, "30")
        if isinstance(parsed, web.Response):
            return parsed
        entity_ids, days, window = parsed

        raw = (request.query.get("attributes") or "").strip()
        attributes = tuple(dict.fromkeys(a.strip() for a in raw.split(",") if a.strip()))
        if len(attributes) > MAX_STATE_ATTRIBUTES:
            return self.json_message(f"too many attributes (limit {MAX_STATE_ATTRIBUTES})", 400)
        if not all(valid_attribute(a) for a in attributes):
            return self.json_message("invalid attribute", 400)

        targets = self._measurements(settings, entity_ids)
        if not targets:
            return self.json({})
        limit = int(settings.get(CONF_MAX_STATE_ROWS, DEFAULT_MAX_STATE_ROWS))
        queries = [q for e, m in targets for q in states_queries(e, m, days, attributes, limit, window)]
        payload = await self._query(settings, queries)
        if isinstance(payload, web.Response):
            return payload
        return self.json(parse_states(payload, [e for e, _ in targets], attributes, limit))
