"""A flow below the display dead-band must not colour a whole quarter.

Three watt-hours of spill at a quarter boundary is a real measurement, but
painting the full cell as a grid charge reads as a bug: the charge ran for about
three seconds into the quarter. The mask derivation used ``_EPSILON``, a
numerical guard of 1e-9, so any measured spill coloured the cell. It now uses a
display dead-band.
"""
from __future__ import annotations

from custom_components.omnibattery.pricing.daily_timeline import (
    ACTION_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_SOLAR_CHARGE,
)
from custom_components.omnibattery.tracking.daily_timeline import (
    DailyOperationTimelineManager,
)

mask_from = DailyOperationTimelineManager._action_mask_from_decision


def test_a_three_watt_hour_residue_no_longer_colours_a_quarter():
    # The observed case: the 11:30 charge stopped just after 11:45.
    assert mask_from({"grid_to_battery_kwh": 0.003}) == 0


def test_the_deadband_boundary_is_exclusive():
    assert mask_from({"grid_to_battery_kwh": 0.01}) == 0
    assert mask_from({"grid_to_battery_kwh": 0.0101}) == ACTION_GRID_CHARGE


def test_a_real_charge_still_shows():
    assert mask_from({"grid_to_battery_kwh": 1.2}) == ACTION_GRID_CHARGE


def test_the_deadband_covers_every_flow():
    assert mask_from({"solar_to_battery_kwh": 0.003}) == 0
    assert mask_from({"battery_to_home_kwh": 0.003}) == 0
    assert mask_from({"solar_to_battery_kwh": 0.5}) == ACTION_SOLAR_CHARGE
    assert mask_from({"battery_to_home_kwh": 0.5}) == ACTION_DISCHARGE


def test_a_named_action_is_not_a_measured_residue():
    """The dead-band only governs bits derived from energy values.

    A decision that names its action outright is a statement, not a measurement,
    so it keeps colouring the quarter however small the flow turns out to be.
    """
    assert mask_from({"action_mask": ACTION_GRID_CHARGE}) == ACTION_GRID_CHARGE
