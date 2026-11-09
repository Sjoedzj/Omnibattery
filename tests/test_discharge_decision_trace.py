"""Per-quarter discharge decisions, read from the real control path.

The ``charge_candidates`` tests next door pin the microscope over "why not
charge". These pin the other direction: the reason a quarter was allowed to
discharge or was held back must come from the live blocker registry, the pricing
manager's own price/threshold accessors and the measured delivery - never from a
re-projection of the plan. They also pin the two properties that make the trace
safe to ship: it is bounded and JSON-safe, and collecting it changes nothing
about the blockers, the plan or the dispatch.
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.omnibattery import ChargeDischargeController, diagnostics
from custom_components.omnibattery.control.discharge_trace import (
    MAX_RECORDS,
    DischargeDecisionTrace,
)

LOCAL = timezone(timedelta(hours=1))


class _Clock:
    """Injectable wall clock plus monotonic source for the energy integration."""

    def __init__(self, moment: datetime, mono: float = 0.0) -> None:
        self.moment = moment
        self.mono = mono

    def now(self) -> datetime:
        return self.moment

    def monotonic(self) -> float:
        return self.mono


def _moment(index: int, minute: int = 7) -> datetime:
    """Return a local wall-clock time inside quarter ``index``."""
    return datetime(
        2026, 8, 30, index // 4, (index % 4) * 15 + minute, tzinfo=LOCAL
    )


class _Coordinator:
    def __init__(self, name: str = "battery_1", soc: float | None = 50.0,
                 delivered: float | None = None) -> None:
        self.name = name
        self.is_available = True
        self.data = {} if soc is None else {"battery_soc": soc}
        self.delivered = delivered


class _Pricing:
    """Stands in for PricingManager; counts the accessors the real path owns."""

    def __init__(self, price: float | None = None, threshold: float | None = None) -> None:
        self.price = price
        self.threshold = threshold
        self.price_calls = 0
        self.threshold_calls = 0

    def _get_current_price(self):
        self.price_calls += 1
        return self.price

    def _dp_price_discharge_threshold(self):
        self.threshold_calls += 1
        return self.threshold


class _Reserve:
    def __init__(self, reserve_kwh: float | None = None) -> None:
        self.status = {"state": "reserving", "reason": "ok", "reserve_soc_pct": 12.0}
        if reserve_kwh is not None:
            self.status["reserve_kwh"] = reserve_kwh

    def get_status(self) -> dict:
        return dict(self.status)


class _Controller:
    """Duck-typed stand-in exposing exactly the accessors the trace reuses."""

    def __init__(
        self,
        *,
        coordinators: list | None = None,
        global_blockers: dict | None = None,
        battery_blockers: dict | None = None,
        price: float | None = None,
        threshold: float | None = None,
        reserve_kwh: float | None = None,
    ) -> None:
        self.coordinators = list(coordinators) if coordinators else [_Coordinator()]
        self._pricing_mgr = _Pricing(price, threshold)
        self._discharge_reserve_mgr = _Reserve(reserve_kwh)
        self._global_discharge_blockers = dict(global_blockers or {})
        self._battery_discharge_blockers: dict = {}
        if battery_blockers:
            self._battery_discharge_blockers[self.coordinators[0]] = dict(
                battery_blockers
            )
        self.set_calls: list = []
        self.remove_calls: list = []

    # -- the real accessors the control path itself uses --------------------
    @staticmethod
    def _serialize(registry: dict) -> dict:
        return {
            source: {
                "reason": record.get("reason"),
                "details": dict(record.get("details") or {}),
            }
            for source, record in registry.items()
        }

    def get_discharge_blockers(self, coordinator=None) -> dict:
        if coordinator is None:
            return self._serialize(self._global_discharge_blockers)
        return self._serialize(
            self._battery_discharge_blockers.get(coordinator, {})
        )

    def get_battery_discharge_blockers(self) -> dict:
        return {
            coordinator.name: self._serialize(blockers)
            for coordinator, blockers in self._battery_discharge_blockers.items()
            if blockers
        }

    def _coordinator_delivered_power(self, coordinator):
        return coordinator.delivered

    def _known_batteries_for_block_summary(self) -> list:
        return list(self.coordinators)

    def set_discharge_block(self, *args, **kwargs) -> None:
        self.set_calls.append((args, kwargs))

    def remove_discharge_block(self, *args, **kwargs) -> None:
        self.remove_calls.append((args, kwargs))


def _build(controller: _Controller, clock: _Clock) -> DischargeDecisionTrace:
    return DischargeDecisionTrace(
        controller, now=clock.now, monotonic_fn=clock.monotonic
    )


def _only(trace: DischargeDecisionTrace) -> dict:
    """Return the one observed quarter (the snapshot also names earlier ones)."""
    records = trace.snapshot()["records"]
    observed = [record for record in records if record["verdict"] != "past"]
    assert len(observed) == 1
    return observed[0]


# ----------------------------------------------------------------------
# The real path is the source of the reason
# ----------------------------------------------------------------------


def test_quarter_that_discharged_is_named_discharging():
    controller = _Controller(
        coordinators=[_Coordinator(soc=55.0, delivered=-1500.0)],
        price=0.44,
        threshold=0.30,
        reserve_kwh=1.2,
    )
    clock = _Clock(_moment(62), mono=100.0)
    trace = _build(controller, clock)

    trace.observe()  # first cycle: no elapsed time yet, so no energy
    clock.mono += 60.0  # a minute later, still 1.5 kW out
    trace.observe()

    record = _only(trace)
    assert record["index"] == 62
    assert record["slot_start"].startswith("2026-08-30T15:30")
    assert record["verdict"] == "discharging"
    # 1500 W for 60 s, integrated from the measured delivery.
    assert record["discharge_kwh"] == pytest.approx(0.025, abs=1e-3)
    assert record["price"] == 0.44
    assert record["threshold"] == 0.30
    assert record["soc_pct"] == 55.0
    assert record["reserve_kwh"] == 1.2
    assert record["blockers"] == []


def test_price_blocked_quarter_names_the_threshold_the_path_compared():
    controller = _Controller(
        global_blockers={
            # The blocker keeps no threshold of its own here, so the two numbers
            # can only come from the pricing manager's live accessors.
            "price_discharge": {
                "reason": "price",
                "details": {"current_price": 0.21},
            }
        },
        price=0.21,
        threshold=0.30,
    )
    clock = _Clock(_moment(40))
    trace = _build(controller, clock)
    trace.observe()

    record = _only(trace)
    assert record["verdict"] == "price_blocked"
    assert record["blockers"] == ["price_discharge"]
    assert record["price"] == 0.21
    assert record["threshold"] == 0.30
    # The numbers came from the pricing manager's own accessors, not from a
    # second implementation of the comparison.
    assert controller._pricing_mgr.threshold_calls >= 1
    assert controller._pricing_mgr.price_calls >= 1


def test_threshold_falls_back_to_what_the_blocking_cycle_recorded():
    """Another pricing mode has no DP floor, so the blocker's own value is used."""
    controller = _Controller(
        global_blockers={
            "price_discharge": {
                "reason": "price",
                "details": {"current_price": 0.21, "threshold": 0.25},
            }
        },
        price=0.21,
        threshold=None,
    )
    clock = _Clock(_moment(40))
    trace = _build(controller, clock)
    trace.observe()

    assert _only(trace)["threshold"] == 0.25


def test_reserve_hold_is_named_reserve_blocked_with_its_floor():
    controller = _Controller(
        battery_blockers={
            "price_reserve": {
                "reason": "price_reserve",
                "details": {
                    "battery": "battery_1",
                    "soc": 41.5,
                    "min_soc": 30.0,
                    "reserved_floor": 42.0,
                    "reserve_soc_pct": 12.0,
                },
            }
        },
        price=0.30,
        threshold=0.25,
        reserve_kwh=1.75,
    )
    clock = _Clock(_moment(70))
    trace = _build(controller, clock)
    trace.observe()

    record = _only(trace)
    assert record["verdict"] == "reserve_blocked"
    assert record["blockers"] == ["price_reserve"]
    assert record["reserved_floor"] == 42.0
    assert record["reserve_kwh"] == 1.75


def test_safety_floor_is_named_floor_blocked():
    controller = _Controller(
        battery_blockers={
            "min_soc": {"reason": "min_soc", "details": {"effective_min_soc": 20.0}}
        }
    )
    clock = _Clock(_moment(50))
    trace = _build(controller, clock)
    trace.observe()

    assert _only(trace)["verdict"] == "floor_blocked"


def test_any_other_blocker_is_named_by_its_source():
    controller = _Controller(
        global_blockers={"ev_charging": {"reason": "ev_charging", "details": {}}}
    )
    clock = _Clock(_moment(50))
    trace = _build(controller, clock)
    trace.observe()

    record = _only(trace)
    assert record["verdict"] == "other:ev_charging"
    assert record["blockers"] == ["ev_charging"]


def test_price_floor_outranks_the_reserve_when_both_are_active():
    """Fixed priority: the economic price floor is the first reason."""
    controller = _Controller(
        global_blockers={"price_discharge": {"reason": "price", "details": {}}},
        battery_blockers={
            "price_reserve": {
                "reason": "price_reserve",
                "details": {"reserved_floor": 42.0},
            }
        },
    )
    clock = _Clock(_moment(50))
    trace = _build(controller, clock)
    trace.observe()

    record = _only(trace)
    assert record["verdict"] == "price_blocked"
    assert record["blockers"] == ["price_discharge", "price_reserve"]
    assert record["reserved_floor"] == 42.0


# ----------------------------------------------------------------------
# Missing data is named, never invented
# ----------------------------------------------------------------------


def test_quarter_without_a_price_or_telemetry_never_invents_a_zero():
    controller = _Controller(
        coordinators=[_Coordinator(soc=None, delivered=None)],
        price=None,
        threshold=None,
    )
    clock = _Clock(_moment(30))
    trace = _build(controller, clock)
    trace.observe()

    record = _only(trace)
    assert record["price"] is None
    assert record["threshold"] is None
    assert record["reserve_kwh"] is None
    assert record["reserved_floor"] is None
    assert record["soc_pct"] is None
    assert record["discharge_kwh"] is None
    assert record["verdict"] == "other:no_telemetry"


def test_a_long_gap_is_not_smeared_over_a_quarter():
    """Nobody observed the gap, so its energy is left out rather than guessed."""
    controller = _Controller(coordinators=[_Coordinator(soc=60.0, delivered=-2000.0)])
    clock = _Clock(_moment(20), mono=0.0)
    trace = _build(controller, clock)
    trace.observe()
    clock.mono += 3600.0  # an hour of downtime
    trace.observe()

    record = _only(trace)
    assert record["discharge_kwh"] == 0.0


def test_quarters_elapsed_before_the_trace_started_are_past():
    controller = _Controller(coordinators=[_Coordinator(soc=50.0, delivered=None)])
    clock = _Clock(_moment(61))
    trace = _build(controller, clock)
    trace.observe()

    payload = trace.snapshot()
    assert payload["record_count"] == 62
    assert payload["summary"]["past"] == 61
    first = payload["records"][0]
    assert first["index"] == 0
    assert first["slot_start"].startswith("2026-08-30T00:00")
    assert first["verdict"] == "past"
    assert first["price"] is None
    assert first["discharge_kwh"] is None
    assert first["blockers"] == []


def test_a_new_day_drops_yesterday_and_stays_bounded():
    controller = _Controller(coordinators=[_Coordinator(soc=50.0, delivered=None)])
    clock = _Clock(_moment(4))
    trace = _build(controller, clock)
    for index in range(0, MAX_RECORDS):
        clock.moment = _moment(index)
        trace.observe()
    assert trace.snapshot()["record_count"] == MAX_RECORDS

    clock.moment = datetime(2026, 8, 31, 0, 7, tzinfo=LOCAL)
    trace.observe()
    payload = trace.snapshot()
    assert payload["date"] == "2026-08-31"
    assert payload["record_count"] == 1
    assert payload["records"][0]["index"] == 0


def test_the_trace_is_actually_fed_by_the_real_blocker_pass():
    """Without this the feature could exist and never run in production."""
    source = inspect.getsource(ChargeDischargeController._refresh_operation_blockers)
    assert "_discharge_decision_trace" in source
    assert "observe()" in source


# ----------------------------------------------------------------------
# Measurement only
# ----------------------------------------------------------------------


def test_collecting_the_trace_changes_nothing_about_the_control_state():
    controller = _Controller(
        coordinators=[_Coordinator(soc=45.0, delivered=-800.0)],
        global_blockers={"price_discharge": {"reason": "price", "details": {}}},
        battery_blockers={
            "price_reserve": {"reason": "price_reserve", "details": {"reserved_floor": 40.0}}
        },
        price=0.2,
        threshold=0.3,
        reserve_kwh=1.5,
    )
    plan = controller._discharge_reserve_mgr

    def state() -> str:
        return json.dumps(
            {
                "global": controller._serialize(controller._global_discharge_blockers),
                "battery": controller.get_battery_discharge_blockers(),
                "reserve": plan.get_status(),
                "coordinators": [
                    {"name": c.name, "data": dict(c.data), "delivered": c.delivered}
                    for c in controller.coordinators
                ],
            },
            sort_keys=True,
        )

    before = state()
    clock = _Clock(_moment(60), mono=0.0)
    trace = _build(controller, clock)
    for _ in range(3):
        clock.mono += 30.0
        trace.observe()

    assert state() == before
    assert controller.set_calls == []
    assert controller.remove_calls == []
    # It did read the plan it may not change.
    assert controller._discharge_reserve_mgr is plan
    assert _only(trace)["index"] == 60


# ----------------------------------------------------------------------
# The diagnostics projection
# ----------------------------------------------------------------------


def _controller_with_trace() -> _Controller:
    controller = _Controller(
        global_blockers={"price_discharge": {"reason": "price", "details": {}}},
        battery_blockers={
            "price_reserve": {"reason": "price_reserve", "details": {"reserved_floor": 42.0}}
        },
        price=0.21,
        threshold=0.30,
        reserve_kwh=1.75,
    )
    clock = _Clock(_moment(61))
    trace = _build(controller, clock)
    trace.observe()
    controller._discharge_decision_trace = trace
    return controller


def test_diagnostics_payload_is_bounded_and_counts_every_verdict():
    payload = diagnostics._discharge_decisions(_controller_with_trace())

    assert payload["available"] is True
    assert payload["date"] == "2026-08-30"
    assert payload["interval_minutes"] == 15
    assert payload["record_count"] == 62
    assert payload["record_count"] <= 96
    assert payload["summary"] == {"past": 61, "price_blocked": 1}
    assert set(payload["records"][-1]) == {
        "index", "slot_start", "price", "threshold", "reserve_kwh",
        "reserved_floor", "soc_pct", "discharge_kwh", "verdict", "blockers",
    }
    assert payload["records"][-1]["reserved_floor"] == 42.0
    # Never a full series array next to the records.
    assert "series" not in payload


def test_diagnostics_records_are_json_safe():
    payload = diagnostics._discharge_decisions(_controller_with_trace())
    json.dumps(payload)
    for record in payload["records"]:
        assert record["blockers"] == sorted(record["blockers"])
        for value in record.values():
            assert value is None or isinstance(value, (str, int, float, list))


def test_diagnostics_without_a_trace_or_prices_never_crashes():
    for controller in (
        object(),
        _Controller(),
        type("Bare", (), {"_discharge_decision_trace": object()})(),
    ):
        payload = diagnostics._discharge_decisions(controller)
        assert payload["available"] is False
        assert payload["records"] == []
        assert payload["summary"] == {}
