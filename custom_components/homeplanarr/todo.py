"""The household's shopping list as a Home Assistant to-do list.

Two-way: what is added, renamed, ticked off or removed in Home Assistant is
sent to Homeplanarr straight away, and the list is read back from Homeplanarr
every minute, so changes made in the app show up here too. The quantity is the
item's description.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HomeplanarrConfigEntry
from .api import HomeplanarrError, HomeplanarrRequestError
from .const import CONF_SHOPPING_LIST, DOMAIN
from .coordinator import ShoppingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeplanarrConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.options.get(CONF_SHOPPING_LIST):
        # synced with a list the user already has (shopping_mirror.py): no list of our own
        registry = er.async_get(hass)
        if entity_id := registry.async_get_entity_id("todo", DOMAIN, _unique_id(entry.entry_id)):
            registry.async_remove(entity_id)
        return
    async_add_entities([ShoppingListEntity(entry.runtime_data.shopping, entry.entry_id)])


def _unique_id(entry_id: str) -> str:
    return f"{entry_id}_shopping_list"


def _to_item(row: dict[str, Any]) -> TodoItem:
    quantity = row.get("quantity")
    return TodoItem(
        uid=str(row["id"]),
        summary=str(row.get("name") or ""),
        status=TodoItemStatus.COMPLETED if row.get("done") else TodoItemStatus.NEEDS_ACTION,
        description=str(quantity) if quantity not in (None, "") else None,
    )


class ShoppingListEntity(CoordinatorEntity[ShoppingCoordinator], TodoListEntity):
    """Homeplanarr's shared shopping list."""

    _attr_has_entity_name = True
    _attr_translation_key = "shopping_list"
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )

    def __init__(self, coordinator: ShoppingCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = _unique_id(entry_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Homeplanarr",
            manufacturer="Homeplanarr",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=coordinator.api.app_url,
        )

    @property
    def todo_items(self) -> list[TodoItem]:
        return [_to_item(row) for row in self.coordinator.data or []]

    def _row(self, uid: str) -> dict[str, Any] | None:
        return next((r for r in self.coordinator.data or [] if str(r["id"]) == uid), None)

    async def async_create_todo_item(self, item: TodoItem) -> None:
        name = (item.summary or "").strip()
        if not name:
            raise HomeAssistantError("An item needs a name")
        await self._call(self.coordinator.api.shopping_add(name, item.description))

    async def async_update_todo_item(self, item: TodoItem) -> None:
        row = self._row(item.uid or "")
        if row is None:
            raise HomeAssistantError("The item is no longer on the list")
        changes: dict[str, Any] = {}
        if item.summary is not None and item.summary.strip() != row.get("name"):
            changes["name"] = item.summary.strip()
        quantity = item.description or None
        if quantity != (row.get("quantity") or None):
            changes["quantity"] = quantity
        done = item.status == TodoItemStatus.COMPLETED
        if done != bool(row.get("done")):
            changes["done"] = done
        if changes:
            await self._call(self.coordinator.api.shopping_update(int(row["id"]), changes))

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        for uid in uids:
            try:
                await self.coordinator.api.shopping_delete(int(uid))
            except HomeplanarrRequestError as err:
                if err.status != 404:  # already gone in the app: that is what was asked
                    await self.coordinator.async_request_refresh()
                    raise HomeAssistantError(f"Homeplanarr: {err}") from err
            except HomeplanarrError as err:
                await self.coordinator.async_request_refresh()
                raise HomeAssistantError(f"Homeplanarr: {err}") from err
        await self.coordinator.async_refresh()

    async def _call(self, request) -> None:
        try:
            await request
        except HomeplanarrError as err:
            raise HomeAssistantError(f"Homeplanarr: {err}") from err
        finally:
            # read the list back, so HA shows what Homeplanarr stored
            await self.coordinator.async_refresh()
