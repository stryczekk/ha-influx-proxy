"""Constants for InfluxDB Proxy."""

DOMAIN = "influx_proxy"

CONF_DATABASE = "database"
CONF_AUTH = "auth"
CONF_TOKEN = "token"
CONF_MEASUREMENT_MODE = "measurement_mode"
CONF_DEFAULT_MEASUREMENT = "default_measurement"
CONF_OVERRIDE_MEASUREMENT = "override_measurement"
CONF_MAX_ENTITIES = "max_entities"
CONF_MAX_DAYS = "max_days"
CONF_MAX_STATE_ROWS = "max_state_rows"

AUTH_NONE = "none"
AUTH_BASIC = "basic"
AUTH_TOKEN = "token"

DEFAULT_DATABASE = "homeassistant"
DEFAULT_MAX_ENTITIES = 12
DEFAULT_MAX_DAYS = 800

API_PATH = "/api/influx_proxy/series"
API_STATES_PATH = "/api/influx_proxy/states"

# /states returns raw rows, so it takes more entities (a room of lamps) and
# caps the rows per entity instead (configurable). A busy presence sensor
# writes ~700 rows a day: half a year (a range plus the one before it, to
# compare) is ~130 000 rows - the default has to leave room for that.
STATES_MAX_ENTITIES = 40
DEFAULT_MAX_STATE_ROWS = 250000
