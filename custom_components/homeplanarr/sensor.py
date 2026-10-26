"""Sensors with the household's figures from Homeplanarr."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import HomeplanarrConfigEntry
from .const import DOMAIN
from .coordinator import HomeplanarrCoordinator

MONEY = "money"  # unit filled from the household's currency


@dataclass(frozen=True, kw_only=True)
class HomeplanarrSensorDescription(SensorEntityDescription):
    """A sensor read from one key of the summary."""

    key_unit: str | None = None
    value_fn: Callable[[dict[str, Any]], Any] | None = None
    attrs: tuple[str, ...] = ()


def _local_ts(key: str) -> Callable[[dict[str, Any]], datetime | None]:
    """Homeplanarr sends naive local times; attach HA's zone."""

    def read(data: dict[str, Any]) -> datetime | None:
        raw = data.get(key)
        if not raw:
            return None
        parsed = dt_util.parse_datetime(str(raw))
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
        return parsed

    return read


def _date(key: str) -> Callable[[dict[str, Any]], date | None]:
    def read(data: dict[str, Any]) -> date | None:
        raw = data.get(key)
        return dt_util.parse_date(str(raw)) if raw else None

    return read


SENSORS: tuple[HomeplanarrSensorDescription, ...] = (
    HomeplanarrSensorDescription(
        key="monthly_cost_total",
        translation_key="monthly_cost_total",
        key_unit=MONEY,
        device_class=SensorDeviceClass.MONETARY,
        attrs=(
            "monthly_cost_expenses",
            "monthly_cost_loans",
            "monthly_cost_rent",
            "monthly_cost_property_tax",
        ),
    ),
    HomeplanarrSensorDescription(
        key="energy_month_kwh",
        translation_key="energy_month_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
    ),
    HomeplanarrSensorDescription(
        key="energy_month_cost",
        translation_key="energy_month_cost",
        key_unit=MONEY,
        device_class=SensorDeviceClass.MONETARY,
    ),
    HomeplanarrSensorDescription(
        key="water_month_l",
        translation_key="water_month_l",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL,
        attrs=("water_month_cost",),
    ),
    HomeplanarrSensorDescription(
        key="solar_month_kwh",
        translation_key="solar_month_kwh",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        attrs=("solar_month_value",),
    ),
    HomeplanarrSensorDescription(
        key="chores_due",
        translation_key="chores_due",
        state_class=SensorStateClass.MEASUREMENT,
        attrs=("next_chore",),
    ),
    HomeplanarrSensorDescription(
        key="chores_overdue",
        translation_key="chores_overdue",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HomeplanarrSensorDescription(
        key="next_chore",
        translation_key="next_chore",
    ),
    HomeplanarrSensorDescription(
        key="leader_name",
        translation_key="leader_name",
        attrs=("leader_points",),
    ),
    HomeplanarrSensorDescription(
        key="next_renewal_name",
        translation_key="next_renewal_name",
        attrs=("next_renewal_date", "next_renewal_days"),
    ),
    HomeplanarrSensorDescription(
        key="cheapest_3h_start",
        translation_key="cheapest_3h_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_local_ts("cheapest_3h_start"),
        attrs=("cheapest_3h_end", "cheapest_3h_price"),
    ),
    HomeplanarrSensorDescription(
        key="waste_next_date",
        translation_key="next_waste_pickup",
        device_class=SensorDeviceClass.DATE,
        value_fn=_date("waste_next_date"),
        attrs=("waste_next_days", "waste_next_types", "waste_next_place", "waste_homes"),
    ),
    HomeplanarrSensorDescription(
        key="low_battery_devices",
        translation_key="low_battery_devices",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HomeplanarrSensorDescription(
        key="silent_sensors",
        translation_key="silent_sensors",
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeplanarrConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(HomeplanarrSensor(coordinator, entry.entry_id, d) for d in SENSORS)

    # one "Chores left" sensor per person, added as people appear
    known: set[int] = set()

    def add_people() -> None:
        new = [
            p
            for p in (coordinator.data or {}).get("chores_by_person") or []
            if isinstance(p, dict) and isinstance(p.get("user_id"), int) and p["user_id"] not in known
        ]
        known.update(p["user_id"] for p in new)
        if new:
            async_add_entities(
                ChoresLeftSensor(coordinator, entry.entry_id, p["user_id"], str(p.get("name") or p["user_id"]))
                for p in new
            )

    add_people()
    entry.async_on_unload(coordinator.async_add_listener(add_people))


def _device(coordinator: HomeplanarrCoordinator, entry_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Homeplanarr",
        manufacturer="Homeplanarr",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=coordinator.api.app_url,
    )


class HomeplanarrSensor(CoordinatorEntity[HomeplanarrCoordinator], SensorEntity):
    """One figure from the household summary."""

    _attr_has_entity_name = True
    entity_description: HomeplanarrSensorDescription

    def __init__(
        self,
        coordinator: HomeplanarrCoordinator,
        entry_id: str,
        description: HomeplanarrSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = _device(coordinator, entry_id)

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        if self.entity_description.value_fn:
            return self.entity_description.value_fn(data)
        return data.get(self.entity_description.key)

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.key_unit == MONEY:
            return (self.coordinator.data or {}).get("currency") or "SEK"
        return self.entity_description.native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self.entity_description.attrs:
            return None
        data = self.coordinator.data or {}
        return {key: data.get(key) for key in self.entity_description.attrs}


class ChoresLeftSensor(CoordinatorEntity[HomeplanarrCoordinator], SensorEntity):
    """The chores assigned to one person that are due today or overdue."""

    _attr_has_entity_name = True
    _attr_translation_key = "chores_left"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: HomeplanarrCoordinator, entry_id: str, user_id: int, name: str
    ) -> None:
        super().__init__(coordinator)
        self._user_id = user_id
        self._attr_unique_id = f"{entry_id}_chores_left_{user_id}"
        self._attr_translation_placeholders = {"person": name}
        self._attr_device_info = _device(coordinator, entry_id)

    def _person(self) -> dict[str, Any] | None:
        for p in (self.coordinator.data or {}).get("chores_by_person") or []:
            if isinstance(p, dict) and p.get("user_id") == self._user_id:
                return p
        return None

    @property
    def available(self) -> bool:
        # a person who left the household (or a key without chores) shows unavailable
        return super().available and self._person() is not None

    @property
    def native_value(self) -> int | None:
        person = self._person()
        return person.get("open") if person else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        person = self._person()
        if not person:
            return None
        return {"overdue": person.get("overdue"), "chores": person.get("chores") or []}
