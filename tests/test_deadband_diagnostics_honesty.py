"""S1 — diagnostics honesty for the display dead-band (commit 309e753d).

Commit 309e753d taught the tracking diary to ignore flows below
``ACTION_ENERGY_DEADBAND_KWH`` when it colours a quarter. That is right for the
graph, but it also made the diary *forget* the difference: a quarter that
carried three watt-hours and a quarter where nothing happened at all both read
as "no action", and the diagnostics counted both as ``unknown_cells``.

The diary now keeps the raw measurement next to the displayed mask, so the
counters can show both numbers: what the graph shows, and what the meters saw.
Existing keys keep their meaning (``by_action`` is still the displayed mask, and
``unknown_cells`` is still the graph's own count) — the new keys are additive.
"""
from __future__ import annotations

import copy
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery import diagnostics
from custom_components.omnibattery.pricing.daily_timeline import ACTION_DISCHARGE
from custom_components.omnibattery.tracking.daily_timeline import (
    DailyOperationTimelineManager,
)

MADRID = ZoneInfo("Europe/Madrid")


class FakeStore:
    """Async Store double retaining exactly what the manager writes."""

    def __init__(self, data=None):
        self.data = copy.deepcopy(data)
        self.writes: list[dict] = []

    async def async_load(self):
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        self.data = copy.deepcopy(data)
        self.writes.append(copy.deepcopy(data))


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _manager(clock, store=None):
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Madrid"))
    entry = SimpleNamespace(
        entry_id="entry-one",
        data={"pricing_mode": "dynamic_pricing"},
        options={},
    )
    return DailyOperationTimelineManager(
        hass,
        entry,
        SimpleNamespace(pricing_mode="dynamic_pricing"),
        store=store or FakeStore(),
        now_provider=clock,
        debounce_seconds=0.01,
    )


def _counts(manager) -> dict:
    return manager.build_public_snapshot()["counts"]


# ---------------------------------------------------------------------------
# The two numbers
# ---------------------------------------------------------------------------


def test_a_sub_deadband_flow_is_counted_raw_but_not_as_a_displayed_action():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)

    # 3 Wh of discharge: real, but about eleven seconds of flow.
    assert manager.record_runtime_decision({"battery_to_home_kwh": 0.003}) is True

    counts = _counts(manager)

    # The graph shows nothing: the dead-band still decides the colours.
    assert counts["by_action"]["discharge"] == 0
    # The measurement is not lost, though.
    assert counts["raw_by_action"]["discharge"] == 1
    assert counts["below_deadband_cells"] == 1


def test_a_real_flow_is_not_reported_as_hidden():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)

    assert manager.record_runtime_decision({"battery_to_home_kwh": 0.4}) is True

    counts = _counts(manager)

    assert counts["by_action"]["discharge"] == 1
    assert counts["raw_by_action"]["discharge"] == 1
    assert counts["below_deadband_cells"] == 0


def test_a_cell_that_displays_an_action_is_never_counted_as_hidden():
    """A stated action makes the cell visible; its tiny flow hides nothing."""
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)

    manager.record_runtime_decision(
        {"action_mask": ACTION_DISCHARGE, "battery_to_home_kwh": 0.003}
    )

    counts = _counts(manager)

    assert counts["by_action"]["discharge"] == 1
    assert counts["below_deadband_cells"] == 0
    assert counts["raw_by_action"]["discharge"] == 1


def test_the_two_counters_stay_equal_without_any_hidden_flow():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)

    assert manager.record_runtime_decision({"battery_to_home_kwh": 0.4}) is True
    assert manager.record_runtime_decision({"grid_to_battery_kwh": 1.2}) is True

    counts = _counts(manager)

    assert counts["raw_by_action"] == counts["by_action"]
    assert counts["below_deadband_cells"] == 0


# ---------------------------------------------------------------------------
# The diagnostics download
# ---------------------------------------------------------------------------


def test_the_diagnostics_summary_shows_both_numbers():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    manager = _manager(clock)
    manager.record_runtime_decision({"battery_to_home_kwh": 0.003})

    result = diagnostics._daily_operation_timeline_summary(
        SimpleNamespace(daily_operation_timeline=manager)
    )

    assert result["counts"]["by_action"]["discharge"] == 0
    assert result["counts"]["raw_by_action"]["discharge"] == 1
    assert result["counts"]["below_deadband_cells"] == 1
    # Still bounded: the raw numbers are counters, not 96-element arrays.
    assert "series" not in result
    assert "operations" not in result


def test_the_diagnostics_summary_tolerates_a_payload_without_the_new_counts():
    """An older or restored snapshot must not make the download explode."""
    result = diagnostics._daily_operation_timeline_summary(SimpleNamespace())

    assert result["counts"]["below_deadband_cells"] == 0
    assert result["counts"]["raw_by_action"] == result["counts"]["by_action"]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_hidden_measurement_survives_a_store_round_trip():
    clock = MutableClock(datetime(2026, 8, 23, 10, 7, tzinfo=MADRID))
    store = FakeStore()
    manager = _manager(clock, store)
    manager.record_runtime_decision({"battery_to_home_kwh": 0.003})
    await manager.async_save_all()

    restored = _manager(clock, store)
    assert await restored.async_load() is True

    counts = _counts(restored)

    assert counts["raw_by_action"]["discharge"] == 1
    assert counts["below_deadband_cells"] == 1
