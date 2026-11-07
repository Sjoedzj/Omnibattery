"""The PV credit only spends sun the learned profile can vouch for.

The reserve claims the dearest hours ahead in descending order, and that order
is the feature: a battery full at noon has to keep the 18:15 peak covered
first, then the next dearest hour. The sun may cancel part of those claims, but
only when the forecast behind it is proven.

These tests pin the gate that decides this, the behaviour it must not change,
and the 2025-11-06 cycle that motivated it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.control import discharge_reserve as _gate
from custom_components.omnibattery.control.discharge_reserve import (
    DischargeReserveManager,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.discharge_reserve import (
    reserve_soc_pct,
)

# Read through the module rather than importing by name: without the gate this
# symbol does not exist, and a collection error would say less than the value
# assertions below do.
SOLAR_CREDIT_MIN_FUTURE_COVERAGE = getattr(
    _gate, "SOLAR_CREDIT_MIN_FUTURE_COVERAGE", None
)

DAY = datetime(2026, 9, 4)
NOW = DAY + timedelta(hours=9)

# Sunrise 07:00, sunset 19:00. At 09:00 two of the twelve daylight hours are
# gone, so the evaluation window runs from 1/6 to 1.0.
DAY_START = DAY + timedelta(hours=7)
DAY_END = DAY + timedelta(hours=19)
ONE_DAY = DAY + timedelta(days=1)
HORIZON = ONE_DAY + timedelta(hours=7)


def _slot(hour: float, price: float) -> PriceSlot:
    start = DAY + timedelta(hours=hour)
    return PriceSlot(start, start + timedelta(hours=1), price)


MIDDAY = _slot(12, 0.15)
SUN_13 = _slot(13, 0.15)
PEAK_18 = _slot(18, 0.60)
PEAK_19 = _slot(19, 0.50)
SLOTS = [MIDDAY, SUN_13, PEAK_18, PEAK_19]


class _Profile:
    """A solar-profile double that records the coverage window it was asked for."""

    def __init__(self, *, mature: bool, future_coverage_ratio: float):
        self.mature = mature
        self.future_coverage_ratio = future_coverage_ratio
        self.asked: list[tuple] = []

    def get_snapshot(self, **kwargs):
        self.asked.append((kwargs.get("future_progress_start"), kwargs.get("future_progress_end")))
        return SimpleNamespace(
            mature=self.mature,
            future_coverage_ratio=self.future_coverage_ratio,
            fallback_reason=None if self.mature else "insufficient_days",
        )


def _coordinator(soc: float = 73.0, floor: float = 12.0, capacity: float = 10.24):
    return SimpleNamespace(
        name="battery-1",
        data={"battery_soc": soc, "battery_total_energy": capacity},
        is_available=True,
        battery_manual_mode_enabled=False,
        min_soc=floor,
        max_soc=100.0,
    )


def _intervals() -> list[float]:
    """Evening load only: 1.5 kWh in the 18:00 hour, 1.5 kWh in the 19:00 hour."""
    values = [0.0] * 96
    for index in range(72, 76):
        values[index] = 1.5 / 4.0
    for index in range(76, 80):
        values[index] = 1.5 / 4.0
    return values


def _forecast():
    intervals = _intervals()
    return SimpleNamespace(
        intervals_kwh=intervals,
        intervals_by_date={DAY.date(): intervals},
        energy_kwh=3.0,
        source="profile",
    )


def _solar_share(hour: float) -> float:
    """Cumulative generation share: sun concentrated in the 11:30-14:30 window."""
    if hour <= 11.5:
        return 0.0
    if hour >= 14.5:
        return 1.0
    return (hour - 11.5) / 3.0


NOV6_FORECAST_KWH = 7.5


def _pricing():
    return SimpleNamespace(
        get_future_price_slots=lambda horizon_end=None: list(SLOTS),
        _profile_remaining_consumption=lambda start, end: _forecast(),
        # 6 kWh over the three sunny hours, none of it during the peaks.
        _curtailment_forecast_model=lambda now: (6.0, _solar_share, None),
        _get_current_price=lambda: 0.20,
        energy_horizon_end=lambda _now: HORIZON,
        _solar_timeline_window=lambda now, tracker: (DAY_START, DAY_END),
    )


def _controller(profile=None, *, mode="active"):
    tracker = None if profile is None else SimpleNamespace(solar_profile=profile)
    return SimpleNamespace(
        discharge_reserve_enabled=True,
        discharge_reserve_min_saving=0.05,
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        predictive_charging_mode="dynamic_pricing",
        manual_mode_enabled=False,
        _manual_slot_owned=None,
        coordinators=[_coordinator()],
        _is_manual_slot_owned=lambda coordinator: False,
        _curtailment_runtime_status=None,
        _capacity_protection_active=False,
        max_charge_capacity=10_000.0,
        solar_profile_mode=mode,
        _consumption_tracker=tracker,
        _pricing_mgr=_pricing(),
    )


def _manager(controller):
    manager = DischargeReserveManager(SimpleNamespace(), controller)
    manager._now = lambda: NOW
    asyncio.new_event_loop().run_until_complete(manager.async_rebuild_plan("test"))
    return manager


def _built(profile=None, *, mode="active"):
    return _built_with(_controller(profile, mode=mode))


def _built_with(controller):
    manager = _manager(controller)
    manager.reserve_soc_pct()
    return manager


def _reliable(manager, now):
    """The gate's verdict, read through the module so its absence is visible."""
    assert hasattr(_gate, "SOLAR_CREDIT_MIN_FUTURE_COVERAGE"), (
        "the reliability gate is missing from control/discharge_reserve.py"
    )
    return manager._solar_credit_is_reliable(now)


def _surplus(controller) -> dict:
    manager = DischargeReserveManager(SimpleNamespace(), controller)
    manager._now = lambda: NOW
    _demand, surplus = manager._demand_and_surplus_by_slot(
        controller._pricing_mgr, SLOTS, NOW, HORIZON
    )
    return surplus


# ----------------------------------------------------------------------
# (a) An unproven forecast credits nothing, so every peak stays reserved
# ----------------------------------------------------------------------


def test_an_immature_profile_credits_nothing_and_every_peak_stays_reserved():
    """``insufficient_days`` is the integration saying it does not know yet."""
    profile = _Profile(mature=False, future_coverage_ratio=1.0)
    manager = _built(profile)

    assert _surplus(manager._controller) == {}
    status = manager.get_status()
    assert status["claimed_kwh"] == pytest.approx(3.0)
    assert status["pv_credit_kwh"] == 0.0
    assert status["reserve_kwh"] == pytest.approx(3.0)
    assert manager.reserve_soc_pct() == pytest.approx(reserve_soc_pct(3.0, 10.24))
    assert [claim["start"] for claim in status["claims"]] == [
        PEAK_18.start.isoformat(),
        PEAK_19.start.isoformat(),
    ]


def test_a_mature_profile_that_does_not_cover_the_coming_daylight_credits_nothing():
    """Mature on the days it has seen, blind to the hours it would cancel."""
    profile = _Profile(mature=True, future_coverage_ratio=0.79)
    manager = _built(profile)

    assert _surplus(manager._controller) == {}
    assert manager.get_status()["pv_credit_kwh"] == 0.0
    assert manager.get_status()["reserve_kwh"] == pytest.approx(3.0)


def test_the_gate_asks_for_the_window_of_daylight_still_ahead():
    profile = _Profile(mature=True, future_coverage_ratio=0.9)
    _manager(_controller(profile))

    assert profile.asked == [(pytest.approx(1.0 / 6.0), pytest.approx(1.0))]


# ----------------------------------------------------------------------
# (b) A proven forecast keeps today's behaviour, exactly
# ----------------------------------------------------------------------


def test_a_mature_covering_profile_credits_the_claims_as_before():
    profile = _Profile(mature=True, future_coverage_ratio=0.80)
    manager = _built(profile)

    surplus = _surplus(manager._controller)
    # Sun in the two midday hours, nothing in the claiming hours, so both
    # evening peaks stay claimable and the credit is spent against them.
    assert surplus[MIDDAY] > 0.0
    assert surplus[SUN_13] > 0.0
    assert surplus[PEAK_18] == 0.0
    assert surplus[PEAK_19] == 0.0

    status = manager.get_status()
    assert status["claimed_kwh"] == pytest.approx(3.0)
    assert status["pv_credit_kwh"] == pytest.approx(2.55)
    assert status["reserve_kwh"] == pytest.approx(0.45)
    # The credit order is the claim order: the dearest peak is paid first, then
    # the next dearest, and it stops when the sun runs out.
    assert [claim["pv_credit_kwh"] for claim in status["claims"]] == pytest.approx(
        [1.5, 1.05]
    )
    assert status["state"] == "reserving"


def test_the_coverage_threshold_itself_is_the_boundary():
    assert SOLAR_CREDIT_MIN_FUTURE_COVERAGE == 0.80

    manager = _manager(_controller(_Profile(mature=True, future_coverage_ratio=0.80)))
    assert _reliable(manager, NOW) is True

    manager = _manager(_controller(_Profile(mature=True, future_coverage_ratio=0.7999)))
    assert _reliable(manager, NOW) is False


# ----------------------------------------------------------------------
# (c) No profile, or the profile switched off: no verdict, no change
# ----------------------------------------------------------------------


def test_without_a_profile_the_credit_is_untouched():
    manager = _built(None)

    assert _surplus(manager._controller)[MIDDAY] > 0.0
    assert _reliable(manager, NOW) is True
    assert manager.get_status()["pv_credit_kwh"] == pytest.approx(2.55)


def test_with_the_profile_switched_off_the_credit_is_untouched():
    profile = _Profile(mature=False, future_coverage_ratio=0.0)
    manager = _built(profile, mode="off")

    assert _surplus(manager._controller)[MIDDAY] > 0.0
    assert _reliable(manager, NOW) is True
    assert manager.get_status()["pv_credit_kwh"] == pytest.approx(2.55)
    assert profile.asked == []


def test_an_unresolvable_solar_window_still_judges_the_profile_mature():
    """Nothing to measure coverage against is not the same as failing it."""
    controller = _controller(_Profile(mature=True, future_coverage_ratio=0.0))
    controller._pricing_mgr._solar_timeline_window = lambda now, tracker: (None, None)
    manager = _manager(controller)

    assert _reliable(manager, NOW) is True
    # Nothing to measure, so nothing is asked of the profile either.
    assert controller._consumption_tracker.solar_profile.asked == []


# ----------------------------------------------------------------------
# The claim lane: a claimed peak's own hour
# ----------------------------------------------------------------------
#
# The parent asked for a regression test on the "B2 exemption": a claimed slot
# must not make room for its own sun. Measured against the tree at 9907ecb0 the
# exemption does *not* hold, and the test below records what the code actually
# does today rather than what it should do. It is written to pass on the current
# behaviour so the finding is pinned and visible; it fails the moment the
# exemption is implemented, which is the moment someone must decide deliberately
# whether to keep the correction.


def test_a_claimed_slot_does_not_credit_itself_with_its_own_sun():
    """The requested B2 property, verified at the credit lane's own contract.

    ``_reserve_after_pv`` walks the slots from ``now`` up to (but not including)
    a claim's own slot, so the claiming slot is never part of the walk that
    settles its own claim. A peak whose own hour is sunny therefore cannot
    credit itself: that hour's sun is only ever spent on a *later* claim.

    The scenario passes the claims directly because the slot pipeline cannot
    express it: there, the sun in a slot also cancels that slot's own net
    demand, so an hour with load and generation at once stops being a claim at
    all before the credit lane ever sees it.
    """
    from custom_components.omnibattery.pricing.discharge_reserve import (
        ReservePlan,
        ReserveSlot,
    )

    def _reserve_slot(hour, price, demand, sun):
        start = DAY + timedelta(hours=hour)
        return ReserveSlot(start, start + timedelta(hours=1), price, demand, sun)

    own = _reserve_slot(19, 0.60, 1.0, 1.5)  # the dearest claim, sunny itself
    later = _reserve_slot(21, 0.40, 1.0, 0.0)  # the claim after it
    plan = ReservePlan(slots=[own, later])

    reserve, credits = plan._reserve_after_pv(
        NOW, [(own, 1.0), (later, 1.0)], 10.0, 6.0
    )

    # The dearest claim keeps its whole claim; the sun in its own hour does not
    # pay it. That same sun is spent on the claim that starts after it.
    assert credits == pytest.approx([0.0, 1.0])
    assert reserve == pytest.approx(1.0)


# ----------------------------------------------------------------------
# (e) 2025-11-06, 05:43: what the gate does to that cycle
# ----------------------------------------------------------------------

NOV6 = datetime(2025, 11, 6)
NOV6_NOW = NOV6 + timedelta(hours=5, minutes=43)
NOV6_CAPACITY = 10.24
NOV6_MIN_SOC = 12.0
NOV6_SOC = 73.0
NOV6_RTE = 0.8
MORNING_LOAD_KWH = 5.58  # metered, 06:30 - 08:45

NOV6_PRICES = {
    6: 0.36,
    7: 0.32,
    8: 0.30,
    9: 0.22,
    10: 0.18,
    11: 0.15,
    12: 0.14,
    13: 0.13,
    14: 0.16,
    15: 0.24,
    16: 0.33,
    17: 0.44,
    18: 0.57,
    19: 0.52,
    20: 0.41,
    21: 0.34,
}
NOV6_SLOTS = [
    PriceSlot(NOV6 + timedelta(hours=hour), NOV6 + timedelta(hours=hour + 1), price)
    for hour, price in sorted(NOV6_PRICES.items())
]
# AC generation per hour, a sunny November day. Concentrated in the middle of
# the day: the morning and the evening peaks see none of it.
def _nov6_solar_share(hour: float) -> float:
    if hour <= 8.0:
        return 0.0
    if hour >= 12.0:
        return 1.0
    return (hour - 8.0) / 4.0


NOV6_FORECAST_KWH = 7.5


def _nov6_intervals() -> list[float]:
    """The household's own shape: a morning load and an evening load."""
    values = [0.0] * 96
    for index in range(24, 27):  # 06:00 - 06:45: 1.2 kWh
        values[index] = 1.2 / 3.0
    for index in range(27, 35):  # 06:45 - 08:45: the metered 4.38 kWh
        values[index] = 4.38 / 8.0
    # 4.0 kWh of evening load over 17:00 - 22:00: 0.4 kWh per hour slot, so
    # every peak claims a real share and the morning peak competes with them
    # for the 6.25 kWh the battery holds above its floor.
    for hour in (17, 18, 19, 20, 21):
        values[hour * 4 + 2] = 0.2
        values[hour * 4 + 3] = 0.2
    return values


def _nov6_controller(*, mature: bool, coverage: float):
    intervals = _nov6_intervals()

    def _profile_remaining(start, end):
        return SimpleNamespace(
            intervals_kwh=intervals,
            intervals_by_date={NOV6.date(): intervals},
            energy_kwh=sum(intervals),
            source="profile",
        )

    def _forecast_model(now):
        return NOV6_FORECAST_KWH, _nov6_solar_share, 0.0

    profile = _Profile(mature=mature, future_coverage_ratio=coverage)
    controller = _controller(profile)
    controller.coordinators = [
        _coordinator(soc=NOV6_SOC, floor=NOV6_MIN_SOC, capacity=NOV6_CAPACITY)
    ]
    controller._pricing_mgr = SimpleNamespace(
        get_future_price_slots=lambda horizon_end=None: list(NOV6_SLOTS),
        _profile_remaining_consumption=_profile_remaining,
        _curtailment_forecast_model=_forecast_model,
        _get_current_price=lambda: 0.30,
        energy_horizon_end=lambda _now: NOV6 + timedelta(days=1, hours=7),
        _solar_timeline_window=lambda now, tracker: (
            NOV6 + timedelta(hours=7),
            NOV6 + timedelta(hours=17),
        ),
    )
    return controller


def _nov6_plan(*, mature: bool, coverage: float):
    controller = _nov6_controller(mature=mature, coverage=coverage)
    manager = DischargeReserveManager(SimpleNamespace(), controller)
    manager._now = lambda: NOV6_NOW
    asyncio.new_event_loop().run_until_complete(manager.async_rebuild_plan("nov6"))
    manager.reserve_soc_pct()
    return manager.get_status()


def _nov6_soc_at_0842(status) -> float:
    """The battery serves the morning load; the floor stops it when it runs out."""
    floor = NOV6_MIN_SOC + status["reserve_soc_pct"]
    deliverable = NOV6_CAPACITY * (NOV6_SOC - floor) / 100.0 * NOV6_RTE
    if deliverable >= MORNING_LOAD_KWH:
        return NOV6_SOC - (MORNING_LOAD_KWH / NOV6_RTE) / NOV6_CAPACITY * 100.0
    return floor


def test_november_6_the_immature_profile_keeps_the_morning_peak_reserved():
    """The reported cycle: without the gate the credit wrote off the whole reserve.

    Measured on 2025-11-06 at 05:43: SOC 73%, a fleet of 10.24 kWh with a 12 %
    floor, 5.58 kWh of morning load between 06:30 and 08:45. The profile on that
    install reported ``insufficient_days``, so no claim may be credited.
    """
    status = _nov6_plan(mature=False, coverage=1.0)

    assert status["claimed_kwh"] == pytest.approx(3.35, abs=0.01)
    assert status["pv_credit_kwh"] == 0.0
    assert status["reserve_kwh"] == pytest.approx(3.35, abs=0.01)
    assert status["reserve_soc_pct"] == pytest.approx(32.69, abs=0.05)
    assert _nov6_soc_at_0842(status) == pytest.approx(12.0 + 32.69, abs=0.05)
    # The dearest hour left is the 06:00 peak, and it keeps its whole claim.
    dearest = status["claims"][0]
    assert dearest["start"][11:16] == "06:00"
    assert dearest["price"] == pytest.approx(0.36)
    assert dearest["claimed_kwh"] == pytest.approx(1.75, abs=0.01)
    assert dearest["pv_credit_kwh"] == 0.0


def test_november_6_a_proven_profile_releases_the_credit_as_before():
    """Same cycle, mature and covering profile: today's behaviour, unchanged."""
    status = _nov6_plan(mature=True, coverage=1.0)

    assert status["claimed_kwh"] == pytest.approx(3.35, abs=0.01)
    assert status["pv_credit_kwh"] == pytest.approx(1.6, abs=0.01)
    assert status["reserve_kwh"] == pytest.approx(1.75, abs=0.01)
    assert status["reserve_soc_pct"] == pytest.approx(17.07, abs=0.05)
    assert _nov6_soc_at_0842(status) == pytest.approx(12.0 + 17.07, abs=0.05)
    # Every evening claim is cancelled by the forecast sun; the 06:00 peak, which
    # no sun precedes, is the one claim that survives either way.
    assert [claim["pv_credit_kwh"] for claim in status["claims"]] == pytest.approx(
        [0.0, 0.4, 0.4, 0.4, 0.4]
    )
