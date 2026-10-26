from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


SUMMARY = {
    "monthly_cost_total": 12345.5,
    "monthly_cost_expenses": 2000,
    "monthly_cost_loans": 10345.5,
    "energy_month_kwh": 321.2,
    "energy_month_cost": 410.0,
    "chores_due": 3,
    "chores_overdue": 1,
    "next_chore": "Take out trash",
    "leader_name": "Alice",
    "leader_points": 40,
    "cheapest_3h_start": "2026-10-16T02:00:00",
    "low_battery_devices": 0,
    "silent_sensors": 0,
    "waste_next_date": "2026-10-28",
    "waste_next_days": 2,
    "waste_next_types": "Restavfall, Matavfall",
    "waste_next_place": None,
    "waste_homes": [{"property_id": 1, "name": "Villan", "date": "2026-10-28", "days": 2, "types": ["Restavfall", "Matavfall"], "place": None}],
    "chores_by_person": [
        {"user_id": 1, "name": "Alice", "open": 0, "overdue": 0, "chores": []},
        {"user_id": 2, "name": "Bob", "open": 2, "overdue": 1, "chores": ["Disk", "Dammsuga"]},
    ],
    "currency": "SEK",
}

VEHICLES = [
    {"id": 7, "name": "Volvon", "kind": "car", "odometer_unit": "mil", "odometer": 1200},
    {"id": 8, "name": "Husbilen", "kind": "motorhome", "odometer_unit": "km", "odometer": None},
]

API = "custom_components.homeplanarr.api.HomeplanarrApi"


@pytest.fixture
def api():
    with (
        patch(f"{API}.sensors", AsyncMock(return_value=SUMMARY)) as sensors,
        patch(f"{API}.energy", AsyncMock(return_value={"id": 1})) as energy,
        patch(f"{API}.energy_bulk", AsyncMock(return_value={"imported": 2})) as bulk,
        patch(f"{API}.metrics", AsyncMock(return_value={"stored": 1})) as metrics,
        patch(f"{API}.device_status", AsyncMock(return_value={"stored": 1})) as devices,
        patch(f"{API}.chore_event", AsyncMock(return_value={"id": 9, "points": 5})) as chore,
        patch(f"{API}.vehicles", AsyncMock(return_value=VEHICLES)) as vehicles,
        patch(f"{API}.odometer", AsyncMock(return_value={"stored": 1})) as odometer,
        patch(f"{API}.shopping_list", AsyncMock(return_value=[])) as shopping,
        patch(f"{API}.shopping_add", AsyncMock(return_value={"id": 3})) as shop_add,
        patch(f"{API}.shopping_update", AsyncMock(return_value={"id": 1})) as shop_update,
        patch(f"{API}.shopping_delete", AsyncMock(return_value=None)) as shop_delete,
    ):
        yield type(
            "Api",
            (),
            dict(
                sensors=sensors,
                energy=energy,
                bulk=bulk,
                metrics=metrics,
                devices=devices,
                chore=chore,
                vehicles=vehicles,
                odometer=odometer,
                shopping=shopping,
                shop_add=shop_add,
                shop_update=shop_update,
                shop_delete=shop_delete,
            ),
        )
