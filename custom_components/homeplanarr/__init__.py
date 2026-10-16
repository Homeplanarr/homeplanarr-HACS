"""Homeplanarr: send Home Assistant's meters and sensors to your household, and
show the household's figures in Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_URL, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import HomeplanarrApi, HomeplanarrError
from .const import DOMAIN, SERVICE_COMPLETE_CHORE
from .coordinator import HomeplanarrCoordinator, ShoppingCoordinator
from .pusher import Pusher

PLATFORMS = [Platform.SENSOR, Platform.TODO]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ATTR_CHORE = "chore"
ATTR_MEMBER = "member"
ATTR_COMMENT = "comment"
ATTR_CONFIG_ENTRY = "config_entry_id"

COMPLETE_CHORE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CHORE): cv.string,
        vol.Required(ATTR_MEMBER): cv.string,
        vol.Optional(ATTR_COMMENT): cv.string,
        vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
    }
)


@dataclass
class HomeplanarrData:
    api: HomeplanarrApi
    coordinator: HomeplanarrCoordinator
    shopping: ShoppingCoordinator
    pusher: Pusher


type HomeplanarrConfigEntry = ConfigEntry[HomeplanarrData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def complete_chore(call: ServiceCall) -> ServiceResponse:
        entries = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.state is ConfigEntryState.LOADED
            and (ATTR_CONFIG_ENTRY not in call.data or e.entry_id == call.data[ATTR_CONFIG_ENTRY])
        ]
        if not entries:
            raise ServiceValidationError("Homeplanarr is not set up")
        entry: HomeplanarrConfigEntry = entries[0]
        try:
            result = await entry.runtime_data.api.chore_event(
                call.data[ATTR_CHORE],
                call.data[ATTR_MEMBER],
                call.data.get(ATTR_COMMENT) or "via Home Assistant",
            )
        except HomeplanarrError as err:
            raise HomeAssistantError(f"Homeplanarr: {err}") from err
        await entry.runtime_data.coordinator.async_request_refresh()
        return result if isinstance(result, dict) else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_COMPLETE_CHORE,
        complete_chore,
        schema=COMPLETE_CHORE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HomeplanarrConfigEntry) -> bool:
    api = HomeplanarrApi(
        async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_API_KEY]
    )
    coordinator = HomeplanarrCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    shopping = ShoppingCoordinator(hass, entry, api)
    await shopping.async_config_entry_first_refresh()
    pusher = Pusher(hass, entry, api)
    await pusher.async_start()
    entry.runtime_data = HomeplanarrData(api, coordinator, shopping, pusher)
    entry.async_on_unload(pusher.async_stop)
    entry.async_on_unload(entry.add_update_listener(_reload_on_change))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _reload_on_change(hass: HomeAssistant, entry: HomeplanarrConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HomeplanarrConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
