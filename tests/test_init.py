"""Setup, sensors, pushes and the chore action, against a faked API."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.homeplanarr.api import HomeplanarrAuthError, HomeplanarrError
from custom_components.homeplanarr.const import (
    CONF_BATTERY_ENTITIES,
    CONF_ENERGY_ENTITIES,
    CONF_ENERGY_INTERVAL,
    CONF_LEVEL_ENTITIES,
    CONF_WATER_ENTITIES,
    DOMAIN,
)
from custom_components.homeplanarr.pusher import MeterTracker


async def _setup(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Homeplanarr",
        unique_id="https://homeplanarr.com",
        data={CONF_URL: "https://homeplanarr.com", CONF_API_KEY: "hp_test"},
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow(hass: HomeAssistant, api) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "homeplanarr.com/app/", CONF_API_KEY: " hp_abc "}
    )
    assert result["step_id"] == "entities"
    with patch("custom_components.homeplanarr.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ENERGY_ENTITIES: ["sensor.meter"], CONF_ENERGY_INTERVAL: "15"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: "https://homeplanarr.com", CONF_API_KEY: "hp_abc"}
    assert result["options"][CONF_ENERGY_ENTITIES] == ["sensor.meter"]


async def test_config_flow_bad_key(hass: HomeAssistant, api) -> None:
    api.sensors.side_effect = HomeplanarrAuthError("nope")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "https://homeplanarr.com", CONF_API_KEY: "hp_bad"}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_sensors(hass: HomeAssistant, api) -> None:
    await _setup(hass, {})
    cost = hass.states.get("sensor.homeplanarr_monthly_cost")
    assert cost is not None, [s.entity_id for s in hass.states.async_all()]
    assert float(cost.state) == 12345.5
    assert cost.attributes["unit_of_measurement"] == "SEK"
    assert cost.attributes["monthly_cost_loans"] == 10345.5
    assert hass.states.get("sensor.homeplanarr_next_chore").state == "Take out trash"
    cheap = hass.states.get("sensor.homeplanarr_cheapest_3_hours_start")
    assert dt_util.parse_datetime(cheap.state) == datetime(
        2026, 10, 16, 2, tzinfo=dt_util.get_default_time_zone()
    )


async def test_auth_failure_starts_reauth(hass: HomeAssistant, api) -> None:
    api.sensors.side_effect = HomeplanarrAuthError("revoked")
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_URL: "https://homeplanarr.com", CONF_API_KEY: "hp_x"}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress()
    )


async def test_energy_quarter_hours(
    hass: HomeAssistant, api, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config.async_set_time_zone("Europe/Stockholm")
    freezer.move_to("2026-10-16 10:07:00+02:00")
    hass.states.async_set("sensor.a", "100.0", {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.b", "5000", {"unit_of_measurement": "Wh"})
    await _setup(hass, {CONF_ENERGY_ENTITIES: ["sensor.a", "sensor.b"], CONF_ENERGY_INTERVAL: "15"})

    hass.states.async_set("sensor.a", "100.5", {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.b", "5250", {"unit_of_measurement": "Wh"})
    freezer.move_to("2026-10-16 10:15:10+02:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # set up at 10:07: the first quarter carries what was used from then on
    api.energy.assert_called_once()
    reading = api.energy.call_args.args[0]
    assert reading["ts"] == "2026-10-16T10:00:00"
    assert reading["kwh"] == pytest.approx(0.75)
    assert reading["sensor_id"] == "sensor.homeplanarr_energy_total"
    assert reading["source"] == "home_assistant"

    # a meter that drops is re-based, not counted
    hass.states.async_set("sensor.a", "0.2", {"unit_of_measurement": "kWh"})
    freezer.move_to("2026-10-16 10:30:10+02:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert api.energy.call_args.args[0]["kwh"] == pytest.approx(0)


async def test_energy_retried_after_network_error(
    hass: HomeAssistant, api, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config.async_set_time_zone("Europe/Stockholm")
    freezer.move_to("2026-10-16 10:00:30+02:00")
    hass.states.async_set("sensor.a", "1", {"unit_of_measurement": "kWh"})
    await _setup(hass, {CONF_ENERGY_ENTITIES: ["sensor.a"], CONF_ENERGY_INTERVAL: "15"})
    api.energy.side_effect = HomeplanarrError("offline")
    hass.states.async_set("sensor.a", "2", {"unit_of_measurement": "kWh"})
    freezer.move_to("2026-10-16 10:15:10+02:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    hass.states.async_set("sensor.a", "2.5", {"unit_of_measurement": "kWh"})
    freezer.move_to("2026-10-16 10:30:10+02:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    sent = api.bulk.call_args.args[0]
    assert [(r["ts"], r["kwh"]) for r in sent] == [
        ("2026-10-16T10:00:00", 1.0),
        ("2026-10-16T10:15:00", 0.5),
    ]
    assert sent[0]["sensor_id"] == "sensor.a"


def test_meter_tracker_spreads_a_gap() -> None:
    tz = dt_util.get_default_time_zone()
    tracker = MeterTracker(None)
    t0 = datetime(2026, 10, 16, 10, tzinfo=tz)
    hour = timedelta(hours=1)
    assert tracker.step(t0, {"m": 10.0}, hour, {"m"}) == []
    periods = tracker.step(t0 + 3 * hour, {"m": 16.0}, hour, {"m"})
    assert [(p[0].hour, p[1]["m"]) for p in periods] == [(10, 2.0), (11, 2.0), (12, 2.0)]
    # the same boundary twice changes nothing; the 4 kWh land in the next period
    assert tracker.step(t0 + 3 * hour, {"m": 20.0}, hour, {"m"}) == []
    again = MeterTracker(tracker.as_dict())  # restored from storage
    assert again.boundary == t0 + 3 * hour and again.baselines == {"m": 16.0}
    assert again.step(t0 + 4 * hour, {"m": 20.0}, hour, {"m"})[0][1] == {"m": 4.0}


async def test_hourly_metrics_and_batteries(
    hass: HomeAssistant, api, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config.async_set_time_zone("Europe/Stockholm")
    freezer.move_to("2026-10-16 10:20:00+02:00")
    hass.states.async_set("sensor.water", "12.000", {"unit_of_measurement": "m³"})
    hass.states.async_set(
        "sensor.kitchen_temp", "21.5", {"unit_of_measurement": "°C", "device_class": "temperature"}
    )
    hass.states.async_set("sensor.radon", "80", {"unit_of_measurement": "Bq/m³"})
    hass.states.async_set(
        "sensor.smoke_battery", "15", {"unit_of_measurement": "%", "device_class": "battery"}
    )
    await _setup(
        hass,
        {
            CONF_WATER_ENTITIES: ["sensor.water"],
            CONF_LEVEL_ENTITIES: ["sensor.kitchen_temp", "sensor.radon"],
            CONF_BATTERY_ENTITIES: ["sensor.smoke_battery"],
        },
    )
    hass.states.async_set("sensor.water", "12.150", {"unit_of_measurement": "m³"})
    freezer.move_to("2026-10-16 11:00:25+02:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # first hour is the baseline for water; levels go every hour
    # (the first send, a minute after start, may land in the same jump)
    by_kind = {r["kind"]: r for c in api.metrics.call_args_list for r in c.args[0]}
    assert by_kind["temperature"]["value"] == 21.5
    assert by_kind["radon"]["value"] == 80
    assert by_kind["water"]["value"] == pytest.approx(150)
    assert by_kind["water"]["ts"] == "2026-10-16T10:00:00"
    devices = api.devices.call_args.args[0]
    assert devices == [
        {"entity_id": "sensor.smoke_battery", "name": "sensor.smoke_battery", "battery_pct": 15.0}
    ]


async def test_complete_chore(hass: HomeAssistant, api) -> None:
    await _setup(hass, {})
    result = await hass.services.async_call(
        DOMAIN,
        "complete_chore",
        {"chore": "Take out trash", "member": "Alice"},
        blocking=True,
        return_response=True,
    )
    assert result == {"id": 9, "points": 5}
    api.chore.assert_called_once_with("Take out trash", "Alice", "via Home Assistant")
