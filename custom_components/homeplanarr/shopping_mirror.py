"""Keep a to-do list the user already has in Home Assistant in step with the
household's shopping list, instead of adding Homeplanarr's own list.

Both sides are read every minute and whenever the chosen list changes. The
pairs of items that belong together are remembered (with the values both sides
last agreed on), so the side that changed since then wins; when both changed
the same field, Homeplanarr wins. Unpaired items with the same name are paired
rather than copied, so picking a list that already holds the shopping does not
double it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.todo import TodoListEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store

from .api import HomeplanarrApi, HomeplanarrError, HomeplanarrRequestError
from .const import DOMAIN, SHOPPING_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# Several changes in a row on the HA side (or our own writes echoing back) make
# one round, not one each.
COOLDOWN = 2


def store_for(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.shopping_mirror.{entry_id}")


@dataclass
class Values:
    name: str
    qty: str | None
    done: bool


@dataclass
class Pair:
    hp: int
    ha: str
    last: Values


def _hp_values(row: dict[str, Any]) -> Values:
    qty = row.get("quantity")
    return Values(
        str(row.get("name") or ""),
        str(qty) if qty not in (None, "") else None,
        bool(row.get("done")),
    )


def _ha_values(item: dict[str, Any], last: Values | None, with_qty: bool) -> Values:
    qty = item.get("description") or None
    return Values(
        str(item.get("summary") or ""),
        qty if with_qty else (last.qty if last else None),
        item.get("status") == "completed",
    )


class ShoppingMirror:
    """Syncs Homeplanarr's shopping list with an existing HA to-do entity."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, api: HomeplanarrApi, target: str
    ) -> None:
        self.hass = hass
        self.api = api
        self.target = target
        self._store = store_for(hass, entry_id)
        self._pairs: list[Pair] = []
        self._unsubs: list = []
        self._debouncer = Debouncer(
            hass, _LOGGER, cooldown=COOLDOWN, immediate=True, function=self._sync
        )

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        if stored.get("target") == self.target:
            self._pairs = [
                Pair(p[0], p[1], Values(p[2], p[3], p[4])) for p in stored.get("pairs", [])
            ]
        self._unsubs = [
            async_track_time_interval(self.hass, self._tick, SHOPPING_SCAN_INTERVAL),
            # also covers the list appearing after HA has started
            async_track_state_change_event(self.hass, [self.target], self._changed),
        ]
        await self._debouncer.async_call()

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        self._debouncer.async_shutdown()

    async def _tick(self, _now) -> None:
        await self._debouncer.async_call()

    @callback
    def _changed(self, _event: Event[EventStateChangedData]) -> None:
        self.hass.async_create_task(self._debouncer.async_call())

    async def _sync(self) -> None:
        state = self.hass.states.get(self.target)
        if state is None or state.state == "unavailable":
            return
        with_qty = bool(
            (state.attributes.get(ATTR_SUPPORTED_FEATURES) or 0)
            & TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
        )
        try:
            pairs = await self._round(with_qty)
        except (HomeplanarrError, HomeAssistantError) as err:
            # nothing is saved: every step is found again next round (pairs by name)
            _LOGGER.warning("Could not sync the shopping list with %s: %s", self.target, err)
            return
        except Exception:  # the other list's own failure must not stop setup or the timer
            _LOGGER.exception("Could not sync the shopping list with %s", self.target)
            return
        self._pairs = pairs
        await self._store.async_save(
            {
                "target": self.target,
                "pairs": [[p.hp, p.ha, p.last.name, p.last.qty, p.last.done] for p in pairs],
            }
        )

    async def _items(self) -> list[dict[str, Any]]:
        result = await self.hass.services.async_call(
            "todo",
            "get_items",
            {"status": ["needs_action", "completed"]},
            target={"entity_id": self.target},
            blocking=True,
            return_response=True,
        )
        items = (result or {}).get(self.target, {}).get("items", [])
        return [i for i in items if i.get("uid")]

    async def _todo(self, service: str, data: dict[str, Any]) -> None:
        await self.hass.services.async_call(
            "todo", service, data, target={"entity_id": self.target}, blocking=True
        )

    async def _set_ha(self, uid: str, want: Values, have: Values, with_qty: bool) -> None:
        data: dict[str, Any] = {"item": uid}
        if want.name != have.name:
            data["rename"] = want.name
        if want.done != have.done:
            data["status"] = "completed" if want.done else "needs_action"
        if with_qty and want.qty != have.qty:
            data["description"] = want.qty or ""
        if len(data) > 1:
            await self._todo("update_item", data)

    async def _set_hp(self, item_id: int, want: Values, have: Values) -> None:
        changes: dict[str, Any] = {}
        if want.name != have.name:
            changes["name"] = want.name
        if want.qty != have.qty:
            changes["quantity"] = want.qty
        if want.done != have.done:
            changes["done"] = want.done
        if changes:
            await self.api.shopping_update(item_id, changes)

    async def _round(self, with_qty: bool) -> list[Pair]:
        rows = {int(r["id"]): r for r in await self.api.shopping_list()}
        items = {i["uid"]: i for i in await self._items()}
        pairs: list[Pair] = []

        for pair in self._pairs:
            row = rows.pop(pair.hp, None)
            item = items.pop(pair.ha, None)
            if row is None and item is None:
                continue
            if row is None:  # removed in the app
                await self._todo("remove_item", {"item": [pair.ha]})
                continue
            if item is None:  # removed in HA
                try:
                    await self.api.shopping_delete(pair.hp)
                except HomeplanarrRequestError as err:
                    if err.status != 404:
                        raise
                continue
            hp = _hp_values(row)
            ha = _ha_values(item, pair.last, with_qty)
            last = pair.last
            # per field: the side that moved away from what both agreed on wins, the app first
            want = Values(
                *(
                    h if h != l else a
                    for h, a, l in (
                        (hp.name, ha.name, last.name),
                        (hp.qty, ha.qty, last.qty),
                        (hp.done, ha.done, last.done),
                    )
                )
            )
            await self._set_hp(pair.hp, want, hp)
            await self._set_ha(pair.ha, want, ha, with_qty)
            pairs.append(Pair(pair.hp, pair.ha, want))

        # the same item on both sides, not yet paired: pair it, the app's values win
        by_name: dict[str, list[dict[str, Any]]] = {}
        for item in items.values():
            by_name.setdefault(str(item.get("summary") or "").strip().casefold(), []).append(item)
        for item_id, row in list(rows.items()):
            twins = by_name.get(str(row.get("name") or "").strip().casefold())
            if not twins:
                continue
            item = twins.pop(0)
            del rows[item_id]
            del items[item["uid"]]
            hp = _hp_values(row)
            await self._set_ha(item["uid"], hp, _ha_values(item, hp, with_qty), with_qty)
            pairs.append(Pair(item_id, item["uid"], hp))

        # only in HA: add to the app
        for item in items.values():
            values = _ha_values(item, None, with_qty)
            if not values.name.strip():
                continue
            created = await self.api.shopping_add(values.name, values.qty)
            new_id = int(created["id"])
            if values.done:
                await self.api.shopping_update(new_id, {"done": True})
            pairs.append(Pair(new_id, item["uid"], values))

        # only in the app: add to HA, then find the new items' uids (add_item returns none)
        if rows:
            known = {p.ha for p in pairs}
            for item_id, row in list(rows.items()):
                hp = _hp_values(row)
                if not hp.name.strip():
                    del rows[item_id]
                    continue
                data: dict[str, Any] = {"item": hp.name}
                if with_qty and hp.qty:
                    data["description"] = hp.qty
                await self._todo("add_item", data)
            fresh: dict[str, list[dict[str, Any]]] = {}
            for item in await self._items():
                if item["uid"] not in known:
                    fresh.setdefault(str(item.get("summary") or ""), []).append(item)
            for item_id, row in rows.items():
                hp = _hp_values(row)
                matches = fresh.get(hp.name)
                if not matches:
                    continue  # paired by name next round
                item = matches.pop(0)
                if hp.done:
                    await self._todo("update_item", {"item": item["uid"], "status": "completed"})
                pairs.append(Pair(item_id, item["uid"], hp))

        return pairs
