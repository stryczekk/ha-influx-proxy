"""Minimal InfluxDB client for the InfluxQL `/query` endpoint.

`/query` exists in InfluxDB 1.x, in 2.x (v1 compatibility API, needs a
DBRP mapping) and in 3.x, so one code path covers all three.
Credentials go in the Authorization header, never in the URL, so they do
not end up in access logs of anything in between.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from .const import AUTH_BASIC, AUTH_TOKEN, CONF_AUTH, CONF_DATABASE, CONF_TOKEN
from .query import first_error

TIMEOUT = aiohttp.ClientTimeout(total=60)


class InfluxError(Exception):
    """InfluxDB answered with an error or could not be reached.

    `kind` is a short, safe category for the client and the config flow;
    the message may contain internal detail and belongs in the log only.
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


def _auth(settings: dict[str, Any]) -> tuple[aiohttp.BasicAuth | None, dict[str, str]]:
    if settings.get(CONF_AUTH) == AUTH_BASIC:
        return aiohttp.BasicAuth(settings.get("username", ""), settings.get("password", "")), {}
    if settings.get(CONF_AUTH) == AUTH_TOKEN:
        return None, {"Authorization": f"Token {settings.get(CONF_TOKEN, '')}"}
    return None, {}


async def run_query(
    session: aiohttp.ClientSession, settings: dict[str, Any], query: str
) -> dict[str, Any]:
    """Run one or more `;`-separated InfluxQL statements, return the JSON."""
    auth, headers = _auth(settings)
    params = {"db": settings[CONF_DATABASE], "epoch": "ms", "q": query}
    try:
        async with session.get(
            settings["url"].rstrip("/") + "/query",
            params=params,
            auth=auth,
            headers=headers,
            timeout=TIMEOUT,
            ssl=settings.get("verify_ssl", True),
        ) as response:
            if response.status in (401, 403):
                raise InfluxError("invalid_auth")
            if response.status != 200:
                body = (await response.text())[:200]
                raise InfluxError("query_failed", f"HTTP {response.status}: {body}")
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:  # a login page or a web UI instead of InfluxDB
                raise InfluxError("not_influxdb", str(err)) from err
    except aiohttp.ClientError as err:
        raise InfluxError("cannot_connect", str(err)) from err
    except TimeoutError as err:
        raise InfluxError("cannot_connect", "timeout") from err
    except ValueError as err:  # e.g. aiohttp refusing credentials both in URL and header
        raise InfluxError("invalid_url", str(err)) from err
    if not isinstance(payload, dict):
        raise InfluxError("not_influxdb", "response is not a JSON object")
    error = first_error(payload)
    if error:
        kind = "database_not_found" if "database not found" in error else "query_failed"
        raise InfluxError(kind, error)
    return payload


async def test_connection(session: aiohttp.ClientSession, settings: dict[str, Any]) -> None:
    """Cheap query that proves URL, credentials and database all work."""
    await run_query(session, settings, "SHOW MEASUREMENTS LIMIT 1")
