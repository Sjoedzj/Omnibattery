"""What the plan actually allocates, published next to what the balance needs.

``planned_grid_charge_kwh`` is the scalar need the energy balance stated, and it
keeps that meaning: ``test_planning_horizon`` pins that a non-extended plan is
sized from it. The chronological plan can allocate less — or nothing — because
#662 leaves deadline energy to the grid when importing it later is cheaper than
storing it now. Reading the need as "planned grid charge" then told the user a
6.71 kWh charge was coming while the schedule was empty, so the allocated sum is
published as its own field and the notifications report it.

The plan is built through the real engine (``_build_chronological_plan``), so
these tests also prove the field is filled on the path the REMAINING rebuilds
take, i.e. without ``control_extension``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import PREDICTIVE_MODE_DYNAMIC_PRICING
from custom_components.omnibattery.pricing import PriceSlot, notifications
from tests.test_planning_horizon import NOW, _manager, _slot

NEED_KWH = 6.7072
# Everything on offer costs more than any deadline's own value_cap
# (price x round trip efficiency - margin), which is the reported situation:
# the peak quarters cost 0.79-0.89 and sit above their own ceiling.
DEAR = 0.89
CHEAP = 0.30


def _planning_manager(soc=30.0):
    manager = _manager(None, soc=soc)
    controller = manager._controller
    controller.predictive_charging_mode = PREDICTIVE_MODE_DYNAMIC_PRICING
    controller.min_arbitrage_margin = 0.05
    return manager


def _plan_with(price, need_kwh=NEED_KWH, ceiling=None, soc=30.0, consumption_kwh=2.0):
    manager = _planning_manager(soc=soc)
    # A control-horizon build takes its consumption total from the decision,
    # exactly as the REMAINING rebuilds do.
    decision = {
        "avg_consumption_kwh": consumption_kwh,
        "planned_grid_charge_kwh": need_kwh,
        "should_charge": True,
        "solar_forecast_input": None,
    }
    plan = manager._build_chronological_plan(
        now=NOW,
        slots=[_slot(0, hour, price) for hour in range(20, 24)],
        decision_data=decision,
        price_ceiling=ceiling,
    )
    return plan, decision


# ----------------------------------------------------------------------
# the published field
# ----------------------------------------------------------------------

def test_published_allocated_sum_is_zero_when_the_plan_buys_nothing():
    # The reported case: 6.7072 kWh is needed, every remaining quarter costs
    # more than the static ceiling (in production the arbitrage ceiling of the
    # peak quarters), so the plan buys nothing at all.
    plan, decision = _plan_with(DEAR, ceiling=0.70)

    assert plan.allocated_kwh == pytest.approx(0.0)
    assert plan.allocations == []

    # The scalar keeps its established meaning ...
    assert decision["planned_grid_charge_kwh"] == pytest.approx(NEED_KWH)
    # ... and the new field states what is really bought.
    assert decision["planned_grid_charge_allocated_kwh"] == pytest.approx(0.0)


def test_published_allocated_sum_matches_the_plan_not_the_need():
    # Cheap quarters the plan can place energy in; the scalar still states the
    # full need while the published allocation follows what the plan stores.
    plan, decision = _plan_with(CHEAP)

    assert plan.allocated_kwh > 0.0
    assert decision["planned_grid_charge_allocated_kwh"] == pytest.approx(
        plan.allocated_kwh
    )
    assert decision["planned_grid_charge_allocated_kwh"] < decision[
        "planned_grid_charge_kwh"
    ]
    assert decision["planned_grid_charge_kwh"] == pytest.approx(NEED_KWH)


def test_published_allocated_sum_follows_the_plan_when_662_leaves_energy_out():
    # Quarters under the static ceiling, but above the deadline's own value_cap
    # (a battery at 15% empties an hour in, so there is a real deadline): #662
    # leaves that deadline energy to the grid, and the published figure has to
    # follow the plan rather than the need.
    plan, decision = _plan_with(0.60, ceiling=0.70, soc=15.0)

    assert plan.cheaper_to_import_kwh > 0.0
    assert 0.0 < plan.allocated_kwh < NEED_KWH
    assert decision["planned_grid_charge_allocated_kwh"] == pytest.approx(
        plan.allocated_kwh
    )
    assert decision["planned_grid_charge_kwh"] == pytest.approx(NEED_KWH)


def test_the_allocated_sum_is_the_same_plan_that_produced_the_deadlines():
    plan, decision = _plan_with(0.60, ceiling=0.70, soc=15.0)
    assert decision["planned_grid_charge_allocated_kwh"] == pytest.approx(
        sum(allocation.planned_battery_kwh for allocation in plan.allocations)
    )


# ----------------------------------------------------------------------
# the notifications
# ----------------------------------------------------------------------

_CFG = dict(
    max_contracted_power=5000,
    max_charge_capacity=3000,
    charging_time_slot={"start_time": "23:00", "end_time": "06:00"},
)


def _notification_decision(allocated):
    return {
        "should_charge": True,
        "solar_forecast_kwh": 3.0,
        "usable_energy_kwh": 5.0,
        "avg_soc": 60.0,
        "avg_consumption_kwh": 8.0,
        "total_available_kwh": 9.0,
        "energy_deficit_kwh": 8.0,
        "planned_grid_charge_kwh": NEED_KWH,
        "planned_grid_charge_allocated_kwh": allocated,
        "solar_surplus_kwh": 2.5,
    }


def test_predictive_notification_names_the_difference_instead_of_the_need():
    _, message = notifications.format_predictive_notification_message(
        _notification_decision(0.0), **_CFG
    )

    assert "Planned grid charge: 0.00 kWh" in message
    assert (
        "Need 6.71 kWh — the rest is not stored ahead of use" in message
    )
    # The misleading claim is gone: the need must not read as a planned charge.
    assert "Planned grid charge: 6.71 kWh" not in message
    # The solar line is still reported.
    assert "Forecast solar surplus: 2.50 kWh" in message


def test_predictive_notification_is_unchanged_when_the_plan_places_it_all():
    _, message = notifications.format_predictive_notification_message(
        _notification_decision(NEED_KWH), **_CFG
    )

    assert "Planned grid charge: 6.71 kWh" in message
    assert "the rest is left to the grid" not in message


def test_predictive_notification_keeps_the_scalar_without_the_new_field():
    decision = _notification_decision(None)
    del decision["planned_grid_charge_allocated_kwh"]
    _, message = notifications.format_predictive_notification_message(decision, **_CFG)

    assert "Planned grid charge: 6.71 kWh" in message


def test_predictive_notification_ignores_a_sub_cent_difference():
    _, message = notifications.format_predictive_notification_message(
        _notification_decision(NEED_KWH - 0.001), **_CFG
    )

    assert "Planned grid charge: 6.71 kWh" in message
    assert "the rest is left to the grid" not in message


def _dynamic_schedule():
    start = datetime(2999, 1, 1, 18, 0)
    slot = PriceSlot(start, start + timedelta(minutes=15), CHEAP)
    return SimpleNamespace(
        hours_needed=1.0,
        selected_slots=[slot],
        average_price=CHEAP,
        estimated_cost=0.5,
        charging_needed=True,
        chronological_planning_active=False,
    )


def test_dynamic_pricing_notification_reports_the_allocated_charge():
    decision = {
        "should_charge": True,
        "avg_soc": 60.0,
        "usable_energy_kwh": 5.0,
        "solar_forecast_kwh": 3.0,
        "energy_deficit_kwh": 8.0,
        "planned_grid_charge_kwh": NEED_KWH,
        "planned_grid_charge_allocated_kwh": 1.25,
        "avg_consumption_kwh": 8.0,
    }
    _, message = notifications.format_dynamic_pricing_notification(
        decision,
        _dynamic_schedule(),
        unit="€/kWh",
        max_price_threshold=None,
        discharge_price_threshold=None,
        max_contracted_power=5000,
        max_charge_capacity=3000,
    )

    assert "Grid charge planned: 1.25 kWh" in message
    assert "Grid charge planned: 6.71 kWh" not in message


def test_dynamic_pricing_notification_falls_back_to_the_scalar():
    decision = {
        "should_charge": True,
        "avg_soc": 60.0,
        "usable_energy_kwh": 5.0,
        "solar_forecast_kwh": 3.0,
        "energy_deficit_kwh": 8.0,
        "planned_grid_charge_kwh": NEED_KWH,
        "avg_consumption_kwh": 8.0,
    }
    _, message = notifications.format_dynamic_pricing_notification(
        decision,
        _dynamic_schedule(),
        unit="€/kWh",
        max_price_threshold=None,
        discharge_price_threshold=None,
        max_contracted_power=5000,
        max_charge_capacity=3000,
    )

    assert "Grid charge planned: 6.71 kWh" in message
