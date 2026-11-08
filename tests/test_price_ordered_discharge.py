"""Price-ordered discharge: the dearest quarters claim scarce energy first.

The projection used to discharge in clock order.  When the battery ran empty
halfway through an expensive block, the *cheapest* quarters of that block got
the energy and the dearest ones stayed on the grid.  These tests pin the new
behaviour:

* scarce energy lands on the most expensive quarters (duurste eerst);
* the daily ``battery_to_home_kwh`` total is identical to the chronological run;
* the grid books the demand the battery no longer covers (``grid_to_home_kwh``);
* with enough energy, or without prices, nothing changes at all;
* ``min_soc`` is never crossed and the charge side is not touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.omnibattery.pricing.daily_timeline import (
    BatteryProjectionInput,
    ProjectionIntervalInput,
    simulate_battery_projection,
)


START = datetime(2026, 11, 8, 15, 0)


def _interval(index: int, consumption: float, price: float | None) -> ProjectionIntervalInput:
    start = START + timedelta(minutes=15 * index)
    return ProjectionIntervalInput(
        start,
        start + timedelta(minutes=15),
        consumption_kwh=consumption,
        price_kwh=price,
    )


def _battery(
    stored: float = 2.0,
    *,
    min_soc: float = 0.0,
    capacity: float = 10.0,
    charge_power: float = 4000.0,
    discharge_power: float = 4000.0,
) -> BatteryProjectionInput:
    return BatteryProjectionInput(
        "battery-a",
        stored,
        capacity,
        min_soc,
        100.0,
        charge_power,
        discharge_power,
    )


def _discharged_at(result) -> dict[str, float]:
    return {
        flow.start.strftime("%H:%M"): flow.battery_to_home_kwh
        for flow in result.intervals
        if flow.battery_to_home_kwh > 1e-9
    }


def test_scarce_energy_goes_to_the_dearest_quarters_first():
    prices = [0.10, 0.20, 0.50, 0.40]
    intervals = [_interval(index, 1.0, price) for index, price in enumerate(prices)]

    result = simulate_battery_projection(intervals, [_battery(stored=2.0)])

    # Two kWh of usable energy, four deficit quarters: only the two dearest get
    # battery output.  Time order would have picked 15:00 and 15:15.
    assert _discharged_at(result) == {
        "15:30": pytest.approx(1.0),
        "15:45": pytest.approx(1.0),
    }


def test_total_discharge_energy_is_identical_to_the_chronological_run():
    prices = [0.10, 0.20, 0.50, 0.40]
    priced = [_interval(index, 1.0, price) for index, price in enumerate(prices)]
    chronological = [
        _interval(index, 1.0, None) for index, _price in enumerate(prices)
    ]

    ordered_result = simulate_battery_projection(priced, [_battery(stored=2.0)])
    baseline_result = simulate_battery_projection(
        chronological, [_battery(stored=2.0)]
    )

    ordered_total = sum(
        flow.battery_to_home_kwh for flow in ordered_result.intervals
    )
    baseline_total = sum(
        flow.battery_to_home_kwh for flow in baseline_result.intervals
    )
    assert ordered_total == pytest.approx(baseline_total)
    assert ordered_total == pytest.approx(2.0)
    # Same energy, different quarters.
    assert [flow.battery_to_home_kwh for flow in ordered_result.intervals] != [
        flow.battery_to_home_kwh for flow in baseline_result.intervals
    ]


def test_grid_import_books_the_demand_the_battery_no_longer_covers():
    prices = [0.10, 0.20, 0.50, 0.40]
    intervals = [_interval(index, 1.0, price) for index, price in enumerate(prices)]

    result = simulate_battery_projection(intervals, [_battery(stored=2.0)])

    # Every quarter still serves exactly its own 1.0 kWh demand.
    for flow in result.intervals:
        assert flow.battery_to_home_kwh + flow.grid_to_home_kwh == pytest.approx(
            flow.consumption_kwh
        )
    # The two skipped cheap quarters are supplied by the grid.
    assert [flow.grid_to_home_kwh for flow in result.intervals] == pytest.approx(
        [1.0, 1.0, 0.0, 0.0]
    )


def test_ample_energy_leaves_the_projection_untouched():
    prices = [0.10, 0.20, 0.50, 0.40]
    priced = [_interval(index, 1.0, price) for index, price in enumerate(prices)]
    chronological = [
        _interval(index, 1.0, None) for index, _price in enumerate(prices)
    ]

    ordered_result = simulate_battery_projection(priced, [_battery(stored=4.0)])
    baseline_result = simulate_battery_projection(
        chronological, [_battery(stored=4.0)]
    )

    assert [
        (flow.battery_to_home_kwh, flow.grid_to_home_kwh, flow.stored_energy_end_kwh)
        for flow in ordered_result.intervals
    ] == pytest.approx(
        [
            (
                flow.battery_to_home_kwh,
                flow.grid_to_home_kwh,
                flow.stored_energy_end_kwh,
            )
            for flow in baseline_result.intervals
        ]
    )


def test_single_price_keeps_the_chronological_order():
    intervals = [_interval(index, 1.0, 0.30) for index in range(4)]
    chronological = [_interval(index, 1.0, None) for index in range(4)]

    ordered_result = simulate_battery_projection(intervals, [_battery(stored=2.0)])
    baseline_result = simulate_battery_projection(
        chronological, [_battery(stored=2.0)]
    )

    assert [flow.battery_to_home_kwh for flow in ordered_result.intervals] == (
        pytest.approx([flow.battery_to_home_kwh for flow in baseline_result.intervals])
    )


def test_min_soc_is_never_crossed_when_energy_is_reordered():
    prices = [0.10, 0.20, 0.50, 0.40]
    intervals = [_interval(index, 1.0, price) for index, price in enumerate(prices)]
    # 50% of 10 kWh is the floor; 7 kWh stored leaves exactly 2 kWh usable.
    battery = _battery(stored=7.0, min_soc=50.0)

    result = simulate_battery_projection(intervals, [battery])

    assert result.final_stored_kwh_by_battery["battery-a"] == pytest.approx(5.0)
    for flow in result.intervals:
        assert flow.stored_energy_end_kwh >= 5.0 - 1e-9
    assert _discharged_at(result) == {
        "15:30": pytest.approx(1.0),
        "15:45": pytest.approx(1.0),
    }


def test_charge_side_is_replayed_unchanged_while_discharge_moves():
    intervals = [
        ProjectionIntervalInput(
            START,
            START + timedelta(minutes=15),
            consumption_kwh=0.0,
            solar_kwh=1.0,
            price_kwh=0.10,
        ),
        ProjectionIntervalInput(
            START + timedelta(minutes=15),
            START + timedelta(minutes=30),
            consumption_kwh=0.0,
            solar_kwh=1.0,
            price_kwh=0.20,
        ),
    ]
    prices = [0.10, 0.50, 0.40, 0.30]
    intervals += [
        _interval(index, 1.0, prices[index - 2]) for index in range(2, 6)
    ]
    without_prices = [
        ProjectionIntervalInput(item.start, item.end, item.consumption_kwh, item.solar_kwh)
        for item in intervals
    ]
    battery = _battery(stored=0.0)

    ordered_result = simulate_battery_projection(
        intervals, [battery], charge_efficiency=1.0
    )
    baseline_result = simulate_battery_projection(
        without_prices, [battery], charge_efficiency=1.0
    )

    # Charging is identical: the same two solar quarters carry the same input.
    assert [flow.solar_to_battery_kwh for flow in ordered_result.intervals] == (
        pytest.approx(
            [flow.solar_to_battery_kwh for flow in baseline_result.intervals]
        )
    )
    # The 2 kWh that was charged now serves the dearest quarters.
    assert [flow.battery_to_home_kwh for flow in ordered_result.intervals] == (
        pytest.approx([0.0, 0.0, 0.0, 1.0, 1.0, 0.0])
    )
    assert sum(
        flow.battery_to_home_kwh for flow in ordered_result.intervals
    ) == pytest.approx(
        sum(flow.battery_to_home_kwh for flow in baseline_result.intervals)
    )


def test_daily_operation_adapter_ranks_the_published_plan_by_price():
    """The controller adapter must actually feed the price curve to the projection."""
    from types import SimpleNamespace

    from custom_components.omnibattery import ChargeDischargeController
    from custom_components.omnibattery.pricing import PriceSlot

    now = START
    intervals = [_interval(index, 1.0, None) for index in range(4)]
    slots = [
        PriceSlot(item.start, item.end, price)
        for item, price in zip(intervals, [0.10, 0.20, 0.50, 0.40])
    ]
    plan = SimpleNamespace(intervals=intervals, allocations=[])

    def build_projection(**_kwargs):
        return SimpleNamespace(plan=plan, diagnostics={})

    def get_future_price_slots(horizon_end=None, include_past=False):
        return slots

    controller = SimpleNamespace(
        _daily_operation_mode=lambda: "dynamic_pricing",
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
        _daily_operation_battery_inputs=lambda: [_battery(stored=2.0)],
        _consumption_tracker=object(),
        _pricing_mgr=SimpleNamespace(
            build_extended_chronological_projection=build_projection,
            get_future_price_slots=get_future_price_slots,
        ),
        _last_decision_data={},
        _last_chronological_diagnostics={},
        _dynamic_pricing_schedule=None,
        predictive_charging_enabled=False,
        charge_delay_enabled=False,
        _daily_operation_delay_active=lambda: False,
        _daily_operation_delay_unlock=lambda _now: None,
        _delay_setpoint_enabled=False,
        _delay_setpoint_reached=False,
        _charge_delay_unlocked=False,
        _charge_delay_status={},
        max_price_threshold=None,
        manual_mode_enabled=False,
        enable_system_power_limits=False,
    )

    result = ChargeDischargeController._daily_operation_build_projection(
        controller, now
    )

    assert result is not None
    assert [
        item["battery_to_home_kwh"] for item in result["intervals"]
    ] == pytest.approx([0.0, 0.0, 1.0, 1.0])

