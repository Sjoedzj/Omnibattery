"""Bounded per-series shape in the daily-operation diagnostics dump.

A user reported "the expected consumption stays at 0 until 15:15" and could
not answer it from the download: the dump only carried counts, so the 96-cell
series never left the entity. These tests pin down the additive
``series_spans`` block: counts and the zero/non-zero edges per series, never
the arrays themselves.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from custom_components.omnibattery import diagnostics

INTERVALS = 96


def _summary(series: dict[str, object]) -> dict:
    """Run the diagnostics summary for a duck-typed timeline mapping."""
    controller = SimpleNamespace(
        daily_operation_timeline={
            "local_date": "2026-08-23",
            "series": series,
        }
    )
    return diagnostics._daily_operation_timeline_summary(controller)


def _series(values: object) -> list[object]:
    """Pad/truncate a test series to the 96-cell convention."""
    cells = list(values)  # type: ignore[arg-type]
    return (cells + [None] * INTERVALS)[:INTERVALS]


def test_a_series_that_only_starts_at_1515_reports_sixty_one_zero_cells():
    consumption = _series([0.0] * 61 + [0.42] * 35)

    span = _summary({"consumption_forecast_kwh": consumption})["series_spans"][
        "consumption_forecast_kwh"
    ]

    assert span["present_cells"] == 96
    assert span["zero_cells"] == 61
    assert span["first_nonzero_quarter"] == 61
    assert span["first_nonzero_time"] == "15:15"
    assert span["last_nonzero_quarter"] == 95
    assert span["last_nonzero_time"] == "23:45"
    assert span["first_zero_run_end"] == 61


def test_absent_cells_are_not_counted_as_zero():
    # Ten quarters without a measurement, five measured zeros, then real flow.
    consumption = _series([None] * 10 + [0.0] * 5 + [0.3] * 81)

    span = _summary({"consumption_forecast_kwh": consumption})["series_spans"][
        "consumption_forecast_kwh"
    ]

    assert span["present_cells"] == 86  # 96 - 10 absent, not 96
    assert span["zero_cells"] == 5  # only the measured zeros
    assert span["first_nonzero_quarter"] == 15
    assert span["first_nonzero_time"] == "03:45"


def test_a_fully_absent_series_has_no_nonzero_quarter():
    span = _summary({"consumption_forecast_kwh": [None] * INTERVALS})[
        "series_spans"
    ]["consumption_forecast_kwh"]

    assert span["present_cells"] == 0
    assert span["zero_cells"] == 0
    assert span["first_nonzero_quarter"] is None
    assert span["first_nonzero_time"] is None
    assert span["last_nonzero_quarter"] is None
    assert span["last_nonzero_time"] is None
    assert span["first_zero_run_end"] == 0


def test_a_series_that_begins_with_a_value_has_no_leading_zero_run():
    span = _summary({"solar_actual_kwh": _series([0.5] * INTERVALS)})[
        "series_spans"
    ]["solar_actual_kwh"]

    assert span["first_zero_run_end"] == 0
    assert span["zero_cells"] == 0
    assert span["first_nonzero_quarter"] == 0
    assert span["first_nonzero_time"] == "00:00"


def test_all_four_series_are_summarised_with_exactly_seven_scalars():
    series = {
        "consumption_forecast_kwh": _series([0.0] * 61 + [0.4] * 35),
        "solar_forecast_kwh": _series([0.1] * INTERVALS),
        "solar_actual_kwh": _series([0.2] * INTERVALS),
        "consumption_actual_kwh": _series([0.3] * INTERVALS),
    }

    spans = _summary(series)["series_spans"]

    assert set(spans) == {
        "consumption_forecast_kwh",
        "solar_forecast_kwh",
        "solar_actual_kwh",
        "consumption_actual_kwh",
    }
    expected_fields = {
        "present_cells",
        "zero_cells",
        "first_nonzero_quarter",
        "first_nonzero_time",
        "last_nonzero_quarter",
        "last_nonzero_time",
        "first_zero_run_end",
    }
    for key, span in spans.items():
        assert set(span) == expected_fields, key
        assert all(isinstance(value, (int, str, type(None))) for value in span.values())


def test_the_download_contains_no_96_cell_arrays():
    series = {
        "consumption_forecast_kwh": _series([0.0] * 61 + [0.4] * 35),
        "solar_forecast_kwh": _series([0.1] * INTERVALS),
        "solar_actual_kwh": _series([0.2] * INTERVALS),
        "consumption_actual_kwh": _series([0.3] * INTERVALS),
    }

    summary = _summary(series)

    # The shape is there (so the download answers "where does the zero stop"),
    # but the arrays it came from are not.
    assert set(summary["series_spans"]) == set(series)
    assert "series" not in summary
    assert "operations" not in summary

    def _max_list_length(value: object) -> int:
        if isinstance(value, dict):
            return max((_max_list_length(item) for item in value.values()), default=0)
        if isinstance(value, (list, tuple)):
            return max(
                [len(value)]
                + [_max_list_length(item) for item in value]
            )
        return 0

    assert _max_list_length(summary) < INTERVALS
    # The whole summary must stay a small download, not a 96x4 number dump.
    assert len(json.dumps(summary, allow_nan=False)) < 4000
