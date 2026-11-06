"""S3 — the solar period list must say where it came from and how old it is.

The panel can show which periods arrived, but not whether they are the forecast
the user configured or a leftover from the Energy dashboard, and not how long
ago they were fetched. A stale cache from the wrong provider looked exactly like
a fresh, correct one. The controller cache is ``(sensor, monotonic, periods)``,
so both answers were already available and simply were not published.

The periods themselves keep working exactly as before: provenance is read under
its own guard, so a cache whose timestamp cannot be parsed still publishes its
list.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import monotonic
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.binary_sensor import PredictiveChargingStatusSensor
from custom_components.omnibattery.solar_forecast import SolarForecastPeriod

_FIRST_PERIOD_START = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)


def _cached_periods(count: int = 2) -> tuple[SolarForecastPeriod, ...]:
    return tuple(
        SolarForecastPeriod(
            _FIRST_PERIOD_START + timedelta(minutes=15 * index),
            _FIRST_PERIOD_START + timedelta(minutes=15 * (index + 1)),
            0.5 if index else 0.4321,
        )
        for index in range(count)
    )


def _predictive_controller():
    """Minimal controller stub for the predictive-charging status attributes."""
    return SimpleNamespace(
        _daily_consumption_history=[],
        _daily_solar_energy_date=None,
        _daily_solar_forecast_initial_date=None,
        _daily_solar_forecast_initial_kwh=None,
        _dynamic_pricing_schedule=None,
        _household_accumulator_date=None,
        _household_energy_accumulator=0.0,
        _last_decision_data={},
        _last_chronological_diagnostics={},
        _predictive_charge_target_soc=None,
        _pricing_mgr=None,
        charging_time_slots=[],
        coordinators=[],
        last_evaluation_soc=None,
        max_contracted_power=7000,
        predictive_charging_mode="time_slot",
        predictive_charging_overridden=False,
        solar_forecast_diagnostic_source=None,
        solar_forecast_remaining_sensor="sensor.solar_remaining",
        solar_forecast_sensor=None,
        solar_forecast_source="remaining",
        _is_in_predictive_charging_slot=lambda: False,
    )


def _predictive_attrs(controller) -> dict:
    return PredictiveChargingStatusSensor(None, None, controller).extra_state_attributes


def test_the_cache_publishes_the_sensor_it_was_keyed_on():
    controller = _predictive_controller()
    controller._energy_solar_forecast = ("sensor.solcast", monotonic() - 120.0, _cached_periods())

    attrs = _predictive_attrs(controller)

    assert attrs["solar_forecast_periods_source"] == "sensor.solcast"
    assert attrs["solar_forecast_periods_age_s"] == pytest.approx(120.0, abs=5.0)


def test_the_age_never_goes_negative():
    """A cache stamped "after now" (clock step) must not publish a negative age."""
    controller = _predictive_controller()
    controller._energy_solar_forecast = ("sensor.solcast", monotonic() + 3600.0, _cached_periods())

    attrs = _predictive_attrs(controller)

    assert attrs["solar_forecast_periods_age_s"] == 0.0


def test_an_unreadable_age_does_not_cost_the_period_list():
    controller = _predictive_controller()
    controller._energy_solar_forecast = ("sensor.solcast", "not-a-number", _cached_periods())

    attrs = _predictive_attrs(controller)

    assert "solar_forecast_periods_source" not in attrs
    assert "solar_forecast_periods_age_s" not in attrs
    assert len(attrs["solar_forecast_periods"]) == 2


def test_no_cache_publishes_no_provenance():
    controller = _predictive_controller()

    attrs = _predictive_attrs(controller)

    assert "solar_forecast_periods_source" not in attrs
    assert "solar_forecast_periods_age_s" not in attrs
    assert "solar_forecast_periods" not in attrs


def test_the_provenance_stays_out_of_the_recorder():
    """The age changes on every read, so it may never enter recorder history."""
    unrecorded = PredictiveChargingStatusSensor._unrecorded_attributes

    assert "solar_forecast_periods_source" in unrecorded
    assert "solar_forecast_periods_age_s" in unrecorded
