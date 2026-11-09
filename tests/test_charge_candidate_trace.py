"""Tests for the published charge-candidate trace (measurement only).

The trace explains, per quarter-hour, why the chronological planner did or did
not take a price slot as a grid-charge candidate.  These tests pin three
things: the verdicts are correct, the diagnostic payload is bounded and
JSON-safe, and collecting the trace cannot change a single allocation.
"""
from datetime import datetime, timedelta

import pytest

from custom_components.omnibattery import diagnostics
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing import chronological
from custom_components.omnibattery.pricing.chronological import (
    EnergyInterval,
    allocate_price_slots,
    build_energy_deadlines,
)

BASE = datetime(2026, 8, 18)
HORIZON = BASE + timedelta(days=1)


def _interval(index, consumption, solar=0.0):
    start = BASE + timedelta(minutes=15 * index)
    return EnergyInterval(start, start + timedelta(minutes=15), consumption, solar)


def _slot(start_minutes, end_minutes, price):
    return PriceSlot(
        BASE + timedelta(minutes=start_minutes),
        BASE + timedelta(minutes=end_minutes),
        price,
    )


def _quarter(index, price):
    return _slot(15 * index, 15 * index + 15, price)


def _records(plan):
    return plan.candidate_trace["records"]


def _record(plan, index):
    return _records(plan)[index]


def _record_at(plan, iso_start):
    for record in _records(plan):
        if record.get("slot_start") == iso_start:
            return record
    raise AssertionError(f"no trace record for {iso_start}")


def _day(solar_total_kwh=0.0):
    """The #704 shape: pre-dawn window, morning load, midday sun, evening load."""
    intervals = []
    for index in range(96):
        hour = index / 4
        consumption = 0.0 if hour < 8 else 0.125 if hour < 17 else 0.2
        solar = solar_total_kwh / 20 if 12 <= hour < 17 else 0.0
        intervals.append(_interval(index, consumption, solar))
    return intervals


def _night_plan(solar_total_kwh=0.0, **overrides):
    intervals = _day(solar_total_kwh)
    slots = [_quarter(index, 0.10) for index in range(96)]
    arguments = {
        "total_required_kwh": 4.51,
        "effective_power_kw": 2.5,
        "charge_efficiency": 1.0,
        "now": BASE,
        "horizon_end": HORIZON,
        "headroom_kwh": 4.51,
        "usable_initial_kwh": 0.61,
        "max_price_threshold": 0.20,
    }
    arguments.update(overrides)
    deadlines = build_energy_deadlines(
        intervals, usable_initial_kwh=0.61, usable_capacity_kwh=5.12
    )
    return intervals, allocate_price_slots(intervals, deadlines, slots, **arguments)


# ----------------------------------------------------------------------
# Verdicts
# ----------------------------------------------------------------------
def test_chosen_quarter_reports_chosen_with_its_energy_and_no_block():
    _, plan = _night_plan()

    chosen = [record for record in _records(plan) if record["verdict"] == "chosen"]
    assert chosen, "the scenario must charge something"
    for record in chosen:
        assert record["price"] == pytest.approx(0.10)
        assert record["ceiling"] == pytest.approx(0.20)
        assert record["slot_start"] is not None
        # A chosen quarter can still have blocked energy: the plan asked for
        # less than the room allowed.  The accounting must stay consistent.
        assert record["blocked_kwh"] >= 0.0
        assert record["usable_kwh"] > 0.0
    # The room rule let at least the allocated energy through, or this quarter
    # could not have been charged at all.
    allowed = sum(record["usable_kwh"] for record in chosen)
    assert allowed >= plan.allocated_kwh


def test_expensive_quarter_reports_too_expensive_against_the_applied_ceiling():
    expensive = [_quarter(index, 0.65) for index in range(96)]
    intervals = _day()
    deadlines = build_energy_deadlines(
        intervals, usable_initial_kwh=0.61, usable_capacity_kwh=5.12
    )
    plan = allocate_price_slots(
        intervals,
        deadlines,
        expensive,
        total_required_kwh=4.51,
        effective_power_kw=2.5,
        charge_efficiency=1.0,
        now=BASE,
        horizon_end=HORIZON,
        headroom_kwh=4.51,
        usable_initial_kwh=0.61,
        max_price_threshold=0.20,
    )

    assert plan.allocations == []
    assert {record["verdict"] for record in _records(plan)} == {"too_expensive"}
    for record in _records(plan):
        assert record["ceiling"] == pytest.approx(0.20)
        assert record["price"] > record["ceiling"]
        assert record["decided_by"] is None


def test_expensive_quarter_reports_the_per_slot_arbitrage_ceiling():
    slots = [_quarter(0, 0.30), _quarter(1, 0.50)]
    ceilings = {slots[0].start: 0.35, slots[1].start: 0.40}
    intervals = [_interval(7, 5.0)]
    plan = allocate_price_slots(
        intervals,
        build_energy_deadlines(intervals, usable_initial_kwh=0.0),
        slots,
        total_required_kwh=5.0,
        effective_power_kw=2.0,
        now=BASE,
        horizon_end=BASE + timedelta(hours=12),
        max_price_threshold=0.60,
        slot_ceilings=ceilings,
    )

    assert _record(plan, 0)["verdict"] == "chosen"
    assert _record(plan, 0)["ceiling"] == pytest.approx(0.35)
    assert _record(plan, 1)["verdict"] == "too_expensive"
    assert _record(plan, 1)["ceiling"] == pytest.approx(0.40)


def test_unpublishable_per_slot_ceiling_is_reported_as_not_offered():
    """An infinite arbitrage ceiling is not a price cap a user can compare."""
    slots = [_quarter(0, 0.30), _quarter(1, 0.50)]
    ceilings = {slots[0].start: 0.35, slots[1].start: float("-inf")}
    intervals = [_interval(7, 5.0)]
    plan = allocate_price_slots(
        intervals,
        build_energy_deadlines(intervals, usable_initial_kwh=0.0),
        slots,
        total_required_kwh=5.0,
        effective_power_kw=2.0,
        now=BASE,
        horizon_end=BASE + timedelta(hours=12),
        max_price_threshold=0.60,
        slot_ceilings=ceilings,
    )

    assert _record(plan, 1)["verdict"] == "other:not_offered"
    assert _record(plan, 1)["ceiling"] is None


def test_quarter_the_room_rule_refuses_reports_no_room():
    # ValueError: one big solar quarter fills a 1 kWh battery by itself, so the
    # quarters before it have no room left even though their price is fine.
    intervals = [
        _interval(0, 0.0),
        _interval(1, 0.0, 3.0),
        _interval(2, 0.0),
        _interval(3, 0.5),
    ]
    slots = [_quarter(index, 0.10) for index in range(4)]
    deadlines = build_energy_deadlines(
        intervals, usable_initial_kwh=0.0, usable_capacity_kwh=1.0
    )
    plan = allocate_price_slots(
        intervals,
        deadlines,
        slots,
        total_required_kwh=0.5,
        effective_power_kw=4.0,
        now=BASE,
        horizon_end=HORIZON,
        usable_initial_kwh=0.0,
        headroom_kwh=1.0,
    )

    assert [record["verdict"] for record in _records(plan)] == [
        "no_room",
        "no_room",
        "no_room",
        "chosen",
    ]
    assert _record(plan, 0)["usable_kwh"] == pytest.approx(0.0)
    assert _record(plan, 0)["room_kwh"] == pytest.approx(0.0)
    assert _record(plan, 0)["blocked_kwh"] > 0.0


def test_past_and_out_of_horizon_quarters_are_named():
    slots = [_quarter(0, 0.10), _quarter(1, 0.10), _quarter(2, 0.10)]
    intervals = [_interval(3, 0.5)]
    deadlines = build_energy_deadlines(intervals, usable_initial_kwh=0.0)
    plan = allocate_price_slots(
        intervals,
        deadlines,
        slots,
        total_required_kwh=0.5,
        effective_power_kw=4.0,
        now=BASE + timedelta(minutes=20),
        horizon_end=BASE + timedelta(minutes=30),
        usable_initial_kwh=0.0,
        headroom_kwh=1.0,
    )

    assert _record(plan, 0)["verdict"] == "past"
    assert _record(plan, 1)["verdict"] == "too_expensive" or (
        _record(plan, 1)["verdict"] == "chosen"
    )
    assert _record(plan, 2)["verdict"] == "out_of_horizon"


def test_quarter_with_a_solar_surplus_is_not_automatically_no_room():
    """A sun-filled quarter is judged by its own room, not by that sun alone.

    Four quarters: the load needs half a kWh in the first one, a 3 kWh solar
    surplus lands in the second, and nothing else happens.  The cheap pre-solar
    quarter is charged exactly as before #704: it gets the half kWh it needs and
    the later sun does not veto it.  The *second* quarter reports ``no_room``
    only because the battery it filled would already be full there, which is a
    later-fill-point rule, not a same-quarter solar rule.
    """
    intervals = [
        _interval(0, 0.5),
        _interval(1, 0.0, 3.0),
        _interval(2, 0.0),
        _interval(3, 0.0),
    ]
    slots = [_quarter(index, 0.10) for index in range(4)]
    deadlines = build_energy_deadlines(
        intervals, usable_initial_kwh=0.0, usable_capacity_kwh=1.0
    )
    plan = allocate_price_slots(
        intervals,
        deadlines,
        slots,
        total_required_kwh=0.5,
        effective_power_kw=2.5,
        charge_efficiency=1.0,
        now=BASE,
        horizon_end=HORIZON,
        usable_initial_kwh=0.0,
        headroom_kwh=1.0,
    )

    pre_solar = _record(plan, 0)
    assert pre_solar["verdict"] == "chosen"
    assert pre_solar["room_kwh"] == pytest.approx(0.5)
    assert plan.allocations[0].slot.start.minute == 0
    solar_quarter = _record(plan, 1)
    assert solar_quarter["verdict"] == "no_room"
    assert solar_quarter["usable_kwh"] == pytest.approx(0.0)


def test_quarters_are_published_without_a_charge_deficit():
    intervals = _day()
    deadlines = build_energy_deadlines(
        intervals, usable_initial_kwh=5.0, usable_capacity_kwh=5.12
    )
    slots = [_quarter(index, 0.10) for index in range(96)]
    plan = allocate_price_slots(
        intervals,
        deadlines,
        slots,
        total_required_kwh=0.0,
        effective_power_kw=2.5,
        now=BASE,
        horizon_end=HORIZON,
        headroom_kwh=0.12,
        usable_initial_kwh=5.0,
        max_price_threshold=0.20,
    )

    assert plan.allocations == []
    assert len(_records(plan)) == 96
    assert {record["verdict"] for record in _records(plan)} == {"not_needed"}


def test_quarter_without_a_finite_price_is_dropped_and_never_becomes_zero():
    """A quarter with an unknown price is not part of the planner's world.

    FFunes' ``allocate_price_slots`` filters non-finite prices before it builds
    candidate lists, so such a slot never reaches the allocation and the trace
    holds no record for it. That is the honest outcome: the quarter is a gap,
    and a gap must never be published as a price of 0 (free electricity).
    """
    intervals = [_interval(3, 0.5)]
    slots = [PriceSlot(BASE, BASE + timedelta(minutes=15), float("nan"))]
    deadlines = build_energy_deadlines(intervals, usable_initial_kwh=0.0)
    plan = allocate_price_slots(
        intervals,
        deadlines,
        slots,
        total_required_kwh=0.5,
        effective_power_kw=4.0,
        now=BASE,
        horizon_end=HORIZON,
        usable_initial_kwh=0.0,
        headroom_kwh=1.0,
        max_price_threshold=0.20,
    )

    assert plan.candidate_trace["records"] == []
    # Nothing was allocated on the unpriced quarter either.
    assert plan.allocations == []
    assert plan.total_required_kwh == pytest.approx(0.5)


def test_blocked_kwh_adds_up_to_the_energy_the_room_rule_held_back():
    intervals = _day(7.0)
    deadlines = build_energy_deadlines(
        intervals, usable_initial_kwh=0.61, usable_capacity_kwh=5.12
    )
    slots = [_slot(60, 480, 0.1)]
    plan = allocate_price_slots(
        intervals,
        deadlines,
        slots,
        total_required_kwh=4.51,
        effective_power_kw=2.5,
        charge_efficiency=1.0,
        now=BASE,
        horizon_end=HORIZON,
        headroom_kwh=4.51,
        usable_initial_kwh=0.61,
    )

    # #704: the single night slot is cut back by the midday solar fill.
    assert plan.allocated_kwh == pytest.approx(2.01)
    assert len(_records(plan)) == 1
    record = _record_at(plan, "2026-08-18T01:00:00")
    assert record["verdict"] == "chosen"
    # 7 hours at 2.5 kW of usable slot capacity; the solar fill holds back the
    # rest of what the room rule allows through.
    assert record["usable_kwh"] == pytest.approx(2.01)
    assert record["blocked_kwh"] == pytest.approx(15.49 - 2.01, abs=1e-6)
    assert record["room_kwh"] == pytest.approx(2.01)
    assert record["decided_by"] == "deadline:depletion"


# ----------------------------------------------------------------------
# No behaviour change
# ----------------------------------------------------------------------
def test_collecting_the_trace_does_not_change_the_plan(monkeypatch):
    scenarios = [
        dict(),
        dict(solar_total_kwh=7.0),
        dict(max_price_threshold=None),
        dict(max_price_threshold=0.05),
        dict(premium_kwh=1.0, premium_price_threshold=0.65),
    ]
    for overrides in scenarios:
        intervals, traced = _night_plan(**overrides)

        monkeypatch.setattr(chronological, "_CANDIDATE_TRACE_ENABLED", False)
        try:
            untraced = allocate_price_slots(
                intervals,
                build_energy_deadlines(
                    intervals, usable_initial_kwh=0.61, usable_capacity_kwh=5.12
                ),
                [_quarter(index, 0.10) for index in range(96)],
                total_required_kwh=overrides.get("total_required_kwh", 4.51),
                effective_power_kw=2.5,
                charge_efficiency=1.0,
                now=BASE,
                horizon_end=HORIZON,
                headroom_kwh=4.51,
                usable_initial_kwh=0.61,
                max_price_threshold=overrides.get("max_price_threshold", 0.20),
                premium_kwh=overrides.get("premium_kwh", 0.0),
                premium_price_threshold=overrides.get("premium_price_threshold"),
            )
        finally:
            monkeypatch.setattr(chronological, "_CANDIDATE_TRACE_ENABLED", True)

        assert untraced.candidate_trace is None
        assert traced.candidate_trace is not None
        assert untraced == traced
        assert [a.slot for a in untraced.allocations] == [
            a.slot for a in traced.allocations
        ]
        assert [a.planned_battery_kwh for a in untraced.allocations] == pytest.approx(
            [a.planned_battery_kwh for a in traced.allocations]
        )


# ----------------------------------------------------------------------
# Diagnostics payload
# ----------------------------------------------------------------------
def _payload(plan):
    controller = type("Controller", (), {})()
    controller._last_chronological_diagnostics = {"charge_candidates": plan.candidate_trace}
    return diagnostics._charge_candidates(controller)


def test_diagnostics_payload_is_bounded_and_counts_every_verdict():
    intervals = _day()
    deadlines = build_energy_deadlines(
        intervals, usable_initial_kwh=0.61, usable_capacity_kwh=5.12
    )
    # A day with a cheap night, a mid-price midday and an expensive evening.
    slots = [
        _quarter(index, 0.10 if index < 32 else (0.30 if index < 72 else 0.65))
        for index in range(96)
    ]
    plan = allocate_price_slots(
        intervals,
        deadlines,
        slots,
        total_required_kwh=4.51,
        effective_power_kw=2.5,
        charge_efficiency=1.0,
        now=BASE,
        horizon_end=HORIZON,
        headroom_kwh=4.51,
        usable_initial_kwh=0.61,
        max_price_threshold=0.40,
    )
    payload = _payload(plan)

    assert payload["available"] is True
    assert payload["interval_minutes"] == 15
    assert payload["record_count"] == 96
    assert len(payload["records"]) <= 96
    assert sum(payload["summary"].values()) == len(payload["records"])
    assert payload["summary"].get("chosen")
    assert payload["summary"].get("too_expensive")
    assert {record["index"] for record in payload["records"]} == set(range(96))
    for record in payload["records"]:
        assert set(record) <= {
            "index",
            "slot_start",
            "slot_end",
            "price",
            "ceiling",
            "room_kwh",
            "usable_kwh",
            "free_kwh",
            "blocked_kwh",
            "verdict",
            "decided_by",
        }


def test_diagnostics_records_are_json_safe():
    import json

    _, plan = _night_plan(solar_total_kwh=7.0)
    payload = _payload(plan)

    json.dumps(payload, allow_nan=False)


def test_diagnostics_without_a_trace_or_prices_never_crashes():
    assert diagnostics._charge_candidates(type("Controller", (), {})()) == {
        "available": False,
        "date": None,
        "records": [],
        "summary": {},
    }
    controller = type("Controller", (), {})()
    controller._last_decision_data = {"charge_candidates": None}
    assert diagnostics._charge_candidates(controller)["available"] is False

    intervals = _day()
    deadlines = build_energy_deadlines(
        intervals, usable_initial_kwh=0.61, usable_capacity_kwh=5.12
    )
    plan = allocate_price_slots(
        intervals,
        deadlines,
        [_slot(60, 480, float("nan"))],
        total_required_kwh=4.51,
        effective_power_kw=2.5,
        now=BASE,
        horizon_end=HORIZON,
        headroom_kwh=4.51,
        usable_initial_kwh=0.61,
    )
    payload = _payload(plan)

    # A non-finite price never reaches the planner (FFunes filters it before
    # any candidate list is built), so the trace stays empty instead of
    # inventing an "unpriced" record with a price of 0.
    assert payload["available"] is True
    assert payload["records"] == []
    assert payload["summary"] == {}


def test_diagnostics_fall_back_to_the_live_decision_data():
    _, plan = _night_plan()
    controller = type("Controller", (), {})()
    controller._last_decision_data = {"charge_candidates": plan.candidate_trace}

    payload = diagnostics._charge_candidates(controller)

    assert payload["available"] is True
    assert payload["record_count"] == 96

