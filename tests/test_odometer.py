"""A car's odometer sent to its Homeplanarr vehicle, and current values sent soon."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.homeplanarr.const import (
    CONF_ENERGY_INTERVAL,
    CONF_LEVEL_ENTITIES,
    CONF_ODOMETER_ENTITIES,
    CONF_ODOMETERS,
)

from .test_init import _setup

ODO = "sensor.volvo_odometer"


async def _after_start(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """The first send waits a minute after HA has started, for slow sensors."""
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_options_pair_a_sensor_with_a_vehicle(hass: HomeAssistant, api) -> None:
    entry = await _setup(hass, {})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ENERGY_INTERVAL: "15", CONF_ODOMETER_ENTITIES: [ODO]}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "vehicles"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {ODO: "7"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ODOMETERS] == {ODO: "7"}


async def test_no_vehicles_yet(hass: HomeAssistant, api) -> None:
    api.vehicles.return_value = []
    entry = await _setup(hass, {})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ENERGY_INTERVAL: "15", CONF_ODOMETER_ENTITIES: [ODO]}
    )
    assert result["errors"] == {CONF_ODOMETER_ENTITIES: "no_vehicles"}


async def test_odometer_sent_at_start_and_every_hour(
    hass: HomeAssistant, api, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-10-25 18:20:00+00:00")
    hass.states.async_set(ODO, "12345.6", {"unit_of_measurement": "km", "device_class": "distance"})
    await _setup(hass, {CONF_ODOMETER_ENTITIES: [ODO], CONF_ODOMETERS: {ODO: "7"}})
    api.odometer.assert_not_called()
    await _after_start(hass, freezer)
    api.odometer.assert_called_once_with([{"vehicle_id": 7, "value": 12345.6, "unit": "km"}])

    hass.states.async_set(ODO, "12380", {"unit_of_measurement": "km", "device_class": "distance"})
    freezer.move_to("2026-10-25 19:00:25+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert api.odometer.call_args.args[0] == [{"vehicle_id": 7, "value": 12380.0, "unit": "km"}]


async def test_unpicked_sensor_is_not_sent(hass: HomeAssistant, api) -> None:
    hass.states.async_set(ODO, "1", {"unit_of_measurement": "km"})
    # the pairing is left over, but the sensor is no longer picked
    await _setup(hass, {CONF_ODOMETER_ENTITIES: [], CONF_ODOMETERS: {ODO: "7"}})
    api.odometer.assert_not_called()


async def test_indoor_sensor_sent_soon(
    hass: HomeAssistant, api, freezer: FrozenDateTimeFactory
) -> None:
    """A sensor just picked shows up in Homeplanarr without waiting for the hour,
    also when it only gets its value a little after HA has started (Bluetooth)."""
    radon = "sensor.airthings_wave_radon_longterm_average"
    await _setup(hass, {CONF_LEVEL_ENTITIES: [radon]})
    hass.states.async_set(radon, "48", {"unit_of_measurement": "Bq/m³"})
    await _after_start(hass, freezer)
    readings = api.metrics.call_args.args[0]
    assert [(r["kind"], r["entity_id"], r["value"]) for r in readings] == [("radon", radon, 48.0)]
