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

AUTH_NONE = "none"
AUTH_BASIC = "basic"
AUTH_TOKEN = "token"

DEFAULT_DATABASE = "homeassistant"
DEFAULT_MAX_ENTITIES = 12
DEFAULT_MAX_DAYS = 800

API_PATH = "/api/influx_proxy/series"
