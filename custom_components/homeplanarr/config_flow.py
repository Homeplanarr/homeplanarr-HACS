"""Config flow: instance URL + household API key, then which entities to send."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HomeplanarrApi, HomeplanarrAuthError, HomeplanarrBillingError, HomeplanarrError
from .const import (
    CONF_BATTERY_ENTITIES,
    CONF_ENERGY_ENTITIES,
    CONF_ENERGY_INTERVAL,
    CONF_FORWARD_TAGS,
    CONF_LEVEL_ENTITIES,
    CONF_SOLAR_ENTITIES,
    CONF_WATER_ENTITIES,
    DEFAULT_ENERGY_INTERVAL,
    DEFAULT_URL,
    DOMAIN,
    ENERGY_INTERVALS,
)


def _entities(device_class: str | list[str] | None = None) -> selector.EntitySelector:
    config: dict[str, Any] = {"domain": "sensor", "multiple": True}
    if device_class:
        config["device_class"] = device_class
    return selector.EntitySelector(selector.EntitySelectorConfig(**config))


def entities_schema(current: Mapping[str, Any]) -> vol.Schema:
    def default(key: str, fallback: Any) -> dict[str, Any]:
        return {"default": current.get(key, fallback)}

    return vol.Schema(
        {
            vol.Optional(CONF_ENERGY_ENTITIES, **default(CONF_ENERGY_ENTITIES, [])): _entities(
                "energy"
            ),
            vol.Required(
                CONF_ENERGY_INTERVAL, **default(CONF_ENERGY_INTERVAL, DEFAULT_ENERGY_INTERVAL)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=ENERGY_INTERVALS,
                    translation_key=CONF_ENERGY_INTERVAL,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional(CONF_WATER_ENTITIES, **default(CONF_WATER_ENTITIES, [])): _entities(
                "water"
            ),
            vol.Optional(CONF_SOLAR_ENTITIES, **default(CONF_SOLAR_ENTITIES, [])): _entities(
                "energy"
            ),
            vol.Optional(CONF_LEVEL_ENTITIES, **default(CONF_LEVEL_ENTITIES, [])): _entities(),
            vol.Optional(CONF_BATTERY_ENTITIES, **default(CONF_BATTERY_ENTITIES, [])): _entities(
                "battery"
            ),
            vol.Optional(
                CONF_FORWARD_TAGS, **default(CONF_FORWARD_TAGS, False)
            ): selector.BooleanSelector(),
        }
    )


async def _validate(hass, url: str, api_key: str) -> str | None:
    """Return an error key, or None when the key works."""
    api = HomeplanarrApi(async_get_clientsession(hass), url, api_key)
    try:
        await api.sensors()
    except HomeplanarrAuthError:
        return "invalid_auth"
    except HomeplanarrBillingError:
        return "no_subscription"
    except HomeplanarrError:
        return "cannot_connect"
    return None


def _normalise_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith("/app"):
        url = url[: -len("/app")]
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


class HomeplanarrConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = _normalise_url(user_input[CONF_URL])
            api_key = user_input[CONF_API_KEY].strip()
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
            error = await _validate(self.hass, url, api_key)
            if error is None:
                self._data = {CONF_URL: url, CONF_API_KEY: api_key}
                return await self.async_step_entities()
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=DEFAULT_URL): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                    ),
                    vol.Required(CONF_API_KEY): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Homeplanarr", data=self._data, options=user_input)
        return self.async_show_form(step_id="entities", data_schema=entities_schema({}))

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            error = await _validate(self.hass, entry.data[CONF_URL], api_key)
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HomeplanarrOptionsFlow()


class HomeplanarrOptionsFlow(OptionsFlow):
    """Change which entities are sent."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init", data_schema=entities_schema(self.config_entry.options)
        )
