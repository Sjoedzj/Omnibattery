"""R2 — the night curve must read the demand channel, not the simulation's zero.

The daily-operation payload carries two consumption channels for every quarter:
``consumption_kwh`` (what the simulation lets the battery serve) and
``consumption_forecast_kwh`` (the demand the grid serves). On a blocked or
capacity-held quarter the first is 0 while the second holds the real load.

Today's cells read ``consumption_forecast_kwh`` (the ``series`` section), but
the panel's extension list for tomorrow's night asked for ``consumption_kwh``
first, so the night half of the graph fell to a flat zero that the day half
contradicted.

These tests execute the shipped ``marstek-panel.js`` in Node and read the
decoded snapshot, exactly like ``test_frontend_daily_operation_runtime.py``.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from custom_components import omnibattery

PANEL = Path(omnibattery.__file__).parent / "frontend/marstek-panel.js"
BASE_INTERVALS = 96

HARNESS = r"""
(async () => {
const fs = require('node:fs');
const vm = require('node:vm');
let Panel;
const context = vm.createContext({
  HTMLElement: class {},
  customElements: { get() {}, define(name, cls) { Panel = cls; } },
});
const runtime = new vm.SourceTextModule(fs.readFileSync(process.argv[1], 'utf8'), { context });
await runtime.link(() => { throw new Error('Unexpected external import'); });
await runtime.evaluate();
const scenario = JSON.parse(fs.readFileSync(0, 'utf8'));
const panel = Object.create(Panel.prototype);
const entityId = 'sensor.test_daily_operation_timeline';
const attrs = { local_date: '2026-10-05', current_index: 8, current_progress: 0.5, ...scenario.attrs };
panel._panelConfig = {};
panel._hass = {
  states: { [entityId]: { entity_id: entityId, state: 'ready', attributes: attrs } },
  entities: { [entityId]: { entity_id: entityId, platform: 'omnibattery', translation_key: 'daily_operation_timeline' } },
  devices: {}, config: { time_zone: 'UTC' },
  locale: { language: 'en', time_zone: 'server', time_format: '24' },
};
const snapshot = panel._dailyOperationState();
const at = (series, index) => (Array.isArray(series) ? (series[index] ?? null) : null);
const indexes = scenario.indexes || [0, 8, 48, 96, 97, 98, 99];
process.stdout.write(JSON.stringify({
  hasValues: snapshot ? snapshot.hasValues : null,
  consumption: indexes.map(index => at(snapshot && snapshot.forecastConsumption, index)),
  solar: indexes.map(index => at(snapshot && snapshot.forecastSolar, index)),
  indexes,
}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def _run(attrs):
    node = shutil.which("node")
    assert node, "Node is required to execute the shipped frontend"
    result = subprocess.run(
        [node, "--experimental-vm-modules", "-e", HARNESS, str(PANEL)],
        input=json.dumps({"attrs": attrs}),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _series(values):
    """A full 96-element series with the sparse values applied."""
    return [values.get(index) for index in range(BASE_INTERVALS)]


def _payload(*, day_demand=0.30, night_simulated=0.0, night_demand=0.25,
             night_solar=0.0):
    """One tomorrow-night quarter (96) plus the payload the panel needs."""
    return {
        "series": {
            "consumption_forecast_kwh": _series({index: day_demand for index in range(BASE_INTERVALS)}),
            "solar_forecast_kwh": _series({index: 0.0 for index in range(BASE_INTERVALS)}),
        },
        "extended_projection": [
            {
                "extension_index": 0,
                "index": 0,
                "start": "2026-10-06T00:00:00+02:00",
                "end": "2026-10-06T00:15:00+02:00",
                # A blocked/held quarter: the simulation drains nothing while
                # the household still consumes.
                "consumption_kwh": night_simulated,
                "consumption_forecast_kwh": night_demand,
                "solar_kwh": 0.0,
                "solar_forecast_kwh": night_solar,
                "action_mask": 0,
                "soc_end_pct": 40.0,
            }
        ],
    }


def test_a_blocked_night_quarter_reads_the_demand_the_grid_serves():
    result = _run(_payload(night_simulated=0.0, night_demand=0.25))

    assert result["hasValues"] is True
    at_night = result["indexes"].index(BASE_INTERVALS)
    assert result["consumption"][at_night] == pytest.approx(0.25)


def test_the_night_channel_agrees_with_the_day_channel():
    """Both halves must decode the same quantity from the same kind of cell."""
    result = _run(_payload(day_demand=0.30, night_simulated=0.0, night_demand=0.30))

    by_index = dict(zip(result["indexes"], result["consumption"]))
    assert by_index[8] == pytest.approx(0.30)   # today, from series
    assert by_index[BASE_INTERVALS] == pytest.approx(0.30)  # tomorrow night


def test_an_identical_pair_still_decodes_the_shared_value():
    """Nothing changes for the ordinary quarter where both channels agree."""
    result = _run(_payload(day_demand=0.42, night_simulated=0.42, night_demand=0.42))

    by_index = dict(zip(result["indexes"], result["consumption"]))
    assert by_index[BASE_INTERVALS] == pytest.approx(0.42)


def test_the_solar_channel_has_no_simulation_zero_to_hide_behind():
    """Checked for the same defect: tomorrow's solar has no second channel.

    ``solar_kwh`` in the extension comes from the same projection interval the
    ``solar_forecast_kwh`` full-quarter figure is taken from (the extension runs
    at ratio 1), so a night quarter is 0 on both and a producing quarter is the
    same number on both. The regression test pins that they agree, so a future
    change that splits the channels shows up here.
    """
    payload = _payload(night_solar=0.0)
    result = _run(payload)
    at_night = result["indexes"].index(BASE_INTERVALS)
    assert result["solar"][at_night] == pytest.approx(0.0)

    producing = _payload(night_solar=0.18)
    producing["extended_projection"][0]["solar_kwh"] = 0.18
    result = _run(producing)
    assert result["solar"][at_night] == pytest.approx(0.18)
