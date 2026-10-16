"""The shopping list as a to-do list, synced both ways."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.homeplanarr.api import HomeplanarrRequestError

from .test_init import _setup

ENTITY = "todo.homeplanarr_shopping_list"
ROWS = [
    {"id": 1, "name": "Mjölk", "quantity": "2 l", "done": False},
    {"id": 2, "name": "Bröd", "quantity": None, "done": True},
]


async def _items(hass: HomeAssistant) -> list[dict]:
    result = await hass.services.async_call(
        "todo",
        "get_items",
        {"status": ["needs_action", "completed"]},
        target={"entity_id": ENTITY},
        blocking=True,
        return_response=True,
    )
    return result[ENTITY]["items"]


async def test_list_from_homeplanarr(
    hass: HomeAssistant, api, freezer: FrozenDateTimeFactory
) -> None:
    api.shopping.return_value = ROWS
    await _setup(hass, {})
    assert hass.states.get(ENTITY).state == "1"  # one open item
    items = await _items(hass)
    assert [(i["summary"], i["status"], i.get("description")) for i in items] == [
        ("Mjölk", "needs_action", "2 l"),
        ("Bröd", "completed", None),
    ]
    # an item added in the app shows up at the next poll
    api.shopping.return_value = [*ROWS, {"id": 3, "name": "Ost", "quantity": None, "done": False}]
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "2"


async def test_changes_in_ha_go_to_homeplanarr(hass: HomeAssistant, api) -> None:
    api.shopping.return_value = ROWS
    await _setup(hass, {})

    await hass.services.async_call(
        "todo",
        "add_item",
        {"item": "Ost", "description": "1 bit"},
        target={"entity_id": ENTITY},
        blocking=True,
    )
    api.shop_add.assert_called_once_with("Ost", "1 bit")

    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Mjölk", "status": "completed"},
        target={"entity_id": ENTITY},
        blocking=True,
    )
    api.shop_update.assert_called_with(1, {"done": True})

    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Bröd", "rename": "Surdegsbröd", "status": "needs_action"},
        target={"entity_id": ENTITY},
        blocking=True,
    )
    api.shop_update.assert_called_with(2, {"name": "Surdegsbröd", "done": False})

    # removed in the app meanwhile: still counts as removed
    api.shop_delete.side_effect = HomeplanarrRequestError(404, "Item not found")
    await hass.services.async_call(
        "todo",
        "remove_item",
        {"item": ["Bröd"]},
        target={"entity_id": ENTITY},
        blocking=True,
    )
    api.shop_delete.assert_called_once_with(2)
