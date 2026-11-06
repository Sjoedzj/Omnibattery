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


# --- What the remainder actually is ---------------------------------------
#
# ``usable_above_reserve_kwh`` used to stand alone, which read as a fleet-wide
# energy band. It is neither: it is computed on the counted capacity only, and
# once every usable kWh is claimed it is not a quantity of its own at all. The
# tests below pin the two figures that make that visible in the state.


def test_discharge_reserve_names_the_capacity_the_kwh_figures_rest_on():
    """One battery of two is counted, so the band halves; say so in the state."""
    attrs = _reserve_attrs(
        {
            "state": "reserving",
            "reason": "reserved_for_dearer_hours",
            "reserve_soc_pct": 8.46,
            "reserve_kwh": 0.432,
            "usable_energy_kwh": 0.865,
            "total_capacity_kwh": 5.12,
            "claimed_kwh": 0.865,
            "pv_credit_kwh": 0.433,
        }
    )

    assert attrs["counted_capacity_kwh"] == 5.12
    assert attrs["usable_above_reserve_kwh"] == 0.433


def test_discharge_reserve_remainder_is_the_pv_credit_once_everything_is_claimed():
    """Saturated: reserve_kwh = usable - pv_credit, so the remainder is the sun."""
    usable, credit = 0.865, 0.433
    claimed = usable
    attrs = _reserve_attrs(
        {
            "state": "reserving",
            "reason": "reserved_for_dearer_hours",
            "reserve_soc_pct": 8.46,
            "total_capacity_kwh": 5.12,
            "usable_energy_kwh": usable,
            "claimed_kwh": claimed,
            # What the manager publishes: claimed_kwh minus pv_credit_kwh.
            "reserve_kwh": round(claimed - credit, 3),
            "pv_credit_kwh": credit,
        }
    )

    assert attrs["usable_above_reserve_kwh"] == credit
    assert attrs["usable_above_reserve_is_pv_credit"] is True


def test_discharge_reserve_remainder_is_more_than_the_pv_credit_while_unsatisfied():
    """Below saturation the remainder is energy of its own, so the flag is off."""
    attrs = _reserve_attrs(
        {
            "state": "reserving",
            "reason": "reserved_for_dearer_hours",
            "reserve_soc_pct": 40.0,
            "usable_energy_kwh": 10.0,
            "claimed_kwh": 4.0,
            "reserve_kwh": 4.0,
            "pv_credit_kwh": 0.0,
        }
    )

    assert attrs["usable_above_reserve_kwh"] == 6.0
    assert attrs["usable_above_reserve_is_pv_credit"] is False


def test_discharge_reserve_omits_the_basis_and_the_flag_without_a_plan():
    attrs = _reserve_attrs(
        {"state": "released", "reason": "no_plan", "reserve_soc_pct": 0.0}
    )

    assert "counted_capacity_kwh" not in attrs
    assert "usable_above_reserve_is_pv_credit" not in attrs


def test_discharge_reserve_publishes_the_enforced_floor_per_battery():
    """The controller blocks on min_soc + reserve points, per battery."""
    controller = SimpleNamespace(
        discharge_reserve_enabled=True,
        discharge_reserve_min_saving=0.1,
        _discharge_reserve_mgr=SimpleNamespace(get_status=lambda: {}),
        get_battery_discharge_blockers=lambda: {
            "battery-1": {
                "price_reserve": {
                    "reason": "price_reserve",
                    "details": {
                        "battery": "battery-1",
                        "soc": 42.5,
                        "min_soc": 10.0,
                        "reserved_floor": 50.0,
                        "reserve_soc_pct": 40.0,
                    },
                    "since": "2026-08-17T18:00:00+00:00",
                }
            },
            # A block of another kind, and one without a numeric floor: neither
            # belongs in the per-battery reserve floor.
            "battery-2": {"cell_balance": {"details": {"cell": 3}}},
            "battery-3": {"price_reserve": {"details": {"reserved_floor": None}}},
        },
    )

    attrs = DischargeReserveSensor(None, None, controller).extra_state_attributes

    assert attrs["reserved_floor_pct_by_battery"] == {"battery-1": 50.0}


def test_discharge_reserve_floors_are_omitted_without_a_price_reserve_block():
    """A battery above its floor carries no block, so there is no floor to show."""
    attrs = _reserve_attrs(
        {
            "state": "reserving",
            "reason": "reserved_for_dearer_hours",
            "reserve_soc_pct": 8.46,
            "usable_energy_kwh": 0.865,
            "reserve_kwh": 0.432,
            "total_capacity_kwh": 5.12,
        }
    )

    assert "reserved_floor_pct_by_battery" not in attrs


def test_discharge_reserve_floors_never_raise_from_the_blocker_read():
    """Diagnostics must not take the entity down with the registry."""

    def _boom():
        raise RuntimeError("registry unavailable")

    controller = SimpleNamespace(
        discharge_reserve_enabled=True,
        discharge_reserve_min_saving=0.1,
        _discharge_reserve_mgr=SimpleNamespace(get_status=lambda: {}),
        get_battery_discharge_blockers=_boom,
    )

    attrs = DischargeReserveSensor(None, None, controller).extra_state_attributes

    assert "reserved_floor_pct_by_battery" not in attrs


def test_discharge_reserve_small_new_figures_stay_recorded():
    """The new figures are scalars and one short map, so they belong in history."""
    unrecorded = DischargeReserveSensor._unrecorded_attributes

    assert {
        "counted_capacity_kwh",
        "usable_above_reserve_is_pv_credit",
        "reserved_floor_pct_by_battery",
    }.isdisjoint(unrecorded)
    # Only the two per-slot lists are excluded, and nothing was added here.
    assert unrecorded == frozenset({"reserved_slots", "claims"})
