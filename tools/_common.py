"""Shared bits of the migration tools: the integration's own query code and
two tiny HTTP clients. Standard library only."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import pathlib
import sys
import time
import types
import urllib.parse
import urllib.request

# query.py and vm.py are loaded by path: importing the package would pull in Home Assistant.
_DIR = pathlib.Path(__file__).parents[1] / "custom_components" / "influx_proxy"
_pkg = types.ModuleType("influx_proxy_tools")
_pkg.__path__ = [str(_DIR)]
sys.modules[_pkg.__name__] = _pkg
for _name in ("query", "vm"):
    _spec = importlib.util.spec_from_file_location(f"{_pkg.__name__}.{_name}", _DIR / f"{_name}.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _mod
    _spec.loader.exec_module(_mod)
q = sys.modules[f"{_pkg.__name__}.query"]
vm = sys.modules[f"{_pkg.__name__}.vm"]


def _headers(prefix: str, scheme: str) -> dict[str, str]:
    """Credentials from the environment, never from the command line (it ends up in shell history):
    {prefix}_TOKEN, or {prefix}_USERNAME (also {prefix}_USER) and {prefix}_PASSWORD - the names vmctl reads."""
    if os.environ.get(f"{prefix}_TOKEN"):
        return {"Authorization": f"{scheme} {os.environ[f'{prefix}_TOKEN']}"}
    user = os.environ.get(f"{prefix}_USERNAME") or os.environ.get(f"{prefix}_USER")
    if user:
        raw = f"{user}:{os.environ.get(f'{prefix}_PASSWORD', '')}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    return {}


class Influx:
    """InfluxQL over /query (1.x, 2.x v1 API, 3.x). INFLUX_USERNAME/INFLUX_PASSWORD or INFLUX_TOKEN."""

    def __init__(self, url: str, db: str) -> None:
        self.url, self.db = url.rstrip("/"), db
        self.headers = _headers("INFLUX", "Token")

    def query(self, statement: str) -> dict:
        params = urllib.parse.urlencode({"db": self.db, "epoch": "ms", "q": statement})
        req = urllib.request.Request(f"{self.url}/query?{params}", headers=self.headers)
        with urllib.request.urlopen(req, timeout=600) as r:
            payload = json.loads(r.read())
        if error := q.first_error(payload):
            raise SystemExit(f"InfluxDB: {error}")
        return payload

    def values(self, statement: str) -> list[list]:
        series = (self.query(statement)["results"][0].get("series") or [])
        return [row for s in series for row in s.get("values") or []]

    def series(self) -> list[tuple[str, str, str]]:
        """(measurement, domain, entity_id) of every series Home Assistant wrote."""
        out = []
        for measurement in [row[0] for row in self.values("SHOW MEASUREMENTS")]:
            for row in self.values(f"SHOW SERIES FROM {q.quote_identifier(measurement)}"):
                tags = dict(part.split("=", 1) for part in row[0].split(",")[1:] if "=" in part)
                if tags.get("domain") and tags.get("entity_id"):
                    out.append((measurement, tags["domain"].replace("\\ ", " "), tags["entity_id"].replace("\\ ", " ")))
        return sorted(set(out))


class VictoriaMetrics:
    """VM_USERNAME/VM_PASSWORD or VM_TOKEN (vmauth)."""

    def __init__(self, url: str, db: str) -> None:
        self.url, self.db = url.rstrip("/"), db
        self.headers = _headers("VM", "Bearer")

    def get(self, path: str, params) -> bytes:
        req = urllib.request.Request(f"{self.url}{path}?{urllib.parse.urlencode(params)}", headers=self.headers)
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.read()

    def post(self, path: str, text: str) -> None:
        req = urllib.request.Request(f"{self.url}{path}", data=text.encode(), headers=self.headers, method="POST")
        urllib.request.urlopen(req, timeout=600).read()

    def dictionary(self):
        return vm.parse_dictionary(json.loads(self.get("/api/v1/series", vm.dictionary_params(self.db))))

    def recent_text_writes(self, seconds: int = 180) -> int:
        """Series with dictionary entries or codes written in the last `seconds`: a running
        "Store text states" or a migration that has just finished."""
        params = [("match[]", f'{vm.CODE_METRIC}{{db={vm.quote_label(self.db)}}}'),
                  ("match[]", f'{vm.LABEL_METRIC}{{db={vm.quote_label(self.db)}}}'),
                  ("start", str(int(time.time()) - seconds))]
        return sum(1 for line in self.get("/api/v1/export", params).splitlines() if line.strip())

    def last_before(self, entity_id: str, measurement: str, start_ms: int) -> int | None:
        payload = json.loads(self.get("/api/v1/query", vm.last_before_params(entity_id, measurement, start_ms, self.db)))
        return vm.parse_last_before(payload)
