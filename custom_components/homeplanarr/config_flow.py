"""Config flow: instance URL + household API key, then which entities to send."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HomeplanarrApi, HomeplanarrAuthError, HomeplanarrBillingError, HomeplanarrError
from .const import (
    CONF_BATTERY_ENTITIES,
    CONF_MEMBERS,
    CONF_ENERGY_ENTITIES,
    CONF_ENERGY_INTERVAL,
    CONF_FORWARD_TAGS,
    CONF_LEVEL_ENTITIES,
    CONF_ODOMETER_ENTITIES,
    CONF_ODOMETERS,
    CONF_SHOPPING_LIST,
    CONF_SOLAR_ENTITIES,
    CONF_WATER_ENTITIES,
    DEFAULT_ENERGY_INTERVAL,
    DEFAULT_URL,
    DOMAIN,
    ENERGY_INTERVALS,
)
from .mapping import (
    ha_tags,
    ha_users,
    linked_chores,
    members_from_input,
    members_schema,
    save_tags,
    tags_schema,
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
            vol.Optional(CONF_ODOMETER_ENTITIES, **default(CONF_ODOMETER_ENTITIES, [])): _entities(
                "distance"
            ),
            vol.Optional(
                CONF_FORWARD_TAGS, **default(CONF_FORWARD_TAGS, False)
            ): selector.BooleanSelector(),
            # suggested, not default: a default comes back when the field is cleared
            vol.Optional(
                CONF_SHOPPING_LIST,
                description={"suggested_value": current.get(CONF_SHOPPING_LIST)},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="todo")),
        }
    )


def entities_errors(hass: HomeAssistant, user_input: Mapping[str, Any]) -> dict[str, str]:
    target = user_input.get(CONF_SHOPPING_LIST)
    if target:
        entity = er.async_get(hass).async_get(target)
        if entity is not None and entity.platform == DOMAIN:
            return {CONF_SHOPPING_LIST: "own_list"}
    return {}


def vehicles_schema(
    entities: list[str], vehicles: list[dict[str, Any]], current: Mapping[str, Any]
) -> vol.Schema:
    """One vehicle picker per odometer sensor, keyed by the sensor's entity id."""
    options = [
        selector.SelectOptionDict(value=str(v["id"]), label=str(v["name"])) for v in vehicles
    ]
    ids = {o["value"] for o in options}
    fields: dict[Any, Any] = {}
    for entity_id in entities:
        picked = str(current.get(entity_id) or "")
        fields[
            vol.Required(entity_id, default=picked if picked in ids else options[0]["value"])
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=options, mode=selector.SelectSelectorMode.DROPDOWN
            )
        )
    return vol.Schema(fields)


async def fetch_vehicles(
    hass: HomeAssistant, url: str, api_key: str
) -> tuple[list[dict[str, Any]], str | None]:
    """The household's vehicles, or an error key for the odometer field."""
    api = HomeplanarrApi(async_get_clientsession(hass), url, api_key)
    try:
        vehicles = await api.vehicles()
    except HomeplanarrError:
        return [], "cannot_connect"
    if not vehicles:
        return [], "no_vehicles"
    return vehicles, None


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
    url = url.removesuffix("/app")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


class _Steps:
    """The steps after "What to send", shared by setup and Configure: pair odometers
    with vehicles, HA's tags with chores and HA's people with members."""

    hass: HomeAssistant

    def _init_steps(self) -> None:
        self._options: dict[str, Any] = {}
        self._vehicles: list[dict[str, Any]] = []
        self._chores: list[dict[str, Any]] = []
        self._members: list[dict[str, Any]] = []
        self._remote_tags: list[dict[str, Any]] = []
        self._tags: dict[str, str] = {}
        self._users: dict[str, str] = {}

    def _connection(self) -> tuple[str, str]:
        raise NotImplementedError

    def _current(self) -> Mapping[str, Any]:
        """The options saved before this flow (empty during setup)."""
        return {}

    def _finish(self) -> ConfigFlowResult:
        raise NotImplementedError

    def _api(self) -> HomeplanarrApi:
        url, api_key = self._connection()
        return HomeplanarrApi(async_get_clientsession(self.hass), url, api_key)

    async def _accept_entities(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Fetch what the next steps need; an error key per field, or none."""
        errors = entities_errors(self.hass, user_input)
        if errors:
            return errors
        if user_input.get(CONF_ODOMETER_ENTITIES):
            url, api_key = self._connection()
            self._vehicles, error = await fetch_vehicles(self.hass, url, api_key)
            if error:
                return {CONF_ODOMETER_ENTITIES: error}
        if user_input.get(CONF_FORWARD_TAGS):
            api = self._api()
            try:
                self._chores = await api.chores()
                self._members = await api.members()
                self._remote_tags = await api.nfc_tags()
            except HomeplanarrError:
                return {CONF_FORWARD_TAGS: "cannot_connect"}
            self._tags = ha_tags(self.hass)
            self._users = await ha_users(self.hass)
        self._options = dict(user_input)
        if CONF_MEMBERS in self._current():
            self._options[CONF_MEMBERS] = dict(self._current()[CONF_MEMBERS])
        return {}

    async def _next(self, after: str | None = None) -> ConfigFlowResult:
        steps = ["vehicles", "tags", "members"]
        for step in steps[steps.index(after) + 1 if after else 0 :]:
            if step == "vehicles" and self._options.get(CONF_ODOMETER_ENTITIES):
                return await self.async_step_vehicles()
            if not self._options.get(CONF_FORWARD_TAGS):
                continue
            if step == "tags" and self._tags and self._chores:
                return await self.async_step_tags()
            if step == "members" and self._users and self._members:
                return await self.async_step_members()
        return self._finish()

    async def async_step_vehicles(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._options[CONF_ODOMETERS] = dict(user_input)
            return await self._next("vehicles")
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="vehicles",
            data_schema=vehicles_schema(
                self._options[CONF_ODOMETER_ENTITIES],
                self._vehicles,
                self._current().get(CONF_ODOMETERS) or {},
            ),
        )

    async def async_step_tags(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await save_tags(self._api(), self._tags, user_input, self._remote_tags)
            except HomeplanarrError:
                errors["base"] = "cannot_connect"
            else:
                return await self._next("tags")
        preset = (
            linked_chores(self._remote_tags)
            if user_input is None
            else {self._tags[k]: v for k, v in user_input.items() if k in self._tags}
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="tags",
            data_schema=tags_schema(self._tags, self._chores, preset),
            errors=errors,
        )

    async def async_step_members(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._options[CONF_MEMBERS] = members_from_input(self._users, user_input)
            return await self._next("members")
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="members",
            data_schema=members_schema(
                self._users, self._members, self._options.get(CONF_MEMBERS) or {}
            ),
        )


class HomeplanarrConfigFlow(_Steps, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._init_steps()

    def _connection(self) -> tuple[str, str]:
        return self._data[CONF_URL], self._data[CONF_API_KEY]

    def _finish(self) -> ConfigFlowResult:
        return self.async_create_entry(title="Homeplanarr", data=self._data, options=self._options)

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
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._accept_entities(user_input)
            if not errors:
                return await self._next()
        return self.async_show_form(
            step_id="entities", data_schema=entities_schema(user_input or {}), errors=errors
        )

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


class HomeplanarrOptionsFlow(_Steps, OptionsFlow):
    """Change which entities are sent, and the pairings."""

    def __init__(self) -> None:
        self._init_steps()

    def _connection(self) -> tuple[str, str]:
        data = self.config_entry.data
        return data[CONF_URL], data[CONF_API_KEY]

    def _current(self) -> Mapping[str, Any]:
        return self.config_entry.options

    def _finish(self) -> ConfigFlowResult:
        return self.async_create_entry(data=self._options)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._accept_entities(user_input)
            if not errors:
                return await self._next()
        return self.async_show_form(
            step_id="init",
            data_schema=entities_schema(user_input or self.config_entry.options),
            errors=errors,
        )
