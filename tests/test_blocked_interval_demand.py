"""A blocked battery still consumes: the projected demand must survive.

When the price block or a no-discharge window stops the battery, the planner
zeroes ``consumption_kwh`` because that interval drains nothing.  The household
demand does not disappear with it, so the published forecast reads
``demand_kwh`` instead.  These tests pin both halves of that contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing.chronological import EnergyInterval
from custom_components.omnibattery.pricing.daily_timeline import BatteryProjectionInput
from custom_components.omnibattery.pricing.engine import _battery_blocked_interval
from custom_components.omnibattery.tracking.daily_projection import (
    DailyOperationProjectionRequest,
    build_daily_operation_projection,
)

MADRID = ZoneInfo("Europe/Madrid")
QUARTER = timedelta(minutes=15)


def _interval(hour: int, minute: int, consumption: float, solar: float, demand=None):
    start = datetime(2026, 11, 4, hour, minute, tzinfo=MADRID)
    return EnergyInterval(start, start + QUARTER, consumption, solar, demand)


# ----------------------------------------------------------------------
# The helper the two block sites use
# ----------------------------------------------------------------------

def test_blocking_an_interval_keeps_its_demand():
    blocked = _battery_blocked_interval(_interval(12, 0, 0.25, 0.10))

    assert blocked.consumption_kwh == 0.0
    assert blocked.demand_kwh == pytest.approx(0.25)
    # Solar is untouched: the block stops discharge, not production.
    assert blocked.solar_kwh == pytest.approx(0.10)
    assert blocked.start == datetime(2026, 11, 4, 12, 0, tzinfo=MADRID)


def test_a_second_block_pass_keeps_the_original_demand():
    """The price block runs over a list the no-discharge window may have blocked.

    Reading ``consumption_kwh`` instead of an already-carried ``demand_kwh`` would
    turn the second pass into a no-op and lose the demand for good.
    """
    twice = _battery_blocked_interval(_battery_blocked_interval(_interval(12, 0, 0.25, 0.0)))

    assert twice.consumption_kwh == 0.0
    assert twice.demand_kwh == pytest.approx(0.25)


def test_an_unblocked_interval_carries_no_separate_demand():
    """Default stays None so every existing construction site is unaffected."""
    assert _interval(12, 0, 0.25, 0.0).demand_kwh is None


def test_a_negative_demand_is_rejected():
    with pytest.raises(ValueError, match="demand_kwh"):
        _interval(12, 0, 0.25, 0.0, demand=-0.1)


# ----------------------------------------------------------------------
# What the dashboard is actually shown
# ----------------------------------------------------------------------

def _projection(intervals):
    return build_daily_operation_projection(
        DailyOperationProjectionRequest(
            now=datetime(2026, 11, 4, 12, 7, tzinfo=MADRID),
            plan_intervals=tuple(intervals),
            allocations=(),
            battery_inputs=(BatteryProjectionInput("a", 5.0, 10.0, 0.0, 100.0, 4000, 4000),),
            mode="normal",
            decision_data={},
        )
    )


def _at(projection, index):
    return next(item for item in projection["intervals"] if item["index"] == index)


def test_published_forecast_keeps_the_demand_the_battery_may_not_serve():
    # 12:00 is in progress and blocked; 12:15 is blocked; 12:30 is not.
    projection = _projection(
        [
            _interval(12, 0, 0.0, 0.0, demand=0.25),
            _interval(12, 15, 0.0, 0.0, demand=0.25),
            _interval(12, 30, 0.30, 0.0),
        ]
    )

    blocked = _at(projection, 48)
    # The simulation still sees no drain, so the battery is not made to serve it.
    assert blocked["consumption_kwh"] == pytest.approx(0.0)
    assert blocked["battery_to_home_kwh"] == pytest.approx(0.0)
    # The published series says the house consumes anyway.
    assert blocked["consumption_forecast_kwh"] == pytest.approx(0.25)

    # An interval that was never blocked reports the same value on both channels.
    unblocked = _at(projection, 50)
    assert unblocked["consumption_kwh"] == pytest.approx(0.30)
    assert unblocked["consumption_forecast_kwh"] == pytest.approx(0.30)
    assert unblocked["battery_to_home_kwh"] > 0.0
