"""Sends the chosen Home Assistant entities to Homeplanarr.

Replaces the hand-written utility_meter + rest_command + automation setup:

* Energy meters (kWh that only go up) are read at every quarter hour (or
  hour). The difference since the last boundary is one reading, stamped with
  the start of the period in local time, so Homeplanarr prices it against the
  right hour.
* Water and solar meters work the same way, hourly, as homemetric readings.
* Level sensors (temperature, humidity, CO2, radon...) send their current
  value once an hour, with the entity's area as the room.
* Battery sensors are reported as devices once an hour.

The integration keeps each meter's last value itself (in .storage), so no
utility_meter helper is needed and a restart does not lose the period.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import HomeplanarrApi, HomeplanarrError, HomeplanarrRequestError
from .const import (
    CONF_BATTERY_ENTITIES,
    CONF_ENERGY_ENTITIES,
    CONF_ENERGY_INTERVAL,
    CONF_FORWARD_TAGS,
    CONF_LEVEL_ENTITIES,
    CONF_ODOMETER_ENTITIES,
    CONF_ODOMETERS,
    CONF_SOLAR_ENTITIES,
    CONF_WATER_ENTITIES,
    DEFAULT_ENERGY_INTERVAL,
    DOMAIN,
    MAX_CATCHUP,
    MAX_PENDING,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
ENERGY_TOTAL_ID = "sensor.homeplanarr_energy_total"

ENERGY_FACTORS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0, "GJ": 277.778, "MJ": 0.277778}
WATER_FACTORS = {
    "L": 1.0,
    "mL": 0.001,
    "m³": 1000.0,
    "ft³": 28.3168,
    "gal": 3.78541,
    "CCF": 2831.68,
}
LEVEL_KINDS = {
    "temperature": "temperature",
    "humidity": "humidity",
    "carbon_dioxide": "co2",
    "pm25": "pm25",
    "pm10": "pm10",
    "volatile_organic_compounds": "voc",
    "volatile_organic_compounds_parts": "voc",
    "pressure": "pressure",
    "illuminance": "illuminance",
}


def naive_local(moment: datetime) -> str:
    """Homeplanarr stores local wall-clock time without an offset."""
    return dt_util.as_local(moment).replace(tzinfo=None, microsecond=0).isoformat()


def floor_to(moment: datetime, minutes: int) -> datetime:
    """Start of the period `moment` falls in, in local time."""
    local = dt_util.as_local(moment)
    return local.replace(minute=(local.minute // minutes) * minutes, second=0, microsecond=0)


class MeterTracker:
    """Turns cumulative meters into per-period amounts.

    `baselines` holds each meter's value at `boundary`. A meter that went
    down (reset, or a Sum helper missing one of its members) is re-based
    without counting anything: counting the jump back up would invent
    consumption. An unavailable meter keeps its old baseline, so what it used
    meanwhile lands in the period it comes back in.
    """

    def __init__(self, data: dict[str, Any] | None) -> None:
        data = data or {}
        self.baselines: dict[str, float] = dict(data.get("baselines") or {})
        raw = data.get("boundary")
        self.boundary: datetime | None = dt_util.parse_datetime(raw) if raw else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "baselines": self.baselines,
            "boundary": self.boundary.isoformat() if self.boundary else None,
        }

    def step(
        self, boundary: datetime, values: dict[str, float], period: timedelta, keep: set[str]
    ) -> list[tuple[datetime, dict[str, float]]]:
        """Advance to `boundary`; return (period start, amount per meter) pairs."""
        for entity_id in list(self.baselines):
            if entity_id not in keep:
                del self.baselines[entity_id]
        previous = self.boundary
        if previous is not None and boundary <= previous:
            return []  # fired twice for the same boundary
        deltas: dict[str, float] = {}
        for entity_id, value in values.items():
            base = self.baselines.get(entity_id)
            if base is not None and value >= base:
                deltas[entity_id] = value - base
            self.baselines[entity_id] = value
        self.boundary = boundary
        if previous is None or not deltas:
            return []
        gap = boundary - previous
        if gap > MAX_CATCHUP:
            _LOGGER.info("Homeplanarr: %s without readings, not sending that gap", gap)
            return []
        periods = max(1, round(gap / period))
        start = boundary - period * periods
        # HA was down for a while: spread the gap evenly over its periods
        return [
            (start + period * i, {e: d / periods for e, d in deltas.items()})
            for i in range(periods)
        ]


class Pusher:
    """Schedules and sends the pushes for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: HomeplanarrApi) -> None:
        self.hass = hass
        self.entry = entry
        self.api = api
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._unsubs: list[CALLBACK_TYPE] = []
        self._energy = MeterTracker(None)
        self._meters = MeterTracker(None)
        self._pending_energy: list[dict[str, Any]] = []
        self._pending_metrics: list[dict[str, Any]] = []
        self._warned: set[str] = set()

    @property
    def options(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    def _list(self, key: str) -> list[str]:
        return list(self.options.get(key) or [])

    async def async_start(self) -> None:
        data = await self._store.async_load() or {}
        self._energy = MeterTracker(data.get("energy"))
        self._meters = MeterTracker(data.get("meters"))
        self._pending_energy = list(data.get("pending_energy") or [])
        self._pending_metrics = list(data.get("pending_metrics") or [])

        if self._list(CONF_ENERGY_ENTITIES):
            minutes = int(self.options.get(CONF_ENERGY_INTERVAL) or DEFAULT_ENERGY_INTERVAL)
            marks = list(range(0, 60, minutes))
            # a few seconds past the boundary, so meters have reported the last value
            self._unsubs.append(
                async_track_utc_time_change(
                    self.hass, self._on_energy_tick, minute=marks, second=10
                )
            )
        if (
            any(
                self._list(k)
                for k in (
                    CONF_WATER_ENTITIES,
                    CONF_SOLAR_ENTITIES,
                    CONF_LEVEL_ENTITIES,
                    CONF_BATTERY_ENTITIES,
                )
            )
            or self._odometers
        ):
            self._unsubs.append(
                async_track_utc_time_change(self.hass, self._on_hour_tick, minute=0, second=25)
            )
        if self.options.get(CONF_FORWARD_TAGS):
            self._unsubs.append(self.hass.bus.async_listen("tag_scanned", self._on_tag))

        # Take a baseline right away, so the first full period is sent.
        now = dt_util.utcnow()
        if self._list(CONF_ENERGY_ENTITIES) and self._energy.boundary is None:
            self._energy.step(
                floor_to(now, self._energy_minutes),
                self._energy_values(),
                self._energy_period,
                set(self._list(CONF_ENERGY_ENTITIES)),
            )
        meters = self._list(CONF_WATER_ENTITIES) + self._list(CONF_SOLAR_ENTITIES)
        if meters and self._meters.boundary is None:
            self._meters.step(
                floor_to(now, 60), self._meter_values(), timedelta(hours=1), set(meters)
            )
        await self._save()
        # Indoor sensors, batteries and odometers are current values: send them
        # now, so a sensor just picked shows up without waiting for the hour.
        if self._list(CONF_LEVEL_ENTITIES) or self._list(CONF_BATTERY_ENTITIES) or self._odometers:
            self.entry.async_create_background_task(
                self.hass, self._send_current(), f"{DOMAIN} first push"
            )

    async def _send_current(self) -> None:
        self._pending_metrics = (self._pending_metrics + self._level_readings(dt_util.utcnow()))[
            -MAX_PENDING * 5 :
        ]
        await self._send_metrics()
        await self._send_devices()
        await self._send_odometers()
        await self._save()

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "energy": self._energy.as_dict(),
                "meters": self._meters.as_dict(),
                "pending_energy": self._pending_energy,
                "pending_metrics": self._pending_metrics,
            }
        )

    # --- reading entity states -------------------------------------------------

    def _number(self, entity_id: str, factors: dict[str, float] | None) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        if value != value or value in (float("inf"), float("-inf")):
            return None
        if factors is None:
            return value
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        factor = factors.get(unit) if unit else None
        if factor is None:
            if entity_id not in self._warned:
                self._warned.add(entity_id)
                _LOGGER.warning(
                    "Homeplanarr: %s has unit %r, which it cannot convert; skipping it",
                    entity_id,
                    unit,
                )
            return None
        return value * factor

    @property
    def _energy_minutes(self) -> int:
        return int(self.options.get(CONF_ENERGY_INTERVAL) or DEFAULT_ENERGY_INTERVAL)

    @property
    def _energy_period(self) -> timedelta:
        return timedelta(minutes=self._energy_minutes)

    def _energy_values(self) -> dict[str, float]:
        values = {}
        for entity_id in self._list(CONF_ENERGY_ENTITIES):
            value = self._number(entity_id, ENERGY_FACTORS)
            if value is not None:
                values[entity_id] = value
        return values

    def _meter_values(self) -> dict[str, float]:
        values = {}
        for entity_id in self._list(CONF_WATER_ENTITIES):
            value = self._number(entity_id, WATER_FACTORS)
            if value is not None:
                values[entity_id] = value
        for entity_id in self._list(CONF_SOLAR_ENTITIES):
            value = self._number(entity_id, ENERGY_FACTORS)
            if value is not None:
                values[entity_id] = value
        return values

    def _room(self, entity_id: str) -> str | None:
        """The entity's area (or its device's), which Homeplanarr matches to a room by name."""
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None:
            return None
        area_id = entry.area_id
        if area_id is None and entry.device_id:
            device = dr.async_get(self.hass).async_get(entry.device_id)
            area_id = device.area_id if device else None
        area = ar.async_get(self.hass).async_get_area(area_id) if area_id else None
        return area.name if area else None

    def _level_kind(self, entity_id: str) -> str | None:
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        device_class = state.attributes.get(ATTR_DEVICE_CLASS)
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or ""
        if "Bq" in unit or "radon" in entity_id:
            return "radon"
        if device_class in LEVEL_KINDS:
            return LEVEL_KINDS[device_class]
        if "co2" in entity_id or "carbon_dioxide" in entity_id:
            return "co2"
        return None

    # --- energy ------------------------------------------------------------------

    async def _on_energy_tick(self, now: datetime) -> None:
        entities = self._list(CONF_ENERGY_ENTITIES)
        if not entities:
            return
        periods = self._energy.step(
            floor_to(now, self._energy_minutes),
            self._energy_values(),
            self._energy_period,
            set(entities),
        )
        sensor_id = entities[0] if len(entities) == 1 else ENERGY_TOTAL_ID
        for start, amounts in periods:
            self._pending_energy.append(
                {
                    "ts": naive_local(start),
                    "kwh": round(sum(amounts.values()), 5),
                    "entity_id": sensor_id,
                    "sensor_id": sensor_id,
                    "source": "home_assistant",
                }
            )
        self._pending_energy = self._pending_energy[-MAX_PENDING:]
        await self._send_energy()
        await self._save()

    async def _send_energy(self) -> None:
        if not self._pending_energy:
            return
        try:
            if len(self._pending_energy) == 1:
                await self.api.energy(self._pending_energy[0])
            else:
                await self.api.energy_bulk(self._pending_energy)
        except HomeplanarrError as err:
            if err.retryable:
                _LOGGER.warning("Homeplanarr: energy not sent, will retry: %s", err)
                return
            _LOGGER.error("Homeplanarr refused the energy readings: %s", err)
        self._pending_energy = []

    # --- metrics and devices -------------------------------------------------------

    async def _on_hour_tick(self, now: datetime) -> None:
        water = self._list(CONF_WATER_ENTITIES)
        solar = self._list(CONF_SOLAR_ENTITIES)
        readings: list[dict[str, Any]] = []
        if water or solar:
            hour = timedelta(hours=1)
            for start, amounts in self._meters.step(
                floor_to(now, 60), self._meter_values(), hour, set(water + solar)
            ):
                for entity_id, amount in amounts.items():
                    reading: dict[str, Any] = {
                        "kind": "water" if entity_id in water else "solar",
                        "entity_id": entity_id,
                        "value": round(amount, 4),
                        "unit": "L" if entity_id in water else "kWh",
                        "ts": naive_local(start),
                    }
                    if room := self._room(entity_id):
                        reading["room"] = room
                    readings.append(reading)

        readings.extend(self._level_readings(now))

        self._pending_metrics = (self._pending_metrics + readings)[-MAX_PENDING * 5 :]
        await self._send_metrics()
        await self._send_devices()
        await self._send_odometers()
        await self._save()

    def _level_readings(self, now: datetime) -> list[dict[str, Any]]:
        readings: list[dict[str, Any]] = []
        stamp = naive_local(now)
        for entity_id in self._list(CONF_LEVEL_ENTITIES):
            value = self._number(entity_id, None)
            kind = self._level_kind(entity_id)
            if value is None:
                continue
            if kind is None:
                if entity_id not in self._warned:
                    self._warned.add(entity_id)
                    _LOGGER.warning(
                        "Homeplanarr: cannot tell what %s measures; skipping it", entity_id
                    )
                continue
            state = self.hass.states.get(entity_id)
            reading = {
                "kind": kind,
                "entity_id": entity_id,
                "value": value,
                "unit": state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None,
                "ts": stamp,
            }
            if room := self._room(entity_id):
                reading["room"] = room
            readings.append(reading)
        return readings

    async def _send_metrics(self) -> None:
        if not self._pending_metrics:
            return
        try:
            await self.api.metrics(self._pending_metrics)
        except HomeplanarrError as err:
            if err.retryable:
                _LOGGER.warning("Homeplanarr: sensor readings not sent, will retry: %s", err)
                return
            _LOGGER.error("Homeplanarr refused the sensor readings: %s", err)
        self._pending_metrics = []

    async def _send_devices(self) -> None:
        devices = []
        for entity_id in self._list(CONF_BATTERY_ENTITIES):
            value = self._number(entity_id, None)
            if value is None:
                continue
            devices.append(
                {
                    "entity_id": entity_id,
                    "name": self._device_name(entity_id),
                    "battery_pct": max(0.0, min(100.0, value)),
                }
            )
        if not devices:
            return
        try:
            await self.api.device_status(devices)
        except HomeplanarrError as err:
            _LOGGER.warning("Homeplanarr: battery levels not sent: %s", err)

    def _device_name(self, entity_id: str) -> str:
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry and entry.device_id:
            device = dr.async_get(self.hass).async_get(entry.device_id)
            if device and (device.name_by_user or device.name):
                return device.name_by_user or device.name
        state = self.hass.states.get(entity_id)
        return str(state.attributes.get(ATTR_FRIENDLY_NAME, entity_id)) if state else entity_id

    # --- odometers --------------------------------------------------------------------

    @property
    def _odometers(self) -> dict[str, str]:
        """{odometer entity: vehicle id} for the sensors that are still picked."""
        picked = set(self._list(CONF_ODOMETER_ENTITIES))
        return {
            e: v for e, v in (self.options.get(CONF_ODOMETERS) or {}).items() if e in picked and v
        }

    async def _send_odometers(self) -> None:
        readings = []
        for entity_id, vehicle_id in self._odometers.items():
            value = self._number(entity_id, None)
            if value is None or value < 0:
                continue
            state = self.hass.states.get(entity_id)
            unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None
            reading: dict[str, Any] = {"vehicle_id": int(vehicle_id), "value": value}
            if unit:
                reading["unit"] = unit
            readings.append(reading)
        if not readings:
            return
        try:
            # Homeplanarr keeps one entry a day and ignores unchanged values
            await self.api.odometer(readings)
        except HomeplanarrError as err:
            _LOGGER.warning("Homeplanarr: odometer readings not sent: %s", err)

    # --- NFC tags ----------------------------------------------------------------------

    async def _on_tag(self, event: Event) -> None:
        tag_id = event.data.get("tag_id")
        if not tag_id:
            return
        member = None
        if event.context.user_id:
            user = await self.hass.auth.async_get_user(event.context.user_id)
            member = user.name if user else None
        try:
            try:
                await self.api.nfc_scan(str(tag_id), member)
            except HomeplanarrRequestError as err:
                # HA user names need not match the household's members: fall
                # back to the member set on the tag in Homeplanarr
                if not member or err.status != 404 or "Member" not in str(err):
                    raise
                await self.api.nfc_scan(str(tag_id), None)
        except HomeplanarrError as err:
            # unknown tags are normal: not every tag in the house is a chore
            _LOGGER.debug("Homeplanarr: tag %s not credited: %s", tag_id, err)
