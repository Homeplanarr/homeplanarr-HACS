"""Polls Homeplanarr's Home Assistant summary."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HomeplanarrApi, HomeplanarrAuthError, HomeplanarrError
from .const import DOMAIN, SCAN_INTERVAL, SHOPPING_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class HomeplanarrCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches GET /integrations/home-assistant/sensors."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: HomeplanarrApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.api = api

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.api.sensors()
        except HomeplanarrAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HomeplanarrError as err:
            raise UpdateFailed(str(err)) from err


class ShoppingCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Fetches GET /shopping, the household's shopping list."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: HomeplanarrApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_shopping",
            update_interval=SHOPPING_SCAN_INTERVAL,
        )
        self.api = api

    async def _async_update_data(self) -> list[dict[str, Any]]:
        try:
            return await self.api.shopping_list()
        except HomeplanarrAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HomeplanarrError as err:
            raise UpdateFailed(str(err)) from err
