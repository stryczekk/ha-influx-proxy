"""Minimal InfluxDB client for the InfluxQL `/query` endpoint.

`/query` exists in InfluxDB 1.x, in 2.x (v1 compatibility API, needs a
DBRP mapping) and in 3.x, so one code path covers all three.
Credentials go in the Authorization header, never in the URL, so they do
not end up in access logs of anything in between.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import orjson  # ships with Home Assistant; several times faster than json, so the GIL is held for less time

from .const import (
    AUTH_BASIC,
    AUTH_TOKEN,
    BACKEND_VICTORIAMETRICS,
    CONF_AUTH,
    CONF_BACKEND,
    CONF_DATABASE,
    CONF_TOKEN,
)
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
) -> bytes:
    """Run one or more `;`-separated InfluxQL statements, return the raw body.

    The body can be megabytes: decode it with `decode()` in an executor job,
    not on the event loop.
    """
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
            return await response.read()
    except aiohttp.ClientError as err:
        raise InfluxError("cannot_connect", str(err)) from err
    except TimeoutError as err:
        raise InfluxError("cannot_connect", "timeout") from err
    except ValueError as err:  # e.g. aiohttp refusing credentials both in URL and header
        raise InfluxError("invalid_url", str(err)) from err


def decode(body: bytes) -> dict[str, Any]:
    """Parse and check a `/query` response body. Blocking for large bodies:
    call it from an executor job."""
    try:
        payload = orjson.loads(body)
    except ValueError as err:  # orjson.JSONDecodeError is a ValueError  # a login page or a web UI instead of InfluxDB
        raise InfluxError("not_influxdb", str(err)) from err
    if not isinstance(payload, dict):
        raise InfluxError("not_influxdb", "response is not a JSON object")
    error = first_error(payload)
    if error:
        kind = "database_not_found" if "database not found" in error else "query_failed"
        raise InfluxError(kind, error)
    return payload


async def test_connection(session: aiohttp.ClientSession, settings: dict[str, Any]) -> None:
    """Cheap query that proves URL, credentials and database all work."""
    if settings.get(CONF_BACKEND) == BACKEND_VICTORIAMETRICS:
        payload = vm_decode(await vm_get(session, settings, "/api/v1/label/db/values", []))
        database = settings.get(CONF_DATABASE) or ""
        if database and database not in (payload.get("data") or []):
            raise InfluxError("database_not_found", database)
        return
    decode(await run_query(session, settings, "SHOW MEASUREMENTS LIMIT 1"))  # a few bytes


async def vm_get(
    session: aiohttp.ClientSession, settings: dict[str, Any], path: str, params: list[tuple[str, str]] | dict[str, str]
) -> bytes:
    """GET a VictoriaMetrics endpoint, return the raw body (JSON or, for
    /api/v1/export, one JSON object per line)."""
    auth, headers = _auth(settings)
    if "Authorization" in headers:  # vmauth and VictoriaMetrics' own -httpAuth take a bearer token
        headers = {"Authorization": "Bearer " + headers["Authorization"].split(" ", 1)[1]}
    try:
        async with session.get(
            settings["url"].rstrip("/") + path,
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
            return await response.read()
    except aiohttp.ClientError as err:
        raise InfluxError("cannot_connect", str(err)) from err
    except TimeoutError as err:
        raise InfluxError("cannot_connect", "timeout") from err
    except ValueError as err:
        raise InfluxError("invalid_url", str(err)) from err


async def vm_post(session: aiohttp.ClientSession, settings: dict[str, Any], path: str, text: str) -> None:
    """POST lines to a VictoriaMetrics import endpoint."""
    auth, headers = _auth(settings)
    if "Authorization" in headers:
        headers = {"Authorization": "Bearer " + headers["Authorization"].split(" ", 1)[1]}
    try:
        async with session.post(
            settings["url"].rstrip("/") + path,
            data=text.encode(),
            auth=auth,
            headers={**headers, "Content-Type": "text/plain"},
            timeout=TIMEOUT,
            ssl=settings.get("verify_ssl", True),
        ) as response:
            if response.status in (401, 403):
                raise InfluxError("invalid_auth")
            if response.status not in (200, 204):
                body = (await response.text())[:200]
                raise InfluxError("write_failed", f"HTTP {response.status}: {body}")
    except aiohttp.ClientError as err:
        raise InfluxError("cannot_connect", str(err)) from err
    except TimeoutError as err:
        raise InfluxError("cannot_connect", "timeout") from err
    except ValueError as err:  # e.g. aiohttp refusing credentials both in URL and header
        raise InfluxError("invalid_url", str(err)) from err


def vm_decode(body: bytes) -> dict[str, Any]:
    """Parse and check a VictoriaMetrics JSON answer (executor job for large bodies)."""
    try:
        payload = orjson.loads(body)
    except ValueError as err:
        raise InfluxError("not_influxdb", str(err)) from err
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise InfluxError("not_influxdb", "not a VictoriaMetrics answer")
    return payload
