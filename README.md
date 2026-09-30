# InfluxDB Proxy for Home Assistant

[![hacs][hacs-badge]][hacs] [![Validate][validate-badge]][validate] [![Buy Me a Coffee][bmc-badge]][bmc]

Long-term history from **InfluxDB** for your Lovelace cards — served **by
Home Assistant itself**, so it works everywhere your dashboard works.

```
GET /api/influx_proxy/series?entities=sensor.living_room_temperature,sensor.outside_temperature&days=30
```

## Why

Home Assistant keeps detailed history for about 10 days. Many people also
write everything to InfluxDB — and then find out that getting that data
back **into a dashboard** is surprisingly awkward:

- **A card runs in your browser.** Querying InfluxDB directly only works on
  your home network. Open the dashboard through a remote-access tunnel or
  Nabu Casa and the browser cannot reach a separate database host — the
  charts are empty exactly when you are away from home.
- **Credentials end up in the dashboard.** A card that talks to InfluxDB
  needs a password in its configuration, visible to every admin.
- **CORS**, mixed content (HTTPS dashboard, HTTP database), and so on.

This integration adds one endpoint **inside** Home Assistant. Cards call
it with your normal Home Assistant session; Home Assistant queries
InfluxDB on the server side.

- ✅ works locally, through tunnels and through Nabu Casa
- ✅ the database password never leaves the server
- ✅ protected by Home Assistant authentication (401 without a session)
- ✅ **no arbitrary InfluxQL from the browser** — queries are built on the
  server from entity ids and a time range, with proper InfluxQL escaping
- ✅ response in the same shape as Home Assistant long-term statistics
  (`start`, `end`, `mean`, `min`, `max`), so a card can treat both sources
  the same way

> This is a building block for **custom cards**. It does not draw charts on
> its own — see [Using it from a card](#using-it-from-a-card).

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open this repository inside HACS.][hacs-open-badge]][hacs-open]

1. Click the button above — it adds this repository to HACS in your Home
   Assistant. Or manually: HACS → ⋮ → **Custom repositories** → add
   `https://github.com/stryczekk/ha-influx-proxy`, category **Integration**
2. Install **InfluxDB Proxy**, restart Home Assistant
3. Add the integration:

   [![Open your Home Assistant instance and start setting up InfluxDB Proxy.][config-flow-badge]][config-flow]

   or **Settings → Devices & services → Add integration → InfluxDB Proxy**

### Manual

Copy `custom_components/influx_proxy` into your `config/custom_components/`,
restart, then add the integration as above.

## Configuration

| Field | Notes |
|---|---|
| URL | e.g. `http://192.168.1.10:8086` |
| Database | InfluxDB 2.x/3.x: the database name mapped to your bucket |
| Authentication | none, username + password, or token (InfluxDB 2.x/3.x) |
| Verify SSL | turn off only for self-signed certificates |

**Use a read-only account.** The proxy only ever reads.

### Measurement naming — important

Home Assistant's `influxdb` integration can name measurements in different
ways. The proxy must look where your data actually is, so under
**Configure** set the same options as in the integration that **writes**
your data:

| Option | Matches `influxdb` setting | Default |
|---|---|---|
| Measurement named after | `measurement_attr` | unit of measurement |
| Default measurement | `default_measurement` | *(empty = entity_id)* |
| Override measurement | `override_measurement` | *(empty)* |

In *domain and device class* mode an entity without a device class is read
from a measurement named after its domain alone (e.g. `sensor`) — exactly
what the `influxdb` integration writes. Per-entity `override_measurement`
from its `component_config` cannot be mirrored; use the global override.

Each query filters on both tags Home Assistant writes — `domain` and
`entity_id` — so `sensor.x` and `number.x` are never merged, even when they
share a measurement such as `°C`.

## API

`GET /api/influx_proxy/series`

| Parameter | Meaning |
|---|---|
| `entities` | comma-separated entity ids (limit configurable, default 12) |
| `days` | time range, may be fractional (`0.5` = 12 h); limit default 800 |

Aggregation step is chosen automatically: 5 min for a day, 30 min for a
week, 2 h for a month, 6 h up to ~4 months, 1 day beyond.

Response:

```json
{
  "sensor.living_room_temperature": [
    {"start": 1790000000000, "end": 1790000000000, "mean": 21.3, "min": 21.1, "max": 21.6}
  ]
}
```

Entities without data in the range are omitted; repeated entity ids are
merged. Errors come back as `{"message": "..."}`:

| Status | Meaning |
|---|---|
| 400 | bad request (parameters, limits) |
| 401 | no Home Assistant session |
| 403 | the user may not read one of the entities |
| 404 | the integration was never set up (not loaded) |
| 502 | InfluxDB failed — only a category is returned (`cannot_connect`, `invalid_auth`, `query_failed`, …); host, port and the database's error text go to the Home Assistant log |
| 503 | not configured, or briefly while the entry reloads after an options change |

At most four queries run against InfluxDB at the same time; further
requests wait.

## Using it from a card

Inside a custom card, `hass.callApi` adds your session automatically:

```js
const data = await this.hass.callApi(
  "GET",
  "influx_proxy/series?entities=" + encodeURIComponent("sensor.a,sensor.b") + "&days=30"
);
```

## Compatibility

| InfluxDB | Status | Auth used in the test |
|---|---|---|
| 1.8.10 | ✅ tested | none |
| 2.7.12 | ✅ tested — v1 compatibility API; each bucket is automatically usable as a database of the same name | token |
| 3 Core 3.11.5 | ✅ tested — InfluxQL over `/query` | none (`--without-auth`) |

The test (`tests/compat/run.py`) writes points shaped exactly like Home
Assistant's `influxdb` integration writes them and reads them back with the
integration's own query code: series found, `number.*` with the same object
id not mixed in, all values present, default measurement for entities
without a unit, injection attempt harmless, config-flow connection test
query working.

## Tests

| Suite | What it covers | Needs |
|---|---|---|
| `tests/test_query.py` | query building, InfluxQL escaping, measurement naming, result parsing | nothing (`python -m unittest discover -s tests`) |
| `tests/compat/run.py` | the integration's queries against a **real InfluxDB** 1.8 / 2.7 / 3 Core | an InfluxDB |
| `tests/e2e/flow.py` | **config flow as the UI drives it** (fields, credentials in URL, `invalid_url`, `invalid_auth`, `cannot_connect`, single instance), options flow and reload, endpoint validation, limits, duplicates, 401 without session, 503 after removal | a **test** Home Assistant + InfluxDB, `E2E_TEST_INSTANCE=1` (it deletes entries) |
| `tests/e2e/yaml_import.py` | legacy YAML import, no duplicate on restart, repair issue shown and withdrawn, cleanup | a **test** Home Assistant, `E2E_TEST_INSTANCE=1` (it edits `configuration.yaml` and restarts) |

## Migrating from YAML configuration

A `influx_proxy:` section in `configuration.yaml` is imported
automatically on the next restart, with the same measurement behaviour as
before (`default_measurement: state`). A repair notice then asks you to
remove the YAML section.

## Support

Everything here is free and will stay free. If it saved you an evening of
tinkering, you can [buy me a coffee][bmc] ☕ — thank you!

## License

MIT

[hacs]: https://hacs.xyz
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[validate]: https://github.com/stryczekk/ha-influx-proxy/actions/workflows/validate.yml
[validate-badge]: https://github.com/stryczekk/ha-influx-proxy/actions/workflows/validate.yml/badge.svg
[bmc]: https://buymeacoffee.com/stryczekk
[hacs-open]: https://my.home-assistant.io/redirect/hacs_repository/?owner=stryczekk&repository=ha-influx-proxy&category=integration
[hacs-open-badge]: https://my.home-assistant.io/badges/hacs_repository.svg
[config-flow]: https://my.home-assistant.io/redirect/config_flow_start/?domain=influx_proxy
[config-flow-badge]: https://my.home-assistant.io/badges/config_flow_start.svg
[bmc-badge]: https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-FFDD00?logo=buymeacoffee&logoColor=black
