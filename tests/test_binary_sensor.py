"""Tests for system-level binary sensor diagnostics."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.omnibattery.binary_sensor import (
    CurtailmentStatusSensor,
    DischargeReserveSensor,
    PredictiveChargingStatusSensor,
)
from custom_components.omnibattery.pricing import CurtailmentPlan
from custom_components.omnibattery.solar_forecast import SolarForecastPeriod


def _sensor(*, runtime_status: str, plan: CurtailmentPlan | None):
    controller = SimpleNamespace(
        smart_predischarge_enabled=True,
        _curtailment_runtime_status=runtime_status,
        _curtailment_runtime_reason="test",
        _curtailment_active_export_target_w=0.0,
        negative_injection_threshold=0.0,
        _curtailment_plan=plan,
    )
    return CurtailmentStatusSensor(None, None, controller)


def test_curtailment_attributes_expose_external_inverter_signal():
    plan = CurtailmentPlan(
        status="shortfall",
        reason="insufficient_pre_discharge_power_or_slots",
        required_headroom_kwh=4.0,
        current_headroom_kwh=2.5,
        shortfall_kwh=1.5,
    )

    attrs = _sensor(runtime_status="protected_window", plan=plan).extra_state_attributes

    assert attrs["protected_window_active"] is True
    assert attrs["headroom_deficit_kwh"] == 1.5
    assert attrs["inverter_curtailment_required"] is True


def test_curtailment_signal_is_unknown_without_safe_plan():
    attrs = _sensor(runtime_status="fail_safe", plan=None).extra_state_attributes

    assert attrs["protected_window_active"] is False
    assert attrs["inverter_curtailment_required"] is None


def test_predictive_attributes_omit_redundant_fields():
    controller = SimpleNamespace(
        _daily_consumption_history=[(date(2026, 8, 16), 7.5)],
        _daily_solar_energy_date=None,
        _daily_solar_forecast_initial_date=None,
        _daily_solar_forecast_initial_kwh=None,
        _dynamic_pricing_schedule=None,
        _household_accumulator_date=date(2026, 8, 17),
        _household_energy_accumulator=3.25,
        _last_decision_data={
            "grid_charge_kwh": 4.0,
            "min_reserve_kwh": 2.0,
            "planned_grid_charge_kwh": 1.5,
            "consumption_accumulator_ready": True,
            "consumption_accumulator_source": "daily_home_energy",
        },
        _last_chronological_diagnostics={
            "chronological_source": "profile",
            "solar_timeline_source": "learned",
            "solar_timeline_effective_kwh": 3.2,
            "solar_profile_mature": True,
            "solar_profile_days": 12,
            "solar_profile_coverage_ratio": 0.84,
            "minimum_projected_energy_kwh": 1.7,
            "minimum_projected_soc": 28.5,
            "chronological_plan_reason": "ok",
        },
        _predictive_charge_target_soc=None,
        _pricing_mgr=None,
        charging_time_slots=[],
        coordinators=[],
        last_evaluation_soc=42.0,
        max_contracted_power=7000,
        predictive_charging_mode="time_slot",
        predictive_charging_overridden=False,
        solar_forecast_diagnostic_source="remaining_sensor",
        solar_forecast_remaining_sensor="sensor.solar_remaining",
        solar_forecast_sensor=None,
        solar_forecast_source="remaining",
        _is_in_predictive_charging_slot=lambda: False,
    )

    attrs = PredictiveChargingStatusSensor(None, None, controller).extra_state_attributes

    assert attrs["solar_forecast_source"] == "remaining"
    assert attrs["household_consumption_full_day_kwh"] == 3.25
    assert attrs["daily_consumption_history"] == [("2026-08-16", 7.5)]
    assert attrs["planned_grid_charge_kwh"] == 1.5
    assert attrs["consumption_accumulator_source"] == "daily_home_energy"
    assert attrs["chronological_planning_active"] is False
    assert attrs["chronological_source"] == "profile"
    assert attrs["solar_timeline_source"] == "learned"
    assert attrs["solar_profile_days"] == 12
    assert attrs["minimum_projected_soc"] == 28.5
    assert "solar_shadow_selected_source" not in attrs

    redundant = {
        "consumption_accumulator_ready",
        "grid_charge_kwh",
        "history_days",
        "household_consumption_battery_window_kwh",
        "min_reserve_kwh",
        "solar_forecast_diagnostic_source",
        "solar_production_today_kwh",
    }
    assert redundant.isdisjoint(attrs)


def _predictive_controller(*, decision, chronological):
    """Controller stub for the predictive-charging status attributes."""
    return SimpleNamespace(
        _daily_consumption_history=[],
        _daily_solar_energy_date=None,
        _daily_solar_forecast_initial_date=None,
        _daily_solar_forecast_initial_kwh=None,
        _dynamic_pricing_schedule=None,
        _household_accumulator_date=None,
        _household_energy_accumulator=0.0,
        _last_decision_data=decision,
        _last_chronological_diagnostics=chronological,
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


def test_excluded_demand_claim_is_exposed():
    controller = _predictive_controller(
        decision={
            "excluded_demand_claim_kwh": 6.5,
            "solar_available_to_battery_kwh": 7.19,
        },
        chronological={},
    )

    attrs = PredictiveChargingStatusSensor(None, None, controller).extra_state_attributes

    assert attrs["excluded_demand_claim_kwh"] == 6.5
    assert attrs["solar_available_to_battery_kwh"] == 7.19


def test_excluded_demand_claim_falls_back_to_chronological_diagnostics():
    # Pre-slot and evening re-evaluations replace the decision dict without
    # running the chronological planner, so the persisted snapshot fills in.
    controller = _predictive_controller(
        decision={},
        chronological={
            "excluded_demand_claim_kwh": 6.5,
            "solar_available_to_battery_kwh": 7.19,
        },
    )

    attrs = PredictiveChargingStatusSensor(None, None, controller).extra_state_attributes

    assert attrs["excluded_demand_claim_kwh"] == 6.5
    assert attrs["solar_available_to_battery_kwh"] == 7.19


def test_excluded_demand_claim_is_not_recorded():
    unrecorded = PredictiveChargingStatusSensor._unrecorded_attributes

    assert "excluded_demand_claim_kwh" in unrecorded
    assert "solar_available_to_battery_kwh" in unrecorded


# --- Raw provider periods behind the solar timeline ------------------------
# The attribute key existed in _unrecorded_attributes but nothing ever filled
# it, so the panel could not show which periods arrived, how long they were or
# how much energy they carried. The source is the controller's Energy-dashboard
# cache: ``(sensor, monotonic, tuple[SolarForecastPeriod, ...])``.

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


def _predictive_attrs(controller) -> dict:
    return PredictiveChargingStatusSensor(None, None, controller).extra_state_attributes


def test_solar_forecast_periods_are_published_from_the_energy_cache():
    controller = _predictive_controller(decision={}, chronological={})
    controller._energy_solar_forecast = ("sensor.solcast", 1234.5, _cached_periods())

    attrs = _predictive_attrs(controller)

    assert attrs["solar_forecast_periods"] == [
        {
            "start": "2026-08-17T10:00:00+00:00",
            "end": "2026-08-17T10:15:00+00:00",
            "energy_kwh": 0.432,
        },
        {
            "start": "2026-08-17T10:15:00+00:00",
            "end": "2026-08-17T10:30:00+00:00",
            "energy_kwh": 0.5,
        },
    ]


def test_solar_forecast_periods_are_omitted_without_a_cache():
    controller = _predictive_controller(decision={}, chronological={})

    assert "solar_forecast_periods" not in _predictive_attrs(controller)


def test_solar_forecast_periods_are_omitted_above_the_cap():
    controller = _predictive_controller(decision={}, chronological={})
    controller._energy_solar_forecast = ("sensor.solcast", 1234.5, _cached_periods(101))

    assert "solar_forecast_periods" not in _predictive_attrs(controller)


def test_solar_forecast_periods_stay_out_of_the_recorder():
    assert (
        "solar_forecast_periods"
        in PredictiveChargingStatusSensor._unrecorded_attributes
    )


# --- Discharge reserve decision surface -----------------------------------


def _reserve_attrs(status: dict) -> dict:
    """Attributes of the reserve sensor over a manager publishing ``status``."""
    controller = SimpleNamespace(
        discharge_reserve_enabled=True,
        discharge_reserve_min_saving=0.1,
        _discharge_reserve_mgr=SimpleNamespace(get_status=lambda: dict(status)),
    )
    return DischargeReserveSensor(None, None, controller).extra_state_attributes


def test_discharge_reserve_publishes_the_energy_left_to_discharge():
    claims = [
        {
            "start": "2026-08-17T18:00:00+02:00",
            "price": 0.3,
            "claimed_kwh": 2.0,
            "pv_credit_kwh": 0.0,
            "expected_surplus_kwh": 0.0,
        }
    ]
    reserved = [
        {
            "start": "2026-08-17T18:00:00+02:00",
            "end": "2026-08-17T18:15:00+02:00",
            "price": 0.3,
            "net_demand_kwh": 2.0,
        }
    ]

    attrs = _reserve_attrs(
        {
            "state": "reserving",
            "reason": "reserved_for_dearer_hours",
            "reserve_soc_pct": 40.0,
            "reserve_kwh": 4.0,
            "usable_energy_kwh": 10.0,
            "threshold_price": 0.25,
            "reference_price": 0.19,
            "horizon_end": "2026-08-18T06:00:00+02:00",
            "claims": claims,
            "reserved_slots": reserved,
        }
    )

    assert attrs["usable_above_reserve_kwh"] == 6.0
    # The decision inputs that already existed must keep passing through.
    assert attrs["threshold_price"] == 0.25
    assert attrs["reference_price"] == 0.19
    assert attrs["horizon_end"] == "2026-08-18T06:00:00+02:00"
    assert attrs["claims"] == claims
    assert attrs["reserved_slots"] == reserved


def test_discharge_reserve_omits_the_remainder_without_a_plan():
    attrs = _reserve_attrs(
        {"state": "released", "reason": "no_plan", "reserve_soc_pct": 0.0}
    )

    assert "usable_above_reserve_kwh" not in attrs
    assert attrs["reserve_soc_pct"] == 0.0


def test_discharge_reserve_remainder_is_recorded_and_the_lists_are_not():
    """One float is small enough for history; the per-slot lists are not."""
    unrecorded = DischargeReserveSensor._unrecorded_attributes

    assert "usable_above_reserve_kwh" not in unrecorded
    assert {"claims", "reserved_slots"}.issubset(unrecorded)
