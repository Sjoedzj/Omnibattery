"""Peak-directed deadline re-evaluation (the last useful rebuild before a peak).

The magnitude triggers re-plan when the shortfall or the forecast moved by a
fixed amount, and the two of them share one daily budget of 4. An afternoon
whose sun turns out weaker than the plan assumed can therefore arrive with both
damped out, while a deadline the plan committed to is still reachable by a
quarter that costs less than that deadline's own value (import price x round
trip efficiency - arbitrage margin, #662). This trigger is that last call: it
fires on the same deterioration judged by the deadline's value rather than by a
magnitude, and it does not draw on the shared budget.

The scenario tests at the bottom drive the real allocator: the plan as built on
the optimistic forecast is what the schedule holds, the rebuild is what the
half-sun day actually needs. Full sun must change nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    CHARGE_EFFICIENCY,
    DEADLINE_VALUE_REEVAL_COOLDOWN_MIN,
    DEADLINE_VALUE_REEVAL_KWH,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_TIME_SLOT,
)
from custom_components.omnibattery.pricing import (
    SLOT_PURPOSE_DEFICIT,
    PriceSlot,
)
from custom_components.omnibattery.pricing.chronological import (
    EnergyDeadline,
    EnergyInterval,
    allocate_price_slots,
    build_energy_deadlines,
    simulate_allocations,
)
from custom_components.omnibattery.pricing.engine import (
    DynamicPricingEvaluationHorizon,
    PricingManager,
)

NOW = datetime(2026, 8, 28, 15, 45)
PEAK_START = datetime(2026, 8, 28, 17, 0)
PEAK_END = datetime(2026, 8, 28, 20, 0)
EFFICIENCY = 0.84
MARGIN = 0.05
# value_cap of a deadline inside a 0.89 peak quarter: 0.89 x 0.84 - 0.05.
PEAK_CAP = 0.89 * EFFICIENCY - MARGIN


def _quarter(start, price):
    if not isinstance(start, datetime):
        start = NOW.replace(hour=start, minute=0) + timedelta(minutes=start)
    return PriceSlot(start, start + timedelta(minutes=15), price)


def _afternoon_and_peak_prices():
    """Cheap quarters before the peak, the peak itself, then the evening."""
    slots = [
        _quarter(NOW + timedelta(minutes=15 * step), 0.40) for step in range(5)
    ]
    slots += [
        _quarter(PEAK_START + timedelta(minutes=15 * step), 0.89)
        for step in range(12)
    ]
    slots += [
        _quarter(PEAK_END + timedelta(minutes=15 * step), 0.70)
        for step in range(2)
    ]
    return slots


def _peak_deadline(required_kwh=2.0, deadline=None):
    """A deadline *inside* the peak, which is what makes its cap the peak price.

    ``value_cap`` is read from the quarter that serves the deadline, so a
    deadline landing exactly on the peak boundary is valued at the cheap quarter
    that ends there. The investigated failure is a battery that runs empty a few
    minutes into the peak.
    """
    return EnergyDeadline(
        deadline or PEAK_START + timedelta(minutes=7), required_kwh, "depletion"
    )


def _schedule(deadlines, selected_slots=None):
    return SimpleNamespace(
        chronological_planning_active=True,
        energy_deadlines=list(deadlines),
        selected_slots=list(selected_slots or []),
        slot_energy_targets_kwh={},
        slot_deadlines={},
        slot_purposes={},
        slot_plan_kinds={},
        charging_needed=False,
        deficit_charging_needed=False,
        negative_price_charging_needed=False,
        purpose_for=lambda slot: SLOT_PURPOSE_DEFICIT,
    )


def _ctrl(reference, current, deadlines, **overrides):
    base = dict(
        _dp_last_eval_deficit_kwh=reference,
        _last_decision_data={"energy_deficit_kwh": current},
        _dynamic_pricing_schedule=_schedule(deadlines),
        _dp_deadline_value_reeval_at=None,
        _dp_deficit_reeval_at=None,
        _dp_deficit_reeval_active_slot=None,
        _current_price_slot_active=False,
        _active_dynamic_price_slot=None,
        # Spent on purpose: the whole point is that this trigger ignores it.
        _dp_solar_forecast_reeval_count=4,
        min_arbitrage_margin=MARGIN,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        round_trip_efficiency=EFFICIENCY,
        coordinators=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _mgr(ctrl, slots=None):
    manager = PricingManager(
        SimpleNamespace(config=SimpleNamespace(time_zone="UTC")), ctrl
    )
    manager._deadline_price_slots = lambda: list(
        _afternoon_and_peak_prices() if slots is None else slots
    )
    return manager


# ----------------------------------------------------------------------
# the predicate
# ----------------------------------------------------------------------

def test_fires_when_a_quarter_before_the_deadline_undercuts_its_cap():
    # 15:45-16:00 costs 0.40, the deadline's own cap is 0.6976: buying now pays.
    ctrl = _ctrl(2.0, 2.4, [_peak_deadline()])
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is True


def test_stays_silent_when_only_quarters_above_the_cap_are_left():
    # The peak quarters cost 0.89 and are above their own value_cap, so #662 is
    # right to leave them to the grid; the last useful moment has passed.
    slots = [slot for slot in _afternoon_and_peak_prices() if slot.start >= PEAK_START]
    ctrl = _ctrl(2.0, 2.4, [_peak_deadline()])
    assert _mgr(ctrl, slots=slots)._is_deadline_value_reeval(NOW) is False


def test_the_shared_daily_budget_does_not_block_it():
    # The whole reason this trigger exists: the budget can be spent by the two
    # magnitude triggers before the one firing that still has a cheap quarter.
    ctrl = _ctrl(2.0, 2.4, [_peak_deadline()], _dp_solar_forecast_reeval_count=99)
    manager = _mgr(ctrl)
    assert manager._is_deadline_value_reeval(NOW) is True
    # ... and it never spends a unit of that budget either.
    assert ctrl._dp_solar_forecast_reeval_count == 99


def test_is_directional_and_ignores_a_falling_shortfall():
    ctrl = _ctrl(4.0, 2.4, [_peak_deadline()])
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is False


@pytest.mark.parametrize(
    ("rise", "expected"),
    [
        (0.0, False),
        (DEADLINE_VALUE_REEVAL_KWH - 0.01, False),
        (DEADLINE_VALUE_REEVAL_KWH, True),
    ],
)
def test_needs_a_rise_of_at_least_the_deadline_threshold(rise, expected):
    ctrl = _ctrl(2.0, 2.0 + rise, [_peak_deadline()])
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is expected


def test_respects_its_own_cooldown():
    ctrl = _ctrl(
        2.0,
        2.4,
        [_peak_deadline()],
        _dp_deadline_value_reeval_at=NOW
        - timedelta(minutes=DEADLINE_VALUE_REEVAL_COOLDOWN_MIN - 1),
    )
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is False

    ctrl._dp_deadline_value_reeval_at = NOW - timedelta(
        minutes=DEADLINE_VALUE_REEVAL_COOLDOWN_MIN + 1
    )
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is True


def test_never_disturbs_the_same_live_charge_twice():
    # A rebuild can drop the live slot and stop its charge, so the marker the
    # deficit trigger keeps is shared: one charge may be disturbed once.
    live = SimpleNamespace(start=NOW - timedelta(minutes=10))
    ctrl = _ctrl(
        2.0,
        2.4,
        [_peak_deadline()],
        _current_price_slot_active=True,
        _active_dynamic_price_slot=live,
        _dp_deficit_reeval_active_slot=live.start,
    )
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is False


def test_still_fires_once_inside_a_live_charge():
    live = SimpleNamespace(start=NOW - timedelta(minutes=10))
    ctrl = _ctrl(
        2.0,
        2.4,
        [_peak_deadline()],
        _current_price_slot_active=True,
        _active_dynamic_price_slot=live,
        _dp_deficit_reeval_active_slot=None,
    )
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is True


def test_re_arms_after_the_live_charge_ends():
    live = SimpleNamespace(start=NOW - timedelta(minutes=10))
    ctrl = _ctrl(
        2.0,
        2.4,
        [_peak_deadline()],
        _current_price_slot_active=False,
        _active_dynamic_price_slot=None,
        _dp_deficit_reeval_active_slot=live.start,
    )
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is True


def test_false_without_a_chronological_plan():
    ctrl = _ctrl(2.0, 2.4, [_peak_deadline()])
    ctrl._dynamic_pricing_schedule = None
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is False


def test_false_without_a_plan_deadline():
    ctrl = _ctrl(2.0, 2.4, [])
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is False


def test_false_when_the_only_deadline_has_passed():
    ctrl = _ctrl(2.0, 2.4, [_peak_deadline(deadline=NOW - timedelta(minutes=1))])
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is False


def test_a_guaranteed_floor_deadline_has_no_cap_to_compare():
    # value_cap is None there: the floor is served whatever the import costs, so
    # its presence is no evidence that buying *now* pays.
    ctrl = _ctrl(
        2.0,
        2.4,
        [EnergyDeadline(PEAK_START, 2.0, "guaranteed_floor")],
    )
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is False


def test_false_without_a_configured_arbitrage_margin():
    ctrl = _ctrl(2.0, 2.4, [_peak_deadline()], min_arbitrage_margin=None)
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is False
    ctrl = _ctrl(2.0, 2.4, [_peak_deadline()], min_arbitrage_margin=0.0)
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is False


def test_false_in_time_slot_mode():
    # Time Slot prices are synthetic, so #662 values no deadline there.
    ctrl = _ctrl(
        2.0, 2.4, [_peak_deadline()], predictive_charging_mode=PREDICTIVE_MODE_TIME_SLOT
    )
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is False


def test_false_when_the_deadline_lies_past_the_last_published_price():
    # An unpriced deadline is never worth buying for: its value is unknown, and
    # the allocator treats it as "left to the grid until its price is known".
    late = EnergyDeadline(datetime(2026, 8, 29, 4, 0), 2.0, "depletion")
    ctrl = _ctrl(2.0, 2.4, [late])
    assert _mgr(ctrl)._is_deadline_value_reeval(NOW) is False


def test_false_late_in_the_evening():
    ctrl = _ctrl(2.0, 2.4, [_peak_deadline()])
    late = NOW.replace(hour=23, minute=10)
    assert _mgr(ctrl)._is_deadline_value_reeval(late) is False


def test_ignores_a_deadline_whose_cap_exactly_equals_the_quarter():
    # Strictly below only: at equality there is nothing left to gain, and the
    # rebuild would trade one disturbance for no energy.
    ctrl = _ctrl(2.0, 2.4, [_peak_deadline()])
    equal = [
        PriceSlot(NOW, NOW + timedelta(minutes=15), PEAK_CAP),
        PriceSlot(PEAK_START, PEAK_START + timedelta(minutes=15), 0.89),
    ]
    assert _mgr(ctrl, slots=equal)._is_deadline_value_reeval(NOW) is False


def test_a_deadline_inside_a_cheap_quarter_is_not_worth_buying_for():
    # Its own cap is that quarter's price minus the margin, so the quarter can
    # never undercut it: a deadline the plan can already serve at the going rate
    # is no reason to disturb anything.
    soon = EnergyDeadline(NOW + timedelta(minutes=30), 1.0, "depletion")
    assert _mgr(_ctrl(2.0, 2.4, [soon]))._is_deadline_value_reeval(NOW) is False


def test_fires_when_a_later_deadline_can_still_be_served():
    # The first deadline is only reachable through its own (cheap) quarter, the
    # later one sits in a dear quarter and the same cheap quarter undercuts its
    # cap, so the rebuild is still worth it.
    soon = EnergyDeadline(NOW + timedelta(minutes=30), 1.0, "depletion")
    later = EnergyDeadline(datetime(2026, 8, 28, 21, 15), 2.0, "depletion")
    slots = [
        PriceSlot(NOW, NOW + timedelta(minutes=15), 0.40),
        PriceSlot(NOW + timedelta(minutes=15), NOW + timedelta(minutes=30), 0.40),
        PriceSlot(datetime(2026, 8, 28, 21, 0), datetime(2026, 8, 28, 21, 15), 0.70),
    ]
    ctrl = _ctrl(2.0, 2.4, [soon, later])
    assert _mgr(ctrl, slots=slots)._is_deadline_value_reeval(NOW) is True


# ----------------------------------------------------------------------
# the scenario: half sun against full sun, driven by the real allocator
# ----------------------------------------------------------------------

LOAD_KWH_PER_QUARTER = 0.15  # 0.6 kW flat house load
USABLE_NOW_KWH = 1.0
HEADROOM_KWH = 6.0
POWER_KW = 3.0
HORIZON = datetime(2026, 8, 29, 7, 0)


def _intervals(solar_kwh_per_quarter, until=HORIZON):
    """Flat load, ``solar_kwh_per_quarter`` of sun up to 17:00, then none."""
    intervals = []
    start = NOW
    while start < until:
        end = start + timedelta(minutes=15)
        solar = solar_kwh_per_quarter if start < PEAK_START else 0.0
        intervals.append(EnergyInterval(start, end, LOAD_KWH_PER_QUARTER, solar))
        start = end
    return intervals


def _prices_until_horizon():
    slots = []
    start = NOW
    while start < HORIZON:
        if start < PEAK_START:
            price = 0.40
        elif start < PEAK_END:
            price = 0.89
        else:
            price = 0.70
        slots.append(PriceSlot(start, start + timedelta(minutes=15), price))
        start += timedelta(minutes=15)
    return slots


def _live_plan(intervals, price_slots, required_kwh):
    """The plan the engine rebuilds: same call the allocator gets from it."""
    return allocate_price_slots(
        intervals,
        build_energy_deadlines(
            intervals, USABLE_NOW_KWH, usable_capacity_kwh=USABLE_NOW_KWH + HEADROOM_KWH
        ),
        price_slots,
        total_required_kwh=required_kwh,
        effective_power_kw=POWER_KW,
        now=NOW,
        horizon_end=HORIZON,
        headroom_kwh=HEADROOM_KWH,
        usable_initial_kwh=USABLE_NOW_KWH,
        max_price_threshold=None,
        import_value_efficiency=EFFICIENCY,
        import_value_margin=MARGIN,
    )


def _depletion_at(intervals, plan=None):
    return simulate_allocations(
        intervals,
        USABLE_NOW_KWH,
        usable_capacity_kwh=USABLE_NOW_KWH + HEADROOM_KWH,
        allocations=plan.allocations if plan is not None else (),
    ).earliest_depletion_at


def test_half_sun_leaves_the_peak_to_the_grid_until_the_rebuild_covers_it():
    half_sun = _intervals(0.12)
    optimistic = _intervals(0.12 + 0.18)
    slots = _prices_until_horizon()

    # The plan on the schedule was built on the sunny forecast: it covers the
    # peak, so it schedules nothing and the live shortfall rises against it.
    scheduled = _live_plan(optimistic, slots, required_kwh=0.0)
    assert _depletion_at(optimistic, scheduled) is not None

    # With the sun that actually arrives, the battery is empty inside the peak.
    without_rebuild = _depletion_at(half_sun)
    assert without_rebuild is not None and PEAK_START < without_rebuild < PEAK_END

    # The trigger sees the stale plan's deadline and a quarter that undercuts
    # its value, so it asks for the rebuild while the cheap afternoon is open.
    ctrl = _ctrl(
        2.0,
        2.4,
        scheduled.deadlines,
        _dynamic_pricing_schedule=SimpleNamespace(
            chronological_planning_active=True,
            energy_deadlines=list(scheduled.deadlines),
            selected_slots=[],
            slot_energy_targets_kwh={},
            slot_deadlines={},
        ),
    )
    assert _mgr(ctrl, slots=slots)._is_deadline_value_reeval(NOW) is True

    # The rebuild buys the cheap afternoon and the peak is covered: the
    # projection no longer runs empty before it ends.
    rebuilt = _live_plan(half_sun, slots, required_kwh=2.0)
    assert rebuilt.allocated_kwh > 0
    assert all(allocation.slot.end <= PEAK_END for allocation in rebuilt.allocations)
    covered = _depletion_at(half_sun, rebuilt)
    assert covered is None or covered >= PEAK_END


def test_full_sun_neither_fires_nor_buys_extra():
    full_sun = _intervals(0.30)
    slots = _prices_until_horizon()

    scheduled = _live_plan(full_sun, slots, required_kwh=0.0)
    # Full sun fills the battery across the peak on its own, so the plan buys
    # nothing: whatever deadline the night still produces is covered by what
    # the afternoon stored.
    assert scheduled.allocated_kwh == pytest.approx(0.0, abs=0.01)
    assert scheduled.deadline_required_kwh == pytest.approx(0.0, abs=0.01)
    covered = _depletion_at(full_sun)
    assert covered is None or covered >= PEAK_END

    # The shortfall never moved, so the trigger has nothing to act on even
    # though the plan does state a deadline for the night.
    ctrl = _ctrl(2.0, 2.0, scheduled.deadlines)
    assert _mgr(ctrl, slots=slots)._is_deadline_value_reeval(NOW) is False

    # And an untriggered rebuild is an unchanged plan: no extra purchase.
    unchanged = _live_plan(full_sun, slots, required_kwh=0.0)
    assert unchanged.allocated_kwh == pytest.approx(scheduled.allocated_kwh)


def test_the_peak_is_only_reachable_while_a_cheaper_quarter_is_left():
    """The window this trigger exists for closes on its own.

    Same half-sun day, but asked at 16:30 with nothing under the cap left
    before the deadline: firing then would only disturb a running charge for
    energy #662 correctly refuses to buy.
    """
    half_sun = _intervals(0.12)
    scheduled = _live_plan(half_sun, _prices_until_horizon(), required_kwh=0.0)
    after_peak_start = [
        slot for slot in _prices_until_horizon() if slot.start >= PEAK_START
    ]
    ctrl = _ctrl(2.0, 2.4, scheduled.deadlines)
    late = datetime(2026, 8, 28, 16, 30)
    assert _mgr(ctrl, slots=after_peak_start)._is_deadline_value_reeval(late) is False


def test_charge_efficiency_constant_is_the_one_the_allocator_uses():
    # Guard against the scenario drifting away from the planner's own model.
    assert CHARGE_EFFICIENCY == pytest.approx(0.84, abs=0.05)
    assert PEAK_CAP == pytest.approx(0.6976, abs=1e-4)


# ----------------------------------------------------------------------
# wired into the control cycle
# ----------------------------------------------------------------------

class _Stop(Exception):
    """Sentinel: the rebuild we are waiting for has been asked for."""


def _run_control_cycle(manager, slots, *, rebuild_expected):
    """Drive the real handler over the deadline phase and record what happened.

    Returns ``(rebuild_calls, predicate_verdicts)``. The rebuild stub raises
    ``_Stop`` so the handler stops right there; with ``rebuild_expected=False``
    another exception is tolerated, because the phases after the trigger need
    more stub surface than these tests provide — the recorded verdicts prove the
    phase was reached either way.
    """
    import asyncio

    from custom_components.omnibattery.pricing import engine as pricing_engine

    calls = []
    recorded = []

    async def noop(*_args, **_kwargs):
        return None

    async def rebuild(**kwargs):
        calls.append(kwargs)
        raise _Stop

    manager._maybe_refresh_service_prices = noop
    manager._check_dp_pre_slot_reevaluation = noop
    manager._is_evening_reevaluation_time = lambda: False
    manager._is_dp_soc_drop_reeval = lambda: False
    manager._is_excluded_demand_reeval = lambda _now: False
    manager._is_solar_forecast_reeval = lambda _now: False
    manager._is_deficit_reeval = lambda _now: False
    manager._is_price_publication_reeval = lambda _now: False
    manager._deadline_price_slots = lambda: list(slots)
    manager._evaluate_dynamic_pricing = rebuild
    real_predicate = manager._is_deadline_value_reeval

    def recording_predicate(now):
        verdict = real_predicate(now)
        recorded.append(verdict)
        return verdict

    manager._is_deadline_value_reeval = recording_predicate

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    original = pricing_engine.datetime
    pricing_engine.datetime = FixedDateTime
    try:
        asyncio.run(manager.handle_dynamic_pricing_predictive_charging())
    except _Stop:
        if not rebuild_expected:  # pragma: no cover - the assertion is the point
            pytest.fail("the deadline trigger fired without a moved balance")
    except Exception:
        if rebuild_expected:  # pragma: no cover - the assertion is the point
            raise
    finally:
        pricing_engine.datetime = original
    return calls, recorded


def test_control_cycle_rebuilds_even_with_the_shared_budget_spent():
    # End to end through the handler: the budget of 4 is gone, both magnitude
    # predicates stand down, and the deadline trigger still asks for the plan
    # that can cover the peak.
    schedule = _schedule([_peak_deadline()])
    ctrl = _ctrl(
        2.0,
        2.4,
        [_peak_deadline()],
        _dynamic_pricing_schedule=schedule,
        **{
            "_dynamic_pricing_evaluated_date": NOW.date(),
            "_dp_eval_retry_count": 0,
            "_dp_config_dirty": False,
            "predictive_charging_overridden": False,
            "max_charge_capacity": 3000,
            "_dp_solar_forecast_reeval_count": 4,
        },
    )
    manager = _mgr(ctrl, slots=_afternoon_and_peak_prices())
    calls, recorded = _run_control_cycle(
        manager, _afternoon_and_peak_prices(), rebuild_expected=True
    )

    assert len(calls) == 1
    assert calls[0]["horizon"] is DynamicPricingEvaluationHorizon.REMAINING
    assert ctrl._dp_deadline_value_reeval_at == NOW
    # It never spends the shared budget it is exempt from.
    assert ctrl._dp_solar_forecast_reeval_count == 4
    assert ctrl._dp_deficit_reeval_active_slot is None
    assert recorded == [True]


def test_control_cycle_remembers_the_live_charge_it_disturbed():
    live = SimpleNamespace(start=NOW - timedelta(minutes=10))
    schedule = _schedule([_peak_deadline()])
    ctrl = _ctrl(
        2.0,
        2.4,
        [_peak_deadline()],
        _dynamic_pricing_schedule=schedule,
        _current_price_slot_active=True,
        _active_dynamic_price_slot=live,
        **{
            "_dynamic_pricing_evaluated_date": NOW.date(),
            "_dp_eval_retry_count": 0,
            "_dp_config_dirty": False,
            "predictive_charging_overridden": False,
            "max_charge_capacity": 3000,
        },
    )
    manager = _mgr(ctrl, slots=_afternoon_and_peak_prices())
    calls, _recorded = _run_control_cycle(
        manager, _afternoon_and_peak_prices(), rebuild_expected=True
    )

    assert len(calls) == 1
    # Shared with the deficit trigger: the same running charge may only be
    # disturbed once, by either of them.
    assert ctrl._dp_deficit_reeval_active_slot == live.start
    assert manager._is_deadline_value_reeval(NOW + timedelta(minutes=1)) is False


def test_control_cycle_stands_down_without_a_moved_balance():
    schedule = _schedule([_peak_deadline()])
    ctrl = _ctrl(
        2.0,
        2.0,
        [_peak_deadline()],
        _dynamic_pricing_schedule=schedule,
        **{
            "_dynamic_pricing_evaluated_date": NOW.date(),
            "_dp_eval_retry_count": 0,
            "_dp_config_dirty": False,
            "predictive_charging_overridden": False,
            "max_charge_capacity": 3000,
        },
    )
    manager = _mgr(ctrl, slots=_afternoon_and_peak_prices())
    calls, recorded = _run_control_cycle(
        manager, _afternoon_and_peak_prices(), rebuild_expected=False
    )

    assert calls == []
    assert recorded == [False]
    assert ctrl._dp_deadline_value_reeval_at is None
