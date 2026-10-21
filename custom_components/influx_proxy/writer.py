"""Text states to VictoriaMetrics, which stores numbers only.

Home Assistant's `influxdb` integration keeps writing everything else; this
writes just what VictoriaMetrics would lose: every non-numeric state as a
code from a dictionary that lives in VictoriaMetrics as well (vm.py). The
proxy reads both back.

- Only entities the `influxdb` integration writes: its include/exclude from
  `configuration.yaml` applies here too (an excluded `person.*` stays out).
- Guards against free text (a media title, a status message): states longer
  than 100 characters, dates and times, more than MAX_ENTITY_LABELS
  different states of one entity, or more than MAX_LABELS in the
  dictionary are not written. What an entity has used, and which entities
  were found to be free text, is kept across restarts.
- Codes never change: nothing is written before the dictionary is read, and
  a copy of it is kept in .storage.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config as conf_util
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entityfilter import INCLUDE_EXCLUDE_FILTER_SCHEMA, EntityFilter
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from . import vm
from .client import InfluxError, vm_decode, vm_get, vm_post
from .const import CONF_DATABASE, DOMAIN

_LOGGER = logging.getLogger(__name__)

FLUSH_INTERVAL = timedelta(seconds=5)
RETRY_INTERVAL = timedelta(minutes=1)
REFRESH_INTERVAL = timedelta(days=1)
MAX_ENTITY_LABELS = 64
MAX_LABELS = 5000
# lines kept while VictoriaMetrics is unreachable (about a day of a busy home)
MAX_PENDING = 50000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
STORE_VERSION = 1
STORE_SAVE_DELAY = 10


def _ms(moment: datetime) -> int:
    """Epoch milliseconds, cut the way VictoriaMetrics cuts the nanoseconds
    of the `influxdb` integration's points - so both land on the same ms."""
    return (moment - _EPOCH) // timedelta(milliseconds=1)


def _numeric(state: str) -> bool:
    try:
        float(state)
    except ValueError:
        return False
    return True


async def _influxdb_filter(hass: HomeAssistant) -> EntityFilter:
    """The include/exclude of the `influxdb` integration (configuration.yaml)."""
    yaml = (await conf_util.async_hass_config_yaml(hass)).get("influxdb") or {}
    return INCLUDE_EXCLUDE_FILTER_SCHEMA({key: yaml[key] for key in ("include", "exclude") if key in yaml})


class StateWriter:
    """Writes non-numeric states as dictionary codes, in batches."""

    def __init__(self, hass: HomeAssistant, settings: dict[str, Any]) -> None:
        self._hass = hass
        self._settings = settings
        self._db = settings.get(CONF_DATABASE) or ""
        self._codes: dict[str, int] | None = None  # None until loaded: never assign codes blind
        self._taken: set[int] = set()  # every code in use, also ones shared by two labels
        self._filter: EntityFilter | None = None
        # What each entity has used and which ones are free text - kept in .storage with a copy of
        # the dictionary (VictoriaMetrics shows a new series only after a few seconds, so a label
        # added just before a restart could otherwise get a second code).
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.text_states")
        self._store_key = f"{settings.get('url', '').rstrip('/')}|{self._db}"
        self._stored: dict[str, Any] = {}
        self._entity_labels: dict[str, set[str]] = {}
        self._skipped: set[str] = set()
        self._pending: list[str] = []
        self._unsubs: list[Any] = []
        self._load_lock = asyncio.Lock()
        self._flushing = False
        self._failing = False
        self._warned_full = False

    @property
    def _session(self) -> aiohttp.ClientSession:
        return async_get_clientsession(self._hass, self._settings.get("verify_ssl", True))

    @callback
    def async_start(self) -> None:
        """Listen at once; read the filter and the dictionary in the background,
        so Home Assistant does not wait for VictoriaMetrics at startup."""
        self._unsubs.append(self._hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state))
        self._unsubs.append(self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop))
        self._unsubs.append(async_track_time_interval(self._hass, self._flush, FLUSH_INTERVAL))
        self._unsubs.append(async_track_time_interval(self._hass, self._load, RETRY_INTERVAL))
        self._unsubs.append(async_track_time_interval(self._hass, self._refresh, REFRESH_INTERVAL))
        self._hass.async_create_background_task(self._load(), f"{DOMAIN} text states dictionary")

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except ValueError:  # the stop listener after Home Assistant has stopped
                pass
        self._unsubs.clear()
        await self._on_stop(None)

    async def _on_stop(self, _event: Event | None) -> None:
        """Home Assistant does not unload entries when it stops: write what is waiting."""
        await self._flush()
        if self._codes is not None:
            await self._store.async_save(self._snapshot())

    async def _load(self, _now: datetime | None = None) -> None:
        """Read the filter and the dictionary once; until then states are not written."""
        async with self._load_lock:
            if self._codes is not None:
                return
            try:
                entity_filter = await _influxdb_filter(self._hass)
            except (HomeAssistantError, vol.Invalid) as err:
                # fail closed: without the filter, entities excluded for privacy could be written
                _LOGGER.error("Text states: cannot read the influxdb include/exclude, nothing is written: %s", err)
                return
            try:
                body = await vm_get(self._session, self._settings, "/api/v1/series", vm.dictionary_params(self._db))
                remote = vm.parse_dictionary(vm_decode(body))
            except InfluxError as err:
                if not self._failing:
                    _LOGGER.warning("Text states: cannot read the dictionary yet, retrying every minute: %s", err)
                else:
                    _LOGGER.debug("Text states: dictionary still unreadable: %s", err)
                self._failing = True
                return
            self._stored = await self._store.async_load() or {}
            local = self._stored.get(self._store_key) or {}
            local_codes: dict[str, int] = local.get("codes") or {}
            if remote.ambiguous:
                _LOGGER.error(
                    "Text states: codes %s stand for more than one state (two writers on one database?)",
                    remote.ambiguous,
                )
            taken = set(remote.labels) | set(remote.ambiguous)
            clashes = {label: code for label, code in local_codes.items()
                       if code in remote.labels and remote.labels[code] != label}
            if clashes:
                _LOGGER.error("Text states: codes written here were given to other states meanwhile: %s", clashes)
            # Labels known only here may not have reached VictoriaMetrics: keep them and write them
            # again - unless their code is taken there meanwhile (then they get a new one when seen).
            only_local = {label: code for label, code in local_codes.items()
                          if label not in remote.codes and code not in taken}
            codes = {**only_local, **remote.codes}
            now = _ms(datetime.now(UTC))
            self._pending.extend(vm.label_line(label, code, now, self._db) for label, code in only_local.items())
            self._taken = taken | set(codes.values())
            self._entity_labels = {e: set(labels) for e, labels in (local.get("entities") or {}).items()}
            self._skipped = set(local.get("skipped") or [])
            self._filter = entity_filter
            self._codes = codes
            self._failing = False
            self._save()
            _LOGGER.debug("Text states: dictionary with %s labels loaded", len(codes))

    def _snapshot(self) -> dict[str, Any]:
        self._stored[self._store_key] = {
            "codes": dict(self._codes or {}),
            "entities": {e: sorted(labels) for e, labels in self._entity_labels.items()},
            "skipped": sorted(self._skipped),
        }
        return self._stored

    def _save(self) -> None:
        self._store.async_delay_save(self._snapshot, STORE_SAVE_DELAY)

    @callback
    def _queue_dictionary(self) -> None:
        if self._codes:
            now = _ms(datetime.now(UTC))
            self._pending.extend(vm.label_line(label, code, now, self._db) for label, code in self._codes.items())

    async def _refresh(self, _now: datetime | None = None) -> None:
        """Write the whole dictionary again, so no label ages out of the retention period."""
        self._queue_dictionary()

    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if self._codes is None or self._filter is None or new is None:
            return
        state = new.state
        if state in (STATE_UNKNOWN, STATE_UNAVAILABLE, "") or _numeric(state):
            return  # the `influxdb` integration skips these too, or writes the number itself
        entity_id = new.entity_id
        if entity_id in self._skipped or not self._filter(entity_id):
            return
        seen = self._entity_labels.setdefault(entity_id, set())
        if not vm.is_state_label(state) or (state not in seen and len(seen) >= MAX_ENTITY_LABELS):
            self._skipped.add(entity_id)
            self._save()
            _LOGGER.info("Text states: %s looks like free text, its states are not written", entity_id)
            return
        code = self._codes.get(state)
        if code is None:
            if len(self._codes) >= MAX_LABELS:
                if not self._warned_full:
                    self._warned_full = True
                    _LOGGER.warning("Text states: dictionary full (%s labels), new states are not written", MAX_LABELS)
                return
            code = max(self._taken, default=0) + 1
            self._codes[state] = code
            self._taken.add(code)
            # in the same batch as its first use: both are stored, or neither
            self._pending.append(vm.label_line(state, code, _ms(event.time_fired), self._db))
        if state not in seen:
            seen.add(state)
            self._save()
        self._pending.append(vm.code_line(entity_id, code, _ms(event.time_fired), self._db))

    async def _flush(self, _now: datetime | None = None) -> None:
        if not self._pending or self._flushing:
            return
        self._flushing = True
        lines, self._pending = self._pending, []
        written = False
        try:
            await vm_post(self._session, self._settings, "/api/v1/import/prometheus", "\n".join(lines) + "\n")
            written = True
        except InfluxError as err:
            if not self._failing:
                _LOGGER.warning("Text states: write failed, keeping changes and retrying: %s", err)
            self._failing = True
        finally:
            self._flushing = False
            if not written:  # also when cancelled at unload: nothing is dropped silently
                self._pending = lines + self._pending
                if len(self._pending) > MAX_PENDING:
                    # the oldest changes go; the dictionary must not (a lost label would free its code)
                    self._pending = self._pending[-MAX_PENDING:]
                    self._queue_dictionary()
        if written and self._failing:
            self._failing = False
            _LOGGER.info("Text states: writing again")
