"""Constants for the Homeplanarr integration."""

from datetime import timedelta

DOMAIN = "homeplanarr"
DEFAULT_URL = "https://homeplanarr.com"

CONF_ENERGY_ENTITIES = "energy_entities"
CONF_ENERGY_INTERVAL = "energy_interval"
CONF_WATER_ENTITIES = "water_entities"
CONF_SOLAR_ENTITIES = "solar_entities"
CONF_LEVEL_ENTITIES = "level_entities"
CONF_BATTERY_ENTITIES = "battery_entities"
CONF_FORWARD_TAGS = "forward_tags"

DEFAULT_ENERGY_INTERVAL = "15"
ENERGY_INTERVALS = ["15", "60"]

# The app's own summary for HA; buildSummary is not free, so poll gently.
SCAN_INTERVAL = timedelta(minutes=5)
# The shopping list is read from Homeplanarr this often, so items added or
# ticked off in the app show up in Home Assistant. One cheap GET.
SHOPPING_SCAN_INTERVAL = timedelta(seconds=60)

# Readings that could not be sent are kept and resent with the next push,
# but not forever: a day of quarter hours.
MAX_PENDING = 96
# A gap longer than this (HA down) is dropped rather than spread over hours
# nobody can price honestly.
MAX_CATCHUP = timedelta(hours=24)

SERVICE_COMPLETE_CHORE = "complete_chore"
