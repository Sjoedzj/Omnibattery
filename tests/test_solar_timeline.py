"""Pure tests for the learned/provider solar timeline."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.pricing.solar_timeline import (
    _validate_periods,
    build_boundaries,
    build_solar_timeline,
    normalize_timeline_weights,
    provider_weights,
)
from custom_components.omnibattery.solar_forecast import (
    SolarForecastPeriod,
    periods_from_wh_hours,
)


MADRID = ZoneInfo("Europe/Madrid")


def _solar_window():
    return (
        datetime(2026, 8, 18, 8, 0, tzinfo=MADRID),
        datetime(2026, 8, 18, 20, 0, tzinfo=MADRID),
    )


def _horizon():
    return build_boundaries(
        datetime(2026, 8, 18, 9, 7, tzinfo=MADRID),
        datetime(2026, 8, 18, 20, 0, tzinfo=MADRID),
    )


def test_normalization_preserves_exact_budget():
    result = normalize_timeline_weights([1.0, 2.0, 3.0], 4.25)

    assert result is not None
    assert sum(result) == pytest.approx(4.25)


def test_partial_first_interval_keeps_actual_start_and_duration():
    boundaries = _horizon()

    assert boundaries[0][0].hour == 9
    assert boundaries[0][0].minute == 7
    assert (boundaries[0][1] - boundaries[0][0]).total_seconds() == pytest.approx(8 * 60)
    assert boundaries[-1][1].hour == 20
    assert sum((end - start).total_seconds() for start, end in boundaries) == pytest.approx(
        (datetime(2026, 8, 18, 20, 0, tzinfo=MADRID).timestamp()
         - datetime(2026, 8, 18, 9, 7, tzinfo=MADRID).timestamp())
    )


def test_naive_horizon_keeps_naive_boundaries_for_runtime_price_slots():
    start = datetime(2026, 8, 30, 0, 5)
    end = datetime(2026, 8, 31, 0, 0)

    boundaries = build_boundaries(start, end)

    assert boundaries
    assert all(item.tzinfo is None for boundary in boundaries for item in boundary)


@pytest.mark.parametrize(
    ("local_date", "expected_intervals", "expected_hours"),
    [
        (datetime(2026, 3, 29, tzinfo=MADRID), 92, 23.0),
        (datetime(2026, 10, 25, tzinfo=MADRID), 100, 25.0),
    ],
)
def test_boundaries_integrate_dst_days(
    local_date: datetime, expected_intervals: int, expected_hours: float
):
    boundaries = build_boundaries(local_date, local_date + timedelta(days=1))

    assert len(boundaries) == expected_intervals
    assert sum(end.timestamp() - start.timestamp() for start, end in boundaries) == pytest.approx(
        expected_hours * 3600
    )


def test_provider_periods_have_priority_and_are_resampled_to_horizon():
    solar_start, solar_end = _solar_window()
    provider = [SolarForecastPeriod(solar_start, solar_end, 8.0)]

    result = build_solar_timeline(
        _horizon(),
        6.0,
        provider_periods=provider,
        solar_start=solar_start,
        solar_end=solar_end,
        mode="active",
    )

    assert result.source == "provider"
    assert len(result.intervals_kwh) == len(_horizon())
    assert sum(result.intervals_kwh) == pytest.approx(6.0)
    assert result.energy_error_kwh == pytest.approx(0.0)


def test_provider_does_not_report_rejected_learned_candidate_as_fallback():
    solar_start, solar_end = _solar_window()
    boundaries = _horizon()

    result = build_solar_timeline(
        boundaries,
        6.0,
        temporal_shape=[1.0] * len(boundaries),
        learned_shape=[0.0] * 96,
        learned_mature=True,
        solar_start=solar_start,
        solar_end=solar_end,
        mode="active",
    )

    assert result.source == "provider"
    assert result.fallback_reason is None
    assert "learned_shape_no_future_energy" in result.candidate_reasons


def test_selected_sinusoidal_fallback_keeps_rejected_learned_reason():
    solar_start, solar_end = _solar_window()

    result = build_solar_timeline(
        _horizon(),
        3.0,
        learned_shape=[0.0] * 96,
        learned_mature=True,
        solar_start=solar_start,
        solar_end=solar_end,
        mode="active",
    )

    assert result.source == "sinusoidal"
    assert result.fallback_reason == "learned_shape_no_future_energy"
    assert result.candidate_reasons == ("learned_shape_no_future_energy",)


def test_zero_fallback_keeps_concrete_candidate_rejections():
    result = build_solar_timeline(
        [],
        3.0,
        temporal_shape=[1.0],
        learned_shape=[1.0] * 96,
        learned_mature=True,
        mode="active",
    )

    assert result.source == "zero_fallback"
    assert result.fallback_reason != "unsafe_temporal_shape"
    assert "legacy_shape_length" in (result.fallback_reason or "")
    assert result.candidate_reasons


def test_mature_profile_is_used_when_provider_is_absent():
    solar_start, solar_end = _solar_window()
    shape = [0.0] * 96
    shape[48] = 1.0

    result = build_solar_timeline(
        _horizon(),
        3.0,
        learned_shape=shape,
        learned_mature=True,
        solar_start=solar_start,
        solar_end=solar_end,
    )

    assert result.source == "learned_profile"
    assert sum(result.intervals_kwh) == pytest.approx(3.0)
    assert max(result.intervals_kwh) == pytest.approx(3.0)


def test_automatic_timeline_falls_back_to_sinusoidal_until_profile_is_mature():
    solar_start, solar_end = _solar_window()
    shape = [0.0] * 96
    shape[48] = 1.0

    result = build_solar_timeline(
        _horizon(),
        3.0,
        learned_shape=shape,
        learned_mature=False,
        solar_start=solar_start,
        solar_end=solar_end,
    )

    assert result.source == "sinusoidal"
    assert "profile_not_mature" in (result.fallback_reason or "")


def test_zero_solar_budget_suppresses_irrelevant_profile_fallback():
    solar_start, solar_end = _solar_window()

    result = build_solar_timeline(
        _horizon(),
        0.0,
        learned_shape=[1.0] * 96,
        learned_mature=False,
        solar_start=solar_start,
        solar_end=solar_end,
    )

    assert result.fallback_reason == "zero_budget"
    assert "profile_not_mature" in result.candidate_reasons


def test_shadow_keeps_sinusoidal_control_and_reports_selected_candidate():
    solar_start, solar_end = _solar_window()
    provider = [SolarForecastPeriod(solar_start, solar_end, 8.0)]

    result = build_solar_timeline(
        _horizon(),
        6.0,
        provider_periods=provider,
        solar_start=solar_start,
        solar_end=solar_end,
        mode="shadow",
    )

    assert result.source == "sinusoidal"
    assert result.shadow_selected_source == "provider"
    assert result.shadow_intervals_kwh
    assert sum(result.intervals_kwh) == pytest.approx(6.0)
    assert sum(result.shadow_intervals_kwh) == pytest.approx(6.0)


def test_safety_margin_is_applied_once_to_future_total():
    solar_start, solar_end = _solar_window()

    result = build_solar_timeline(
        _horizon(),
        5.0,
        safety_margin_kwh=1.25,
        solar_start=solar_start,
        solar_end=solar_end,
        mode="off",
    )

    assert result.remaining_raw_kwh == pytest.approx(5.0)
    assert result.remaining_effective_kwh == pytest.approx(3.75)
    assert sum(result.intervals_kwh) == pytest.approx(3.75)


def test_legacy_positional_shape_is_used_only_for_the_exact_horizon():
    solar_start, solar_end = _solar_window()
    boundaries = _horizon()
    shape = [0.0] * len(boundaries)
    shape[0] = 1.0

    result = build_solar_timeline(
        boundaries,
        2.0,
        temporal_shape=shape,
        solar_start=solar_start,
        solar_end=solar_end,
        mode="active",
    )
    assert result.source == "provider"
    assert result.intervals_kwh[0] == pytest.approx(2.0)

    invalid = build_solar_timeline(
        boundaries,
        2.0,
        temporal_shape=[1.0],
        solar_start=solar_start,
        solar_end=solar_end,
        mode="active",
    )
    assert invalid.source == "sinusoidal"
    assert "legacy_shape_length" in (invalid.fallback_reason or "")


def _hourly_periods(start, hours: int, energy_kwh: float) -> list[SolarForecastPeriod]:
    return [
        SolarForecastPeriod(
            start + timedelta(hours=index),
            start + timedelta(hours=index + 1),
            energy_kwh,
        )
        for index in range(hours)
    ]


def test_provider_curve_reaching_tomorrow_is_not_a_gap():
    """A 48 h provider curve (Solcast, Forecast.Solar) covers tomorrow too."""
    solar_start, solar_end = _solar_window()
    provider = _hourly_periods(solar_start, 12, 0.4) + _hourly_periods(
        solar_start + timedelta(days=1), 12, 0.4
    )
    boundaries = build_boundaries(
        datetime(2026, 8, 18, 9, 0, tzinfo=MADRID),
        datetime(2026, 8, 18, 18, 0, tzinfo=MADRID),
    )

    assert _validate_periods(
        provider, boundaries[0][0], boundaries[-1][1], solar_start, solar_end
    ) == (True, None)

    weights, reason = provider_weights(
        boundaries, provider, solar_start=solar_start, solar_end=solar_end
    )

    assert reason is None
    # Only today's window counts: nine daylight hours of 0.4 kWh.
    assert sum(weights) == pytest.approx(0.4 * 9)


def test_provider_gap_inside_today_is_still_rejected_next_to_tomorrow():
    """Tomorrow's curve must not bless a real hole in today's daylight."""
    solar_start, solar_end = _solar_window()
    provider = [
        SolarForecastPeriod(solar_start, solar_start + timedelta(hours=4), 1.6),
        SolarForecastPeriod(
            solar_start + timedelta(hours=6), solar_start + timedelta(hours=12), 2.4
        ),
    ] + _hourly_periods(solar_start + timedelta(days=1), 12, 0.4)

    assert _validate_periods(
        provider, solar_start, solar_end, solar_start, solar_end
    ) == (False, "provider_gap")


def test_sparse_daylight_curve_is_still_rejected_for_coverage():
    """Tomorrow's periods must not paper over a thin today either."""
    solar_start, solar_end = _solar_window()
    # Half-hour periods with 40-minute holes: no gap exceeds an hour, yet only
    # 300 of the 720 daylight minutes carry energy.
    sparse = [
        SolarForecastPeriod(
            solar_start + timedelta(minutes=70 * index),
            solar_start + timedelta(minutes=70 * index + 30),
            0.5,
        )
        for index in range(10)
    ] + _hourly_periods(solar_start + timedelta(days=1), 12, 0.4)

    assert _validate_periods(
        sparse, solar_start, solar_end, solar_start, solar_end
    ) == (False, "provider_coverage")


def test_three_summed_arrays_with_one_off_grid_key_keep_an_hourly_spread():
    """#558: the reported symptom — an hour of energy in one quarter hour."""
    solar_start, solar_end = _solar_window()
    boundaries = build_boundaries(
        datetime(2026, 8, 18, 9, 0, tzinfo=MADRID),
        datetime(2026, 8, 18, 18, 0, tzinfo=MADRID),
    )
    # Two arrays publish hourly; the third publishes its noon key ten minutes
    # late, so the summed curve holds an off-grid key.
    wh_hours = {
        (solar_start + timedelta(hours=index)).isoformat(): (
            2000.0 if index == 4 else 3000.0
        )
        for index in range(12)
    }
    wh_hours[(solar_start + timedelta(hours=4, minutes=10)).isoformat()] = 1000.0
    provider = periods_from_wh_hours(wh_hours)

    assert [period.end - period.start for period in provider] == [timedelta(hours=1)] * 12

    result = build_solar_timeline(
        boundaries,
        27.0,
        provider_periods=provider,
        solar_start=solar_start,
        solar_end=solar_end,
        mode="active",
    )

    assert result.source == "provider"
    assert result.fallback_reason is None
    assert sum(result.intervals_kwh) == pytest.approx(27.0)
    # No quarter is empty and no quarter carries half of its own hour.
    assert all(value > 0.0 for value in result.intervals_kwh)
    noon = result.intervals_kwh[12:16]  # 12:00-13:00, three hours into the horizon
    assert max(noon) < 0.5 * sum(noon)
