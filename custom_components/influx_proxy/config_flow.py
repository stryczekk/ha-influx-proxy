"""Config flow for InfluxDB Proxy."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import InfluxError, test_connection
from .const import (
    AUTH_BASIC,
    AUTH_NONE,
    AUTH_TOKEN,
    CONF_AUTH,
    CONF_DATABASE,
    CONF_DEFAULT_MEASUREMENT,
    CONF_MAX_DAYS,
    CONF_MAX_ENTITIES,
    CONF_MAX_STATE_ROWS,
    CONF_MEASUREMENT_MODE,
    CONF_OVERRIDE_MEASUREMENT,
    CONF_TOKEN,
    DEFAULT_DATABASE,
    DEFAULT_MAX_DAYS,
    DEFAULT_MAX_ENTITIES,
    DEFAULT_MAX_STATE_ROWS,
    DOMAIN,
)
from .query import MEASUREMENT_MODES, MEASUREMENT_UNIT

_LOGGER = logging.getLogger(__name__)

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

# Errors that point at the first form (URL, database) rather than credentials.
CONNECTION_ERRORS = {"cannot_connect", "database_not_found", "not_influxdb", "invalid_url"}


def _connection_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, "http://localhost:8086")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            vol.Required(CONF_DATABASE, default=defaults.get(CONF_DATABASE, DEFAULT_DATABASE)): str,
            vol.Required(CONF_AUTH, default=defaults.get(CONF_AUTH, AUTH_BASIC)): SelectSelector(
                SelectSelectorConfig(
                    options=[AUTH_NONE, AUTH_BASIC, AUTH_TOKEN],
                    translation_key=CONF_AUTH,
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Required(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): bool,
        }
    )


def _credentials_schema(auth: str) -> vol.Schema:
    if auth == AUTH_TOKEN:
        return vol.Schema({vol.Required(CONF_TOKEN): PASSWORD})
    return vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): PASSWORD})


def _url_error(url: str) -> str | None:
    """Credentials belong in their own step, never in the URL (they would end
    up in the entry title and in logs, and aiohttp refuses URL credentials
    together with an Authorization header)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "invalid_url"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "invalid_url"
    if parts.username or parts.password:
        return "url_credentials"
    return None


class InfluxProxyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two steps: where the database is, then credentials if needed."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            url_error = _url_error(user_input[CONF_URL])
            if url_error:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_connection_schema(user_input),
                    errors={CONF_URL: url_error},
                )
            self._data = user_input
            if user_input[CONF_AUTH] == AUTH_NONE:
                return await self._async_validate_and_create({})
            return await self.async_step_credentials()
        return self.async_show_form(step_id="user", data_schema=_connection_schema({}))

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            return await self._async_validate_and_create(user_input)
        return self.async_show_form(
            step_id="credentials",
            data_schema=_credentials_schema(self._data[CONF_AUTH]),
            errors=errors,
        )

    async def _async_validate_and_create(self, credentials: dict[str, Any]) -> ConfigFlowResult:
        data = {**self._data, **credentials}
        try:
            await test_connection(async_get_clientsession(self.hass, data[CONF_VERIFY_SSL]), data)
        except InfluxError as err:
            _LOGGER.debug("Connection test failed: %s", err)
            # URL/database problems go back to the first form, where they can
            # be corrected; a wrong password stays on the credentials form.
            if err.kind == "invalid_auth" and data[CONF_AUTH] != AUTH_NONE:
                return self.async_show_form(
                    step_id="credentials",
                    data_schema=_credentials_schema(data[CONF_AUTH]),
                    errors={"base": "invalid_auth"},
                )
            key = err.kind if err.kind in CONNECTION_ERRORS | {"invalid_auth"} else "unknown"
            return self.async_show_form(
                step_id="user", data_schema=_connection_schema(data), errors={"base": key}
            )
        except Exception:  # noqa: BLE001 - "unknown" in the UI promises a log entry
            _LOGGER.exception("Unexpected error while testing the InfluxDB connection")
            return self.async_show_form(
                step_id="user", data_schema=_connection_schema(data), errors={"base": "unknown"}
            )
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=data[CONF_URL], data=data)

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Legacy `influx_proxy:` YAML configuration (before the config flow).

        The YAML version fell back to the measurement `state` for entities without a unit,
        so the import keeps that behaviour to show the same data as before.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        has_user = bool(import_data.get(CONF_USERNAME))
        return self.async_create_entry(
            title=import_data[CONF_URL],
            data={
                CONF_URL: import_data[CONF_URL],
                CONF_DATABASE: import_data.get(CONF_DATABASE, DEFAULT_DATABASE),
                CONF_AUTH: AUTH_BASIC if has_user else AUTH_NONE,
                CONF_USERNAME: import_data.get(CONF_USERNAME, ""),
                CONF_PASSWORD: import_data.get(CONF_PASSWORD, ""),
                CONF_VERIFY_SSL: True,
            },
            options={
                CONF_MEASUREMENT_MODE: MEASUREMENT_UNIT,
                CONF_DEFAULT_MEASUREMENT: "state",
                CONF_OVERRIDE_MEASUREMENT: "",
                CONF_MAX_ENTITIES: import_data.get(CONF_MAX_ENTITIES, DEFAULT_MAX_ENTITIES),
                CONF_MAX_DAYS: import_data.get(CONF_MAX_DAYS, DEFAULT_MAX_DAYS),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return InfluxProxyOptionsFlow()


class InfluxProxyOptionsFlow(OptionsFlow):
    """How HA names measurements, plus request limits."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        o = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MEASUREMENT_MODE, default=o.get(CONF_MEASUREMENT_MODE, MEASUREMENT_UNIT)
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=list(MEASUREMENT_MODES),
                            translation_key=CONF_MEASUREMENT_MODE,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_DEFAULT_MEASUREMENT, default=o.get(CONF_DEFAULT_MEASUREMENT, "")
                    ): str,
                    vol.Optional(
                        CONF_OVERRIDE_MEASUREMENT, default=o.get(CONF_OVERRIDE_MEASUREMENT, "")
                    ): str,
                    vol.Required(
                        CONF_MAX_ENTITIES, default=o.get(CONF_MAX_ENTITIES, DEFAULT_MAX_ENTITIES)
                    ): NumberSelector(NumberSelectorConfig(min=1, max=50, mode=NumberSelectorMode.BOX)),
                    vol.Required(
                        CONF_MAX_DAYS, default=o.get(CONF_MAX_DAYS, DEFAULT_MAX_DAYS)
                    ): NumberSelector(NumberSelectorConfig(min=1, max=3650, mode=NumberSelectorMode.BOX)),
                    vol.Required(
                        CONF_MAX_STATE_ROWS, default=o.get(CONF_MAX_STATE_ROWS, DEFAULT_MAX_STATE_ROWS)
                    ): NumberSelector(
                        NumberSelectorConfig(min=1000, max=5000000, step=1000, mode=NumberSelectorMode.BOX)
                    ),
                }
            ),
        )
