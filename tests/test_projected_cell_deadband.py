"""R1 — the display dead-band must reach the *projected* action mask.

``tracking/daily_timeline.py`` already refuses to colour a quarter for a flow
below ``ACTION_ENERGY_DEADBAND_KWH`` (commit 309e753d), but the projected mask
the tracking layer OR's its own bits into is composed in
``pricing/daily_timeline.py`` and used ``_EPSILON`` (1e-9).  A 1.6 Wh discharge
at a quarter boundary therefore arrived with ``ACTION_DISCHARGE`` already set,
and since the tracking pass only adds bits it could never take that back: the
dashboard showed a full discharge quarter that the dead-band said was nothing.

The fix applies the shared threshold where the bit is *composed*, so there is
one definition (``const.ACTION_ENERGY_DEADBAND_KWH``) and not two numbers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.omnibattery.const import ACTION_ENERGY_DEADBAND_KWH
from custom_components.omnibattery.pricing.daily_timeline import (
    ACTION_DISCHARGE,
    ACTION_MASK_ALL,
    BatteryProjectionInput,
    ProjectionIntervalInput,
    actions_for_mask,
    simulate_battery_projection,
)
from custom_components.omnibattery.tracking import daily_timeline as tracking_timeline

UTC = timezone.utc
BASE = datetime(2026, 8, 23, tzinfo=UTC)
QUARTER = timedelta(minutes=15)


def _project(consumption_kwh: float):
    intervals = [
        ProjectionIntervalInput(
            start=BASE,
            end=BASE + QUARTER,
            consumption_kwh=consumption_kwh,
            solar_kwh=0.0,
        )
    ]
    batteries = [
        BatteryProjectionInput(
            key="battery-a",
            stored_kwh=2.0,
            capacity_kwh=10.0,
            min_soc_pct=0.0,
            max_soc_pct=100.0,
            charge_power_w=4000.0,
            discharge_power_w=4000.0,
        )
    ]
    return simulate_battery_projection(intervals, batteries).flows[0]


def test_the_threshold_has_exactly_one_definition():
    """Pricing and tracking must read the same constant, not a copy of it."""
    assert (
        tracking_timeline._ACTION_ENERGY_DEADBAND_KWH
        is ACTION_ENERGY_DEADBAND_KWH
    )


def test_a_three_watt_hour_residue_no_longer_reaches_the_projected_mask():
    """The reported case: 3 Wh of discharge is not a discharge quarter."""
    flow = _project(0.003)

    # The flow is real, so the energy itself must survive untouched.
    assert flow.battery_to_home_kwh == pytest.approx(0.003)
    assert flow.action_mask & ACTION_DISCHARGE == 0
    assert actions_for_mask(flow.action_mask) == ()


def test_a_one_point_six_watt_hour_residue_never_reaches_the_tracking_cell():
    """1.6 Wh was the measured case that came in as a full discharge."""
    flow = _project(0.0016)

    assert flow.action_mask & ACTION_DISCHARGE == 0
    # The tracking pass only OR's bits on top, so a composed mask of 0 is the
    # only thing that can keep the dead-band's promise.
    assert flow.action_mask | 0 == 0


def test_the_deadband_boundary_stays_exclusive_on_the_projection_side():
    assert _project(ACTION_ENERGY_DEADBAND_KWH).action_mask & ACTION_DISCHARGE == 0
    assert (
        _project(ACTION_ENERGY_DEADBAND_KWH + 0.0001).action_mask & ACTION_DISCHARGE
    ) == ACTION_DISCHARGE


def test_a_real_discharge_still_colours_the_projected_quarter():
    flow = _project(0.35)

    assert flow.action_mask == ACTION_DISCHARGE
    assert flow.action_mask & ACTION_MASK_ALL == ACTION_DISCHARGE
