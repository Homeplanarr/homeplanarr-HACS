"""The shopping list synced with a to-do list the user already has."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.homeplanarr.const import CONF_SHOPPING_LIST

from .test_init import _setup

LIST = "todo.shopping"


class _MemoryStore:
    """Local To-do keeps its list in a file; the test keeps it in memory."""

    def __init__(self, hass, path) -> None:
        self._ics = ""

    async def async_load(self) -> str:
        return self._ics

    async def async_store(self, ics: str) -> None:
        self._ics = ics


@pytest.fixture(autouse=True)
def _memory_store():
    with patch("homeassistant.components.local_todo.LocalTodoListStore", _MemoryStore):
        yield


async def _local_list(hass: HomeAssistant) -> None:
    """HA's own Local To-do, which keeps descriptions (the quantity)."""
    entry = MockConfigEntry(
        domain="local_todo", data={"todo_list_name": "Shopping", "storage_key": "shopping"}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(LIST) is not None


async def _items(hass: HomeAssistant) -> dict[str, dict]:
    result = await hass.services.async_call(
        "todo",
        "get_items",
        {"status": ["needs_action", "completed"]},
        target={"entity_id": LIST},
        blocking=True,
        return_response=True,
    )
    return {i["summary"]: i for i in result[LIST]["items"]}


async def _todo(hass: HomeAssistant, service: str, data: dict) -> None:
    await hass.services.async_call("todo", service, data, target={"entity_id": LIST}, blocking=True)


async def _next_round(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_existing_list_is_merged_not_doubled(
    hass: HomeAssistant, api, freezer: FrozenDateTimeFactory
) -> None:
    await _local_list(hass)
    await _todo(hass, "add_item", {"item": "Mjölk"})
    await _todo(hass, "add_item", {"item": "Kaffe", "description": "1 paket"})
    api.shopping.return_value = [
        {"id": 1, "name": "mjölk", "quantity": "2 l", "done": False},
        {"id": 2, "name": "Bröd", "quantity": None, "done": True},
    ]
    api.shop_add.return_value = {"id": 3}
    await _setup(hass, {CONF_SHOPPING_LIST: LIST})

    # no list of our own
    assert hass.states.get("todo.homeplanarr_shopping_list") is None
    # same name: paired, the app's values win; app only: added to HA; HA only: added to the app
    items = await _items(hass)
    assert set(items) == {"mjölk", "Kaffe", "Bröd"}
    assert items["mjölk"]["description"] == "2 l"
    assert items["Bröd"]["status"] == "completed"
    api.shop_add.assert_called_once_with("Kaffe", "1 paket")

    # nothing changed: the next round writes nothing
    api.shopping.return_value = [
        *api.shopping.return_value,
        {"id": 3, "name": "Kaffe", "quantity": "1 paket", "done": False},
    ]
    api.shop_add.reset_mock()
    api.shop_update.reset_mock()
    await _next_round(hass, freezer)
    api.shop_add.assert_not_called()
    api.shop_update.assert_not_called()
    assert len(await _items(hass)) == 3


async def test_changes_flow_both_ways(
    hass: HomeAssistant, api, freezer: FrozenDateTimeFactory
) -> None:
    await _local_list(hass)
    rows = [
        {"id": 1, "name": "Mjölk", "quantity": None, "done": False},
        {"id": 2, "name": "Bröd", "quantity": None, "done": False},
    ]
    api.shopping.return_value = rows
    await _setup(hass, {CONF_SHOPPING_LIST: LIST})
    assert set(await _items(hass)) == {"Mjölk", "Bröd"}

    # ticked off and removed in HA: sent to the app
    await _todo(hass, "update_item", {"item": "Mjölk", "status": "completed"})
    await _todo(hass, "remove_item", {"item": ["Bröd"]})
    await _next_round(hass, freezer)
    api.shop_update.assert_called_with(1, {"done": True})
    api.shop_delete.assert_called_once_with(2)

    # renamed and removed in the app: shown in HA
    api.shopping.return_value = [{"id": 1, "name": "Havremjölk", "quantity": None, "done": True}]
    await _next_round(hass, freezer)
    items = await _items(hass)
    assert set(items) == {"Havremjölk"}
    assert items["Havremjölk"]["status"] == "completed"


async def test_pairs_survive_a_restart(
    hass: HomeAssistant, api, freezer: FrozenDateTimeFactory
) -> None:
    await _local_list(hass)
    api.shopping.return_value = [{"id": 1, "name": "Ägg", "quantity": None, "done": False}]
    entry = await _setup(hass, {CONF_SHOPPING_LIST: LIST})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    # removed in HA while paired: removed in the app, not re-added to HA
    await _todo(hass, "remove_item", {"item": ["Ägg"]})
    await _next_round(hass, freezer)
    api.shop_delete.assert_called_once_with(1)


async def test_own_list_cannot_be_picked(hass: HomeAssistant, api) -> None:
    entry = await _setup(hass, {})
    own = er.async_get(hass).async_get_entity_id(
        "todo", "homeplanarr", f"{entry.entry_id}_shopping_list"
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"energy_interval": "15", CONF_SHOPPING_LIST: own}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SHOPPING_LIST: "own_list"}


async def test_list_without_descriptions(
    hass: HomeAssistant, api, freezer: FrozenDateTimeFactory
) -> None:
    """HA's built-in Shopping list has no descriptions: the quantity stays in the app."""
    entry = MockConfigEntry(domain="shopping_list")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    target = "todo.shopping_list"
    api.shopping.return_value = [{"id": 1, "name": "Mjölk", "quantity": "2 l", "done": False}]
    await _setup(hass, {CONF_SHOPPING_LIST: target})
    result = await hass.services.async_call(
        "todo", "get_items", {}, target={"entity_id": target}, blocking=True, return_response=True
    )
    assert [i["summary"] for i in result[target]["items"]] == ["Mjölk"]
    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Mjölk", "rename": "Havremjölk"},
        target={"entity_id": target},
        blocking=True,
    )
    await _next_round(hass, freezer)
    # only the name was sent; the quantity was not cleared
    api.shop_update.assert_called_once_with(1, {"name": "Havremjölk"})
