"""Publication vs simulation for a blocked quarter-hour (#616).

A price block or a no-discharge window zeroes the simulated drain and nets the
solar against the household demand.  The graph must still show the quarter's
real solar and consumption, without changing the simulated SOC.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.omnibattery.pricing.chronological import EnergyInterval
from custom_components.omnibattery.pricing.daily_timeline import BatteryProjectionInput
from custom_components.omnibattery.tracking.daily_projection import (
    DailyOperationProjectionRequest,
    build_daily_operation_projection,
)

START = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=15)


def _battery() -> BatteryProjectionInput:
    return BatteryProjectionInput(
        key="venus",
        stored_kwh=2.0,
        capacity_kwh=5.0,
        min_soc_pct=10.0,
        max_soc_pct=100.0,
        charge_power_w=3000.0,
        discharge_power_w=3000.0,
    )


def _project(intervals) -> dict:
    request = DailyOperationProjectionRequest(
        now=START,
        plan_intervals=tuple(intervals),
        allocations=(),
        battery_inputs=(_battery(),),
        mode="time_slot",
        decision_data={},
    )
    projection = build_daily_operation_projection(request)
    assert projection is not None
    return projection["intervals"][0]


def _blocked_interval() -> EnergyInterval:
    return EnergyInterval(START, END, 0.3, 0.8).with_battery_idle()


def test_blocked_interval_keeps_original_energy_as_publication_values():
    blocked = _blocked_interval()
    # The simulation keeps the netted values...
    assert blocked.consumption_kwh == 0.0
    assert blocked.solar_kwh == pytest.approx(0.5)
    # ...while the graph can show what the quarter really carried.
    assert blocked.demand_kwh == pytest.approx(0.3)
    assert blocked.solar_publication_kwh == pytest.approx(0.8)


def test_chained_block_preserves_publication_values():
    # The no-discharge window and the price block may both hit one interval.
    blocked = _blocked_interval().with_battery_idle()
    assert blocked.solar_kwh == pytest.approx(0.5)
    assert blocked.demand_kwh == pytest.approx(0.3)
    assert blocked.solar_publication_kwh == pytest.approx(0.8)


def test_projection_publishes_original_energy_for_blocked_quarter():
    item = _project([_blocked_interval()])
    assert item["solar_forecast_kwh"] == pytest.approx(0.8)
    assert item["consumption_forecast_kwh"] == pytest.approx(0.3)
    # The simulation series still carries the netted values.
    assert item["solar_kwh"] == pytest.approx(0.5)
    assert item["consumption_kwh"] == pytest.approx(0.0)


def test_blocked_simulation_matches_pre_patch_interval():
    # Exactly what v1.6.0b1 fed to the simulator: netted solar, zero demand,
    # no publication fields.
    pre_patch = EnergyInterval(START, END, 0.0, 0.5)
    pre = _project([pre_patch])
    post = _project([_blocked_interval()])

    assert post["soc_end_pct"] == pytest.approx(pre["soc_end_pct"])
    assert post["solar_to_battery_kwh"] == pytest.approx(pre["solar_to_battery_kwh"])
    assert post["stored_energy_end_kwh"] == pytest.approx(pre["stored_energy_end_kwh"])
    # Only the published forecast series changed.
    assert pre["solar_forecast_kwh"] == pytest.approx(0.5)
    assert pre["consumption_forecast_kwh"] == pytest.approx(0.0)
    assert post["solar_forecast_kwh"] == pytest.approx(0.8)
    assert post["consumption_forecast_kwh"] == pytest.approx(0.3)


def test_unblocked_quarter_is_unchanged():
    item = _project([EnergyInterval(START, END, 0.3, 0.8)])
    assert item["solar_forecast_kwh"] == pytest.approx(0.8)
    assert item["consumption_forecast_kwh"] == pytest.approx(0.3)
    assert item["solar_kwh"] == pytest.approx(0.8)
    assert item["consumption_kwh"] == pytest.approx(0.3)


def test_the_after_midnight_band_reads_the_published_forecast_too():
    """The extension band shows the same quarter as the chart before midnight.

    The panel reads the after-midnight intervals from ``extended_projection``,
    which drops every field the sensor does not whitelist.  Without the two
    publication fields there, a blocked quarter would fall back to the netted
    ``solar_kwh`` and read differently on either side of midnight.
    """
    from custom_components.omnibattery.sensor import _timeline_projection_extension

    item = _project([_blocked_interval()])
    extension = _timeline_projection_extension(
        [dict(item, index=0, extension_index=0)]
    )

    assert len(extension) == 1
    assert extension[0]["solar_forecast_kwh"] == pytest.approx(0.8)
    assert extension[0]["consumption_forecast_kwh"] == pytest.approx(0.3)
    # The netted simulation values still travel along for the flows.
    assert extension[0]["solar_kwh"] == pytest.approx(0.5)
    assert extension[0]["consumption_kwh"] == pytest.approx(0.0)
