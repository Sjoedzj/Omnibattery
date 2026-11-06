"""R7 — the solar circularity guard must be real, not a form-validation detail.

The options flow refuses to *save* an OmniBattery entity as a solar forecast
sensor (``config_flow._validate_solar_forecast_sensors``), but that is a UI
check at one moment in time. Two holes remained:

* the read path (``solar_forecast.read_solar_forecast_kwh``) never checked, so an
  entry stored before the validator existed — or restored by
  ``_restore_unrenderable_sensors`` — fed the integration's own output into the
  planner that produces it;
* ``_restore_unrenderable_sensors`` runs *after* the validators and re-adds a
  stored value they never saw, so it could reintroduce exactly what the flow
  was refusing to save.

Everything here is a runtime path: no Home Assistant runtime is started.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.omnibattery import config_flow, solar_forecast
from custom_components.omnibattery.const import (
    CONF_SOLAR_FORECAST_REMAINING_SENSOR,
    CONF_SOLAR_FORECAST_SENSOR,
    CONF_SOLAR_PRODUCTION_SENSOR,
)

OWN_FORECAST = "sensor.omnibattery_solar_forecast"
OWN_PRODUCTION = "sensor.marstek_venus_system_solar_power"
LEGITIMATE = "sensor.openmeteo_forecast_today"


def _state(kwh: float):
    return SimpleNamespace(state=str(kwh), attributes={"unit_of_measurement": "kWh"})


def _hass(states=None):
    mapping = states or {}
    return SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: mapping.get(entity_id)))


def _controller(**data):
    return SimpleNamespace(
        config_entry=SimpleNamespace(data=dict(data), options={}),
        solar_forecast_source=None,
        solar_forecast_diagnostic_source=None,
        solar_forecast_periods=None,
    )


# ---------------------------------------------------------------------------
# Runtime read path
# ---------------------------------------------------------------------------


def test_the_read_path_refuses_to_consume_our_own_forecast_entity(caplog):
    controller = _controller(**{CONF_SOLAR_FORECAST_SENSOR: OWN_FORECAST})
    hass = _hass({OWN_FORECAST: _state(12.5)})

    forecast = solar_forecast.read_solar_forecast_kwh(hass, controller)

    assert forecast is None
    assert controller.solar_forecast_source is None
    assert "circular reference" in caplog.text
    assert OWN_FORECAST in caplog.text


def test_the_read_path_refuses_the_remaining_horizon_too():
    controller = _controller(**{CONF_SOLAR_FORECAST_REMAINING_SENSOR: OWN_FORECAST})
    hass = _hass({OWN_FORECAST: _state(4.0)})

    assert solar_forecast.read_solar_forecast_kwh(hass, controller) is None


def test_a_circular_remaining_sensor_still_falls_through_to_the_legacy_one():
    """The guard skips that candidate; it does not disable the adapter."""
    controller = _controller(
        **{
            CONF_SOLAR_FORECAST_REMAINING_SENSOR: OWN_FORECAST,
            CONF_SOLAR_FORECAST_SENSOR: LEGITIMATE,
        }
    )
    hass = _hass({OWN_FORECAST: _state(4.0), LEGITIMATE: _state(9.5)})

    forecast = solar_forecast.read_solar_forecast_kwh(hass, controller)

    assert forecast is not None
    assert forecast.kwh == pytest.approx(9.5)
    assert forecast.source == "today"
    assert controller.solar_forecast_source == "today"


def test_a_normal_sensor_is_never_touched_by_the_guard(caplog):
    controller = _controller(**{CONF_SOLAR_FORECAST_SENSOR: LEGITIMATE})
    hass = _hass({LEGITIMATE: _state(7.25)})

    forecast = solar_forecast.read_solar_forecast_kwh(hass, controller)

    assert forecast is not None
    assert forecast.kwh == pytest.approx(7.25)
    assert "circular" not in caplog.text


def test_the_guard_warns_once_per_controller(caplog):
    """A once-per-lifetime error, like the consumption tracker's own guard."""
    controller = _controller(**{CONF_SOLAR_FORECAST_SENSOR: OWN_FORECAST})
    hass = _hass({OWN_FORECAST: _state(12.5)})

    for _ in range(3):
        assert solar_forecast.read_solar_forecast_kwh(hass, controller) is None

    assert caplog.text.count("circular reference") == 1


def test_the_production_remaining_reader_gets_the_same_guard():
    """``read_remaining_solar_kwh`` is the path the planner actually calls."""
    controller = _controller(**{CONF_SOLAR_FORECAST_SENSOR: OWN_FORECAST})
    hass = _hass({OWN_FORECAST: _state(12.5)})

    result = solar_forecast.read_remaining_solar_kwh(hass, controller)

    assert result.remaining_kwh == 0.0
    assert result.original_source is None
    assert result.conversion == "unsafe_zero"
    assert controller.solar_forecast_source == "fallback"


# ---------------------------------------------------------------------------
# The options-flow restore path
# ---------------------------------------------------------------------------


def test_a_missing_circular_forecast_sensor_is_not_restored(caplog):
    """The validators already ran; they never saw the value this would invent."""
    hass = _hass()
    stored = {CONF_SOLAR_FORECAST_SENSOR: OWN_FORECAST}

    restored = config_flow._restore_unrenderable_sensors(hass, {}, stored)

    assert CONF_SOLAR_FORECAST_SENSOR not in restored
    assert "circular reference" in caplog.text


def test_a_missing_circular_remaining_sensor_is_not_restored():
    hass = _hass()
    stored = {CONF_SOLAR_FORECAST_REMAINING_SENSOR: OWN_FORECAST}

    restored = config_flow._restore_unrenderable_sensors(hass, {}, stored)

    assert CONF_SOLAR_FORECAST_REMAINING_SENSOR not in restored


def test_a_missing_circular_production_sensor_is_not_restored():
    """The production sensor is an input too, with its own validator."""
    hass = _hass()
    stored = {CONF_SOLAR_PRODUCTION_SENSOR: OWN_PRODUCTION}

    restored = config_flow._restore_unrenderable_sensors(hass, {}, stored)

    assert CONF_SOLAR_PRODUCTION_SENSOR not in restored


def test_a_missing_third_party_sensor_is_still_preserved():
    """The #419 behaviour this function exists for must not regress."""
    hass = _hass()
    stored = {CONF_SOLAR_FORECAST_SENSOR: LEGITIMATE}

    restored = config_flow._restore_unrenderable_sensors(hass, {}, stored)

    assert restored[CONF_SOLAR_FORECAST_SENSOR] == LEGITIMATE


def test_an_existing_sensor_still_clears_normally():
    """Only the dead-reference case restores; a live clear is a user decision."""
    hass = _hass({LEGITIMATE: _state(7.25)})
    stored = {CONF_SOLAR_FORECAST_SENSOR: LEGITIMATE}

    restored = config_flow._restore_unrenderable_sensors(hass, {}, stored)

    assert CONF_SOLAR_FORECAST_SENSOR not in restored
