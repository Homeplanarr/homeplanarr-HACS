"""Map Home Assistant's NFC tags to Homeplanarr chores and HA's people to members.

The tag -> chore links live in Homeplanarr itself (its NFC tags, which the app
shows too); the HA user -> member links are kept in the entry's options.
Form fields are keyed by a readable name, since HA shows the key as the label.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .api import HomeplanarrApi
from .const import UNMAPPED


def _unique_labels(items: list[tuple[str, str]]) -> dict[str, str]:
    """{label: id} with a short id suffix where two names are the same."""
    counts: dict[str, int] = {}
    for _, name in items:
        counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
    labels: dict[str, str] = {}
    for item_id, name in sorted(items, key=lambda i: i[1].casefold()):
        label = name if counts[name.casefold()] == 1 else f"{name} ({item_id[:6]})"
        labels[label] = item_id
    return labels


def ha_tags(hass: HomeAssistant) -> dict[str, str]:
    """HA's registered tags as {label: tag id}; each tag is a tag.* entity whose
    unique id is the tag id that `tag_scanned` carries."""
    tags = [
        (str(e.unique_id), str(e.name or e.original_name or e.unique_id))
        for e in er.async_get(hass).entities.values()
        if e.domain == "tag" and e.platform == "tag" and e.unique_id
    ]
    return _unique_labels(tags)


async def ha_users(hass: HomeAssistant) -> dict[str, str]:
    """People who can sign in to HA (not system users) as {label: user id}."""
    users = [
        (u.id, u.name or u.id)
        for u in await hass.auth.async_get_users()
        if u.is_active and not u.system_generated
    ]
    return _unique_labels(users)


def _picker(options: list[selector.SelectOptionDict]) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
    )


def tags_schema(
    tags: Mapping[str, str], chores: list[dict[str, Any]], linked: Mapping[str, str]
) -> vol.Schema:
    """One chore picker per HA tag, preset to what the tag does in Homeplanarr now."""
    options = [selector.SelectOptionDict(value=UNMAPPED, label="—")] + [
        selector.SelectOptionDict(value=str(c["id"]), label=str(c["name"]))
        for c in sorted(chores, key=lambda c: str(c["name"]).casefold())
    ]
    ids = {o["value"] for o in options}
    fields: dict[Any, Any] = {}
    for label, tag_id in tags.items():
        current = linked.get(tag_id, UNMAPPED)
        fields[vol.Required(label, default=current if current in ids else UNMAPPED)] = _picker(
            options
        )
    return vol.Schema(fields)


def linked_chores(remote: list[dict[str, Any]]) -> dict[str, str]:
    """{tag id: chore id} for Homeplanarr's tags that complete a chore."""
    return {
        str(t["tag_id"]): str(t["chore_id"]) for t in remote if t.get("tag_id") and t.get("chore_id")
    }


async def save_tags(
    api: HomeplanarrApi,
    tags: Mapping[str, str],
    picked: Mapping[str, str],
    remote: list[dict[str, Any]],
) -> None:
    """Make Homeplanarr's tags match what was picked: register a new tag under its
    HA name, relink a known one, unlink one set to no chore. Untouched tags and
    tags HA does not know are left alone."""
    known = {str(t["tag_id"]): t for t in remote if t.get("tag_id")}
    for label, tag_id in tags.items():
        choice = picked.get(label, UNMAPPED)
        chore_id = None if choice == UNMAPPED else int(choice)
        tag = known.get(tag_id)
        if tag is None:
            if chore_id is not None:
                await api.nfc_tag_create(tag_id, label, chore_id)
        elif (tag.get("chore_id") or None) != chore_id:
            await api.nfc_tag_update(int(tag["id"]), {"chore_id": chore_id})


def _guess(name: str, members: list[dict[str, Any]]) -> str:
    """The member with the same name, or the same first name, else nobody."""
    wanted = name.casefold().strip()
    for match in (
        lambda m: m == wanted,
        lambda m: m.split(" ")[0] == wanted.split(" ")[0],
    ):
        hits = [str(m["id"]) for m in members if match(str(m["name"]).casefold().strip())]
        if len(hits) == 1:
            return hits[0]
    return UNMAPPED


def members_schema(
    users: Mapping[str, str], members: list[dict[str, Any]], current: Mapping[str, str]
) -> vol.Schema:
    """One member picker per HA user, preset to the saved pairing or a name match."""
    options = [selector.SelectOptionDict(value=UNMAPPED, label="—")] + [
        selector.SelectOptionDict(value=str(m["id"]), label=str(m["name"])) for m in members
    ]
    ids = {o["value"] for o in options}
    fields: dict[Any, Any] = {}
    for label, user_id in users.items():
        picked = str(current.get(user_id) or "")
        if picked not in ids:
            picked = _guess(label, members)
        fields[vol.Required(label, default=picked)] = _picker(options)
    return vol.Schema(fields)


def members_from_input(users: Mapping[str, str], picked: Mapping[str, str]) -> dict[str, str]:
    """{HA user id: member id} for the people paired with a member."""
    return {
        user_id: str(picked[label])
        for label, user_id in users.items()
        if picked.get(label) not in (None, UNMAPPED)
    }
