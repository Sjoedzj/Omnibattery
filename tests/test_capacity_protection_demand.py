"""R3 — capacity protection must keep publishing the real demand.

While peak shaving holds a battery, the planner lowers ``consumption_kwh`` so
the projection shows no overnight drain. The household keeps consuming all of
it, though, and commit b50bfce2 fixed exactly that for the no-discharge window
and the price block (``pricing/engine.py:_battery_blocked_interval``): the true
demand rides along in ``demand_kwh`` and the published forecast reads it.

The capacity-protection path subtracted ``limit_kw x hours`` from the
consumption *without* setting ``demand_kwh``, so the dashboard under-reported
the forecast by exactly the energy the grid was carrying.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.pricing.daily_timeline import BatteryProjectionInput
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.tracking.consumption_profile import ConsumptionForecast
from custom_components.omnibattery.tracking.daily_projection import (
    DailyOperationProjectionRequest,
    build_daily_operation_projection,
)

TZ = timezone(timedelta(hours=2))
BASE_KW = 0.3618
CAPACITY_KWH = 5.12
SUNRISE_HOUR = 6.3
HOLD_LIMIT_W = 50.0
# The hold lasts until sunrise; the interval starting exactly at 06:30 is no
# longer held (``start < held_until``).
HELD_UNTIL = datetime(2026, 9, 11, int(SUNRISE_HOUR), 30, tzinfo=TZ)


class _Coord:
    def __init__(self, soc):
        self.min_soc = 12
        self.max_soc = 100
        self.data = {"battery_soc": soc, "battery_total_energy": CAPACITY_KWH}
        self.name = "Venus 0"
        self.is_available = True


def _manager(soc=45.0, *, limit_w=HOLD_LIMIT_W):
    def forecast_between(start, end, *, fallback="legacy_daily"):
        hours = (end - start).total_seconds() / 3600.0
        return ConsumptionForecast(
            BASE_KW * hours, [BASE_KW * 0.25] * 96, "profile", True
        )

    tracker = SimpleNamespace(
        consumption_profile=SimpleNamespace(),
        forecast_consumption_between=forecast_between,
        solar_profile=SimpleNamespace(_days={}, get_snapshot=lambda **_k: None),
        calculate_sunrise=lambda for_date=None: SUNRISE_HOUR,
        calculate_solar_noon=lambda: 13.0,
    )
    controller = SimpleNamespace(
        coordinators=[_Coord(soc)],
        charging_time_slots=[],
        config_entry=SimpleNamespace(data={}, options={}),
        _consumption_tracker=tracker,
        solar_profile_mode="off",
        _predictive_safety_margin_kwh=0.0,
        _predictive_min_soc_floor=20.0,
        _predictive_min_soc_floor_enabled=True,
        _active_time_slot_quota_kwh=None,
        max_contracted_power=14000.0,
        max_charge_capacity=1500.0,
        max_price_threshold=None,
        charge_delay_enabled=False,
        _solar_t_start=None,
        _is_battery_manual_owned=lambda _c: False,
        _charge_ceiling_soc=lambda _c: 100.0,
        # The peak-shaving hold: a 50 W contracted limit is being protected.
        _is_capacity_protection_soc_limited=lambda: True,
        capacity_protection_limit=limit_w,
    )
    manager = PricingManager(
        SimpleNamespace(states=SimpleNamespace(get=lambda _e: None)), controller
    )
    manager._store_chronological_diagnostics = lambda _d: None
    manager._solar_timeline_input = lambda now, data, *, horizon_end: SimpleNamespace(
        remaining_kwh=0.0,
        periods=None,
        temporal_shape=None,
        original_source="remaining_sensor",
        conversion="none",
        source="remaining",
    )
    return manager


def _plan(manager, now=None):
    return manager._build_chronological_plan(
        now=now or datetime(2026, 9, 11, 1, 0, tzinfo=TZ),
        slots=[],
        decision_data={
            "should_charge": False,
            "avg_consumption_kwh": BASE_KW * 24,
            "energy_deficit_kwh": 0.0,
            "planned_grid_charge_kwh": 0.0,
            "excluded_demand_claim_kwh": 0.0,
        },
        price_ceiling=None,
    )


def _held(plan):
    """The intervals the peak-shaving hold covers (before sunrise)."""
    return [item for item in plan.intervals if item.start < HELD_UNTIL]


def test_the_hold_lowers_the_drain_but_keeps_the_demand():
    plan = _plan(_manager())
    held = _held(plan)

    assert held, "the fixture must produce held intervals"
    for item in held:
        assert item.demand_kwh is not None
        # The reduced figure is what the projection drains...
        assert item.consumption_kwh < item.demand_kwh
        # ...and the demand is the untouched household load, minus nothing.
        reduction = HOLD_LIMIT_W / 1000.0 * (
            (item.end - item.start).total_seconds() / 3600.0
        )
        assert item.consumption_kwh == pytest.approx(item.demand_kwh - reduction)


def test_an_interval_outside_the_hold_keeps_the_shared_meaning():
    """``demand_kwh is None`` still means "consumption_kwh is the demand"."""
    plan = _plan(_manager())
    outside = [
        item for item in plan.intervals if item.start >= HELD_UNTIL
    ]

    assert outside, "the fixture must produce unheld intervals"
    for item in outside:
        assert item.demand_kwh is None


def test_the_published_forecast_reports_the_demand_the_grid_carries():
    """The end-to-end observable: what the dashboard is shown."""
    now = datetime(2026, 9, 11, 1, 0, tzinfo=TZ)
    plan = _plan(_manager(), now=now)
    held = _held(plan)

    projection = build_daily_operation_projection(
        DailyOperationProjectionRequest(
            now=now,
            plan_intervals=tuple(plan.intervals),
            allocations=(),
            battery_inputs=(
                BatteryProjectionInput("venus", 45.0 / 100 * CAPACITY_KWH, CAPACITY_KWH,
                                       0.0, 100.0, 1500.0, 1500.0),
            ),
            mode="normal",
            decision_data={},
        )
    )

    # 01:00 is the first held quarter of the horizon.
    published = next(item for item in projection["intervals"] if item["index"] == 4)
    assert published["consumption_forecast_kwh"] == pytest.approx(
        held[0].demand_kwh
    )
    # The simulation still sees the reduced drain, so no phantom overnight
    # discharge is projected...
    assert published["consumption_kwh"] == pytest.approx(held[0].consumption_kwh)
    # ...while the demand the grid actually serves is not lost.
    assert published["consumption_forecast_kwh"] > published["consumption_kwh"]
    assert published["consumption_forecast_kwh"] - published["consumption_kwh"] == (
        pytest.approx(HOLD_LIMIT_W / 1000.0 * 0.25)
    )
