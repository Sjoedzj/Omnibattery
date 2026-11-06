"""R4 — the evening recharge pass must not refuse silently or lie about it.

Four defects in one path:

* (a) every refusal logged "no slots below price threshold", even when the
  arbitrage margin — not the static ceiling — was the gate that bound;
* (b) the decision data kept ``should_charge=True`` and the full planned energy
  from before the refusal, so the dashboard and diagnostics promised a charge
  that no slot would execute;
* (c) a refusal sent no notification at all, which is indistinguishable from
  "nothing to do";
* (d) the guaranteed-floor exemption for the margin gate did not cover the
  weekly balance charge, so a weekly full charge was skipped exactly on the
  days the sun had fallen short.
"""
from __future__ import annotations

import asyncio
import datetime as datetime_module
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PRICE_INTEGRATION_NORDPOOL,
)
from custom_components.omnibattery.pricing import PriceSlot, notifications
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.pricing.calculations import (
    DEFAULT_ROUND_TRIP_EFFICIENCY,
)

NOW = datetime(2026, 9, 16, 8, 30)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(datetime_module, "datetime", FixedDateTime)
    monkeypatch.setattr(pricing_engine.calculations, "datetime", FixedDateTime)
    monkeypatch.setattr(pricing_engine, "datetime", FixedDateTime)


def _slot(hour, price):
    start = NOW.replace(hour=hour, minute=0)
    return PriceSlot(start, start + timedelta(hours=1), price)


def _controller(**overrides):
    async def _avg():
        return 8.0

    tracker = SimpleNamespace(
        get_dynamic_base_consumption=_avg,
        get_consumption_window_hours_per_day=lambda: 24.0,
        consumption_window_hours_in_range=lambda _a, _b: 6.0,
    )
    base = dict(
        coordinators=[
            SimpleNamespace(
                data={"battery_soc": 20.0, "battery_total_energy": 10.0},
                max_soc=100,
                min_soc=10,
            )
        ],
        _consumption_tracker=tracker,
        _last_decision_data={
            "solar_remaining_raw_kwh": 30.0,
            "solar_safety_margin_kwh": 2.0,
            "solar_remaining_effective_kwh": 28.0,
            "excluded_demand_claim_kwh": 0.0,
            "solar_available_to_battery_kwh": 28.0,
        },
        _dp_last_eval_soc=None,
        _dp_last_eval_excluded_claim_kwh=None,
        _external_loads=SimpleNamespace(claimable_solar_demand_kwh=lambda: 0.0),
        _daily_home_energy_date=None,
        _daily_home_energy_kwh=0.0,
        _household_accumulator_date=None,
        _household_energy_accumulator=0.0,
        # pricing state the manager reads
        _dynamic_pricing_schedule=None,
        _dynamic_pricing_evaluated_date=None,
        _dp_evening_reevaluated_date=None,
        _dp_daily_avg_price=None,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        dp_price_discharge_control=False,
        rt_price_discharge_control=False,
        price_sensor=None,
        price_integration_type=PRICE_INTEGRATION_NORDPOOL,
        max_price_threshold=None,
        discharge_price_threshold=None,
        min_arbitrage_margin=None,
        round_trip_efficiency=DEFAULT_ROUND_TRIP_EFFICIENCY,
        average_price_sensor=None,
        max_contracted_power=3000,
        max_charge_capacity=3000,
        remove_discharge_block=lambda source: None,
        set_discharge_block=lambda source, reason, details=None: None,
        _price_based_discharge_blocked=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _manager(ctrl, *, slots, weekly_gap_kwh=0.0):
    manager = PricingManager(SimpleNamespace(config=SimpleNamespace(time_zone="UTC")), ctrl)

    async def _noop_prices(force=False):
        return None

    manager._maybe_refresh_service_prices = _noop_prices
    manager._remaining_solar_today_kwh = lambda _now: 0.0
    manager.energy_horizon_end = lambda _now: NOW.replace(
        hour=0, minute=0
    ) + timedelta(days=1, hours=6)
    manager._parse_price_data = lambda horizon_end=None, **_kwargs: list(slots)

    async def _remaining(*, now=None, horizon_end=None):
        decision = dict(ctrl._last_decision_data)
        decision.update(
            {
                # 4.0 kWh left to serve, 1.0 kWh usable (20% above a 10% floor of
                # 10 kWh), no solar → a 3.0 kWh balance deficit.
                "remaining_consumption_kwh": 4.0,
                "consumption_rate_kwh_h": 0.6,
                "avg_consumption_kwh": 4.0,
                "solar_forecast_kwh": 0.0,
            }
        )
        return decision

    manager._evaluate_remaining_grid_charging = _remaining
    ctrl._weekly_full_charge_gap_kwh = lambda _coordinators: weekly_gap_kwh

    def build(*, now=None, slots=None, decision_data=None, price_ceiling=None, **_kw):
        allowed = [
            slot
            for slot in slots
            if price_ceiling is None or slot.price <= price_ceiling
        ]
        allocations = [
            SimpleNamespace(slot=allowed[0], planned_battery_kwh=1.0)
        ] if allowed else []
        return pricing_engine.ChronologicalPlan(allocations=allocations)

    manager._build_chronological_plan = build
    return manager


def _run(manager, ctrl):
    asyncio.run(manager._evaluate_evening_recharge())
    return ctrl._last_decision_data


# ---------------------------------------------------------------------------
# (a) the log must name the gate that actually bound
# ---------------------------------------------------------------------------


def test_a_margin_refusal_names_the_margin(caplog):
    # 0.50 * 0.85 - 0.05 = 0.375: neither remaining slot can pay for itself.
    evening, late = _slot(18, 0.51), _slot(19, 0.49)
    ctrl = _controller(min_arbitrage_margin=0.05)
    manager = _manager(ctrl, slots=[evening, late])
    manager._send_evening_recharge_notification = _async_noop_recorder([])

    with caplog.at_level(logging.WARNING):
        _run(manager, ctrl)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a refusal must be logged"
    message = [m for m in warnings if "Evening recharge" in m][-1]
    assert "arbitrage margin" in message
    assert "expected discharge" in message
    # The numbers that caused it, not a generic phrase.
    assert "0.3750" in message
    assert "round-trip 0.85" in message
    assert "0.0500" in message
    assert "no slots below price threshold" not in message


def test_a_threshold_refusal_names_the_configured_ceiling(caplog):
    evening, late = _slot(18, 0.51), _slot(19, 0.49)
    # No margin gate at all: the user's own ceiling is what binds.
    ctrl = _controller(max_price_threshold=0.30)
    manager = _manager(ctrl, slots=[evening, late])
    manager._send_evening_recharge_notification = _async_noop_recorder([])

    with caplog.at_level(logging.WARNING):
        _run(manager, ctrl)

    message = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "Evening recharge" in r.getMessage()
    ][-1]
    assert "configured ceiling" in message
    assert "0.3000" in message
    assert "0.4900" in message  # the cheapest remaining slot
    assert "arbitrage margin" not in message
    assert "no slots below price threshold" not in message


def _async_noop_recorder(calls):
    async def _send(deficit_kwh, slots, *, refusal=None):
        calls.append({"deficit": deficit_kwh, "slots": slots, "refusal": refusal})
    return _send


# ---------------------------------------------------------------------------
# (b) the decision must stop claiming a charge that was not armed
# ---------------------------------------------------------------------------


def test_a_refused_plan_no_longer_reports_should_charge():
    evening, late = _slot(18, 0.51), _slot(19, 0.49)
    ctrl = _controller(min_arbitrage_margin=0.05)
    manager = _manager(ctrl, slots=[evening, late])
    manager._send_evening_recharge_notification = _async_noop_recorder([])

    decision = _run(manager, ctrl)

    assert decision["should_charge"] is False
    assert decision["planned_grid_charge_kwh"] == 0.0
    assert decision["evening_recharge_refused"] is True
    assert decision["evening_recharge_refusal_reason"] == "arbitrage_margin"
    # The demand itself is not denied — it is the purchase that was refused.
    assert decision["energy_deficit_kwh"] == pytest.approx(3.0)
    assert decision["evening_recharge_intent_kwh"] > 0.0
    # Diagnostics only surface ``reason`` when ``should_charge`` is False, so a
    # missing reason would leave the refusal unexplained there as well.
    assert decision["reason"] == "Evening recharge refused: arbitrage margin"


def test_the_plan_is_still_armed_when_no_gate_refuses_it():
    """The counterpart: a healthy slot keeps the pre-existing reporting."""
    evening, late = _slot(18, 0.11), _slot(19, 0.49)
    ctrl = _controller(min_arbitrage_margin=0.05, max_price_threshold=0.60)
    manager = _manager(ctrl, slots=[evening, late])
    manager._send_evening_recharge_notification = _async_noop_recorder([])

    decision = _run(manager, ctrl)

    assert decision["should_charge"] is True
    assert not decision.get("evening_recharge_refused")
    assert ctrl._dynamic_pricing_schedule.selected_slots == [evening]


# ---------------------------------------------------------------------------
# (c) a refusal must reach the user
# ---------------------------------------------------------------------------


def test_a_refusal_is_announced_with_its_cause():
    evening, late = _slot(18, 0.51), _slot(19, 0.49)
    ctrl = _controller(min_arbitrage_margin=0.05)
    manager = _manager(ctrl, slots=[evening, late])
    calls: list = []
    manager._send_evening_recharge_notification = _async_noop_recorder(calls)

    _run(manager, ctrl)

    assert len(calls) == 1
    refusal = calls[0]["refusal"]
    assert refusal is not None, "a refusal must be announced, not swallowed"
    assert refusal["reason"] == "arbitrage_margin"
    assert refusal["ceiling"] == pytest.approx(0.375)
    assert refusal["margin"] == pytest.approx(0.05)
    assert calls[0]["deficit"] == pytest.approx(3.0)


def test_the_refusal_message_keeps_the_evening_identity():
    """Same title and deficit line: it must replace the earlier message."""
    title, message = notifications.format_evening_recharge_notification(
        3.0,
        [],
        unit="€/kWh",
        avg_soc=42.0,
        refusal={
            "reason": "arbitrage_margin",
            "ceiling": 0.375,
            "expected_discharge_price": 0.5,
            "margin": 0.05,
            "round_trip_efficiency": 0.85,
            "planned_kwh": 7.0,
        },
    )

    assert title == "Predictive Charging: Evening re-evaluation"
    assert "42% avg" in message
    assert "Deficit: 3.00 kWh" in message
    assert "arbitrage margin" in message
    assert "0.3750 €/kWh" in message
    assert "7.00 kWh left unpurchased" in message
    # Crucially it must not claim a schedule.
    assert "Cheap slots scheduled" not in message


def test_the_threshold_refusal_message_names_the_ceiling():
    _title, message = notifications.format_evening_recharge_notification(
        3.0,
        [],
        unit="€/kWh",
        avg_soc=42.0,
        refusal={
            "reason": "price_threshold",
            "ceiling": 0.30,
            "expected_discharge_price": 0.52,
            "planned_kwh": 7.0,
        },
    )

    assert "above the ceiling 0.3000 €/kWh" in message
    assert "0.5200 €/kWh" in message


def test_the_sender_keeps_the_notification_id_for_a_refusal():
    """Blueprints key on the ID; a refusal may not create a new notification."""
    calls: list = []

    class _Recorder:
        async def __call__(self, title, message, notification_id):
            calls.append((title, message, notification_id))

    ctrl = _controller()
    manager = PricingManager(SimpleNamespace(config=SimpleNamespace(time_zone="UTC")), ctrl)
    manager._async_create_predictive_notification = _Recorder()

    asyncio.run(
        manager._send_evening_recharge_notification(
            3.0, [], refusal={"reason": "price_threshold", "ceiling": 0.30}
        )
    )

    assert len(calls) == 1
    title, message, notification_id = calls[0]
    assert title == "Predictive Charging: Evening re-evaluation"
    assert notification_id.endswith("predictive_charging_evening_reeval")
    assert "Not charging" in message


# ---------------------------------------------------------------------------
# (d) the weekly balance charge is not an optimisation
# ---------------------------------------------------------------------------


def test_a_weekly_balance_charge_survives_the_arbitrage_margin():
    """The margin gate may not skip the one full charge the pack needs."""
    evening, late = _slot(18, 0.51), _slot(19, 0.49)
    ctrl = _controller(min_arbitrage_margin=0.05, max_price_threshold=0.60)
    # A 3.5 kWh weekly gap dominates the 3.0 kWh balance term: the pack is due
    # its BMS balance charge and the sun has not been able to deliver it.
    manager = _manager(ctrl, slots=[evening, late], weekly_gap_kwh=3.5)
    manager._send_evening_recharge_notification = _async_noop_recorder([])

    decision = _run(manager, ctrl)

    assert not decision.get("evening_recharge_refused")
    assert ctrl._dynamic_pricing_schedule.selected_slots == [evening]
    assert ctrl._dynamic_pricing_schedule.deficit_charging_needed is True


def test_a_weekly_gap_smaller_than_the_balance_still_respects_the_margin():
    """The exemption is for the weekly driver only, not for the gate itself."""
    evening, late = _slot(18, 0.51), _slot(19, 0.49)
    ctrl = _controller(min_arbitrage_margin=0.05, max_price_threshold=0.60)
    # 0.5 kWh < 3.0 kWh: tonight's deficit is ordinary household demand, so the
    # arbitrage margin stays authoritative.
    manager = _manager(ctrl, slots=[evening, late], weekly_gap_kwh=0.5)
    manager._send_evening_recharge_notification = _async_noop_recorder([])

    decision = _run(manager, ctrl)

    assert decision["evening_recharge_refused"] is True
    assert decision["evening_recharge_refusal_reason"] == "arbitrage_margin"
