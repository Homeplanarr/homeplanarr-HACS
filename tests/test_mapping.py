"""HA's NFC tags linked to chores, and HA's users paired with household members."""

from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.homeplanarr.const import (
    CONF_ENERGY_INTERVAL,
    CONF_FORWARD_TAGS,
    CONF_MEMBERS,
)

from .test_init import _setup


def _tag(hass: HomeAssistant, tag_id: str, name: str) -> None:
    er.async_get(hass).async_get_or_create(
        "tag", "tag", tag_id, original_name=f"Tag {tag_id}"
    )
    entity_id = er.async_get(hass).async_get_entity_id("tag", "tag", tag_id)
    er.async_get(hass).async_update_entity(entity_id, name=name)


async def _users(hass: HomeAssistant):
    alice = await hass.auth.async_create_user("Alice")
    bob = await hass.auth.async_create_user("Robert")
    return alice, bob


async def test_options_map_tags_and_people(hass: HomeAssistant, api) -> None:
    _tag(hass, "tag-kitchen", "Köket")
    _tag(hass, "tag-door", "Ytterdörren")
    alice, bob = await _users(hass)
    entry = await _setup(hass, {})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ENERGY_INTERVAL: "15", CONF_FORWARD_TAGS: True}
    )
    assert result["step_id"] == "tags"
    fields = {str(k): k.default() for k in result["data_schema"].schema}
    # preset to what the tag already does in Homeplanarr
    assert fields == {"Köket": "4", "Ytterdörren": "0"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"Köket": "0", "Ytterdörren": "3"}
    )
    api.tag_update.assert_called_once_with(11, {"chore_id": None})
    api.tag_create.assert_called_once_with("tag-door", "Ytterdörren", 3)

    assert result["step_id"] == "members"
    fields = {str(k): k.default() for k in result["data_schema"].schema}
    # Alice matches "Alice Svensson" by first name; Robert matches nobody
    assert fields["Alice"] == "1"
    assert fields["Robert"] == "0"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**fields, "Robert": "2"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MEMBERS] == {alice.id: "1", bob.id: "2"}


async def test_no_tags_step_without_ha_tags(hass: HomeAssistant, api) -> None:
    await _users(hass)
    entry = await _setup(hass, {})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ENERGY_INTERVAL: "15", CONF_FORWARD_TAGS: True}
    )
    assert result["step_id"] == "members"


async def test_pairings_kept_when_tags_stay_on(hass: HomeAssistant, api) -> None:
    api.members.return_value = []  # nothing to pair: the step is skipped
    entry = await _setup(hass, {CONF_FORWARD_TAGS: True, CONF_MEMBERS: {"abc": "2"}})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ENERGY_INTERVAL: "15", CONF_FORWARD_TAGS: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MEMBERS] == {"abc": "2"}


async def test_scan_credits_the_paired_member(hass: HomeAssistant, api) -> None:
    alice, bob = await _users(hass)
    await _setup(hass, {CONF_FORWARD_TAGS: True, CONF_MEMBERS: {bob.id: "2"}})

    hass.bus.async_fire("tag_scanned", {"tag_id": "tag-door"}, context=Context(user_id=bob.id))
    await hass.async_block_till_done()
    api.nfc_scan.assert_called_with("tag-door", "2")

    # not paired: the HA user's name, as before
    hass.bus.async_fire("tag_scanned", {"tag_id": "tag-door"}, context=Context(user_id=alice.id))
    await hass.async_block_till_done()
    api.nfc_scan.assert_called_with("tag-door", "Alice")


async def test_complete_chore_without_member_uses_the_pairing(hass: HomeAssistant, api) -> None:
    _, bob = await _users(hass)
    await _setup(hass, {CONF_MEMBERS: {bob.id: "2"}})
    await hass.services.async_call(
        "homeplanarr",
        "complete_chore",
        {"chore": "Dammsuga"},
        blocking=True,
        context=Context(user_id=bob.id),
    )
    api.chore.assert_called_once_with("Dammsuga", "2", "via Home Assistant")
