"""Quarter-hour price curve behind the daily chart's price strip.

The user asked to see, per quarter, whether the battery charges in the cheap
hours and discharges on the peaks. That needs the *whole* local day, while every
price-aware decision must keep planning on the remaining (future) slots only.
These tests pin the three contracts that makes that possible:

* the sensor publishes a 96-cell ``price_kwh`` series plus one ``price_unit``;
* a quarter without a price is ``None``, never ``0`` (a missing price is not free
  electricity), while a genuine ``0.0`` price survives;
* the series stays inside ``series``, which the recorder skips.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components import omnibattery
from custom_components.omnibattery.const import PRICE_INTEGRATION_EPEX
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.engine import PriceSlot, PricingManager
from custom_components.omnibattery.sensor import (
    DailyOperationTimelineSensor,
    _daily_operation_timeline_attributes,
)

DAY = "2026-08-30"
PANEL = Path(omnibattery.__file__).parent / "frontend/marstek-panel.js"


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 30, hour, minute)


class _Pricing:
    """Stand-in for ``controller.pricing`` that records how it was asked."""

    def __init__(self, slots, unit="€/kWh") -> None:
        self._slots = list(slots)
        self._unit = unit
        self.calls: list[bool] = []

    def get_future_price_slots(self, horizon_end=None, *, include_past=False):
        self.calls.append(include_past)
        return list(self._slots)

    def _get_price_unit(self):
        return self._unit


class _Manager:
    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> dict:
        return self._snapshot


def _attributes(slots, unit="€/kWh"):
    pricing = _Pricing(slots, unit)
    controller = SimpleNamespace(
        daily_operation_timeline=_Manager({"local_date": DAY}),
        pricing=pricing,
    )
    return _daily_operation_timeline_attributes(controller), pricing


def _hourly(start_hour: int, end_hour: int, price: float) -> PriceSlot:
    return PriceSlot(start=_at(start_hour), end=_at(end_hour), price=price)


# ----------------------------------------------------------------------
# The published series
# ----------------------------------------------------------------------

def test_price_series_has_96_quarters_and_null_where_no_price_exists():
    # Only the first ten hours of the day carry a price (40 quarters).
    slots = [_hourly(hour, hour + 1, 0.25) for hour in range(10)]

    attributes, pricing = _attributes(slots)
    series = attributes["series"]["price_kwh"]

    assert len(series) == 96
    assert series[:40] == [0.25] * 40
    # A quarter without a price is absent, not zero: never "" or 0.0.
    assert series[40:] == [None] * 56
    assert all(value is None for value in series[40:])
    # The whole day is requested, otherwise the elapsed half would be missing.
    assert pricing.calls == [True]


def test_hourly_price_fills_its_four_quarters():
    # 12:00-13:00 only: quarters 48..51 are priced, the rest of the day is not.
    attributes, _ = _attributes([_hourly(12, 13, 0.31)])
    series = attributes["series"]["price_kwh"]

    assert series[48:52] == [0.31] * 4
    assert series[47] is None
    assert series[52] is None


def test_quarter_without_a_price_is_null_while_a_free_quarter_stays_zero():
    attributes, _ = _attributes(
        [
            _hourly(10, 11, 0.0),  # a real free hour: 0.0 must survive
            # 11:00-12:00 has no price at all
            _hourly(12, 13, 0.45),
        ]
    )
    series = attributes["series"]["price_kwh"]

    assert series[40] == 0.0
    assert series[43] == 0.0
    assert series[44] is None
    assert series[45] is None
    assert series[48] == 0.45


def test_price_unit_is_published_as_a_scalar():
    attributes, _ = _attributes([_hourly(12, 13, 0.31)])

    assert attributes["price_unit"] == "€/kWh"


def test_price_unit_and_curve_degrade_gracefully_without_pricing():
    controller = SimpleNamespace(daily_operation_timeline=_Manager({"local_date": DAY}))
    attributes = _daily_operation_timeline_attributes(controller)

    assert attributes["price_unit"] is None
    assert attributes["series"]["price_kwh"] == [None] * 96


def test_price_series_is_not_in_the_recorded_attributes():
    attributes, _ = _attributes([_hourly(12, 13, 0.31)])

    # Inside ``series`` only: a top-level list would be recorded on every state
    # write, which is exactly what ``_unrecorded_attributes`` exists to prevent.
    assert "price_kwh" in attributes["series"]
    assert "price_kwh" not in attributes
    assert "series" in DailyOperationTimelineSensor._unrecorded_attributes
    assert "price_kwh" in DailyOperationTimelineSensor._unrecorded_attributes
    assert "operations" in DailyOperationTimelineSensor._unrecorded_attributes


def test_price_curve_falls_back_to_the_future_only_accessor():
    class _LegacyPricing:
        def get_future_price_slots(self):  # no include_past parameter
            return [_hourly(12, 13, 0.2)]

        def _get_price_unit(self):
            return "€/kWh"

    controller = SimpleNamespace(
        daily_operation_timeline=_Manager({"local_date": DAY}),
        pricing=_LegacyPricing(),
    )
    attributes = _daily_operation_timeline_attributes(controller)

    assert attributes["series"]["price_kwh"][48:52] == [0.2] * 4


# ----------------------------------------------------------------------
# The pricing engine seam
# ----------------------------------------------------------------------

def _fixed_clock(monkeypatch, now: datetime) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(pricing_engine, "datetime", FixedDateTime)


def _manager(monkeypatch, entries):
    monkeypatch.setattr(
        pricing_engine, "resolve_official_nordpool_source", lambda *_args: None
    )
    state = SimpleNamespace(state="0.10", attributes={"data": entries})
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: state))
    ctrl = SimpleNamespace(
        hass=hass,
        price_sensor="sensor.dynamic_price",
        price_integration_type=PRICE_INTEGRATION_EPEX,
        _price_data_status="not_evaluated",
    )
    return PricingManager(hass, ctrl), ctrl


def _epex_entries(first_hour: int, last_hour: int, price: float = 0.21):
    """Hourly EPEX entries on ``DAY`` between ``first_hour`` and ``last_hour``."""
    midnight = _at(0)
    return [
        {
            "start_time": (midnight + timedelta(hours=hour)).isoformat(),
            "end_time": (midnight + timedelta(hours=hour + 1)).isoformat(),
            "price_per_kwh": price,
        }
        for hour in range(first_hour, last_hour)
    ]


def test_include_past_adds_the_elapsed_quarters_and_keeps_future_only_default(monkeypatch):
    _fixed_clock(monkeypatch, _at(12))
    manager, _ctrl = _manager(monkeypatch, _epex_entries(0, 24))

    future = manager.get_future_price_slots()
    day = manager.get_future_price_slots(include_past=True)
    # The future-only cache must survive the whole-day read.
    future_again = manager.get_future_price_slots()

    assert [slot.start.hour for slot in future] == list(range(12, 24))
    assert [slot.start.hour for slot in day] == list(range(24))
    assert [slot.start.hour for slot in future_again] == list(range(12, 24))


def test_include_past_never_masks_the_missing_future_slots_status(monkeypatch):
    # Noon, but the feed only carries the (already ended) morning hours.
    _fixed_clock(monkeypatch, _at(12))
    manager, ctrl = _manager(monkeypatch, _epex_entries(0, 3))

    slots = manager._parse_price_data(include_past=True)

    assert [slot.start.hour for slot in slots] == [0, 1, 2]
    # The health check must still see "no future slots", not "ok (3 slots)".
    assert ctrl._price_data_status == "no_future_slots"


def test_filter_future_slots_moves_only_the_lower_bound(monkeypatch):
    _fixed_clock(monkeypatch, _at(12))
    elapsed = PriceSlot(start=_at(9), end=_at(10), price=0.5)
    running = PriceSlot(start=_at(11, 45), end=_at(12), price=0.4)
    upcoming = PriceSlot(start=_at(13), end=_at(14), price=0.1)
    tomorrow = PriceSlot(start=_at(0) + timedelta(days=1), end=_at(1) + timedelta(days=1), price=0.9)

    assert PricingManager._filter_future_slots([elapsed, running, upcoming, tomorrow]) == [
        upcoming
    ]
    assert PricingManager._filter_future_slots(
        [elapsed, running, upcoming, tomorrow], include_past=True
    ) == [elapsed, running, upcoming]


@pytest.mark.parametrize("include_past", [False, True])
def test_parse_price_data_defaults_stay_future_only(monkeypatch, include_past):
    _fixed_clock(monkeypatch, _at(12))
    manager, ctrl = _manager(monkeypatch, _epex_entries(0, 24))

    slots = manager._parse_price_data(include_past=include_past)

    assert [slot.start.hour for slot in slots] == (
        list(range(24)) if include_past else list(range(12, 24))
    )
    assert ctrl._price_data_status == "ok (12 slots)"

# ----------------------------------------------------------------------
# The panel: price strip, own axis and the tooltip row
# ----------------------------------------------------------------------

PANEL_HARNESS = r"""
(async () => {
  const fs = require('node:fs');
  const vm = require('node:vm');
  let Panel;
  const context = vm.createContext({
    HTMLElement: class {},
    customElements: { get() {}, define(_name, cls) { Panel = cls; } },
  });
  const runtime = new vm.SourceTextModule(fs.readFileSync(process.argv[1], 'utf8'), { context });
  await runtime.link(() => { throw new Error('Unexpected external import'); });
  await runtime.evaluate();

  const scenario = JSON.parse(fs.readFileSync(0, 'utf8'));
  const panel = Object.create(Panel.prototype);
  const entityId = 'sensor.test_daily_operation_timeline';
  panel._panelConfig = {};
  panel._hass = {
    states: { [entityId]: { entity_id: entityId, state: 'ready', attributes: scenario.attrs } },
    entities: { [entityId]: { entity_id: entityId, platform: 'omnibattery', translation_key: 'daily_operation_timeline' } },
    devices: {}, config: { time_zone: 'UTC' },
    locale: { language: scenario.lang, time_zone: 'server', time_format: '24' },
  };
  const node = () => ({
    style: {}, attributes: {}, toggles: {}, children: {}, className: '',
    hidden: false, textContent: '', innerHTML: '',
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; },
    querySelector(selector) { return this.children[selector] ||= node(); },
    get classList() { return { toggle: (key, value) => { this.toggles[key] = !!value; } }; },
  });
  const ref = Object.fromEntries(
    ('card badge notice hourlyBalanceLegend priceLegend priceAxis priceAxisUnit '
      + 'yAxis socAxis viewport stage tooltip previous next nowMarker nowText')
      .split(' ').map(key => [key, node()])
  );
  ref.priceBars = Array.from({ length: 96 }, node);
  ref.priceAxisTicks = [node(), node()];
  ref.priceZero = node();
  ref.viewport.clientWidth = 480;
  ref.viewport.scrollWidth = ref.stage.scrollWidth = 1440;
  ref.viewport.scrollLeft = 0;
  ref.scrollState = { manual: false, initialized: false, programmaticUntil: 0 };
  ref.cells = Array.from({ length: 144 }, node);
  ref.paths = Object.fromEntries(
    'solarActual solarForecast consumptionActual consumptionForecast socActual socForecast'
      .split(' ').map(key => [key, node()])
  );
  panel._r = { dailyOperation: ref };

  const snapshot = panel._dailyOperationState();
  panel._patchDailyOperationTimeline();
  const item = panel._dailyOperationItem(snapshot, 0);
  const bars = ref.priceBars.slice(0, 4).map(bar => ({
    height: bar.attributes.height === undefined ? null : Number(bar.attributes.height),
    display: bar.style.display === undefined ? '' : bar.style.display,
    negative: bar.toggles['daily-op-price-negative'] === true,
  }));
  process.stdout.write(JSON.stringify({
    hasPrice: snapshot.hasPrice,
    priceUnit: snapshot.priceUnit,
    firstPrice: item.price,
    priceLabel: panel._t('dailyOperationPrice'),
    tooltip: panel._dailyOperationTooltipHTML(snapshot, item),
    stageHasPrice: ref.stage.toggles['daily-op-has-price'] === true,
    legendHidden: ref.priceLegend.hidden,
    axisHidden: ref.priceAxis.hidden,
    axisUnit: ref.priceAxisUnit.textContent,
    axisMax: ref.priceAxisTicks[0].textContent,
    axisMin: ref.priceAxisTicks[1].textContent,
    bars,
    gapBarDisplay: ref.priceBars[8].style.display === undefined ? '' : ref.priceBars[8].style.display,
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def _price_curve():
    """Quarters 0..47 priced (with a free and a negative one), 48..95 absent."""
    values = [0.245, 0.3, -0.05] + [0.2] * 45
    values[8] = None  # a quarter without any price at all
    return values + [None] * 48


def _run_panel(lang="en"):
    node = shutil.which("node")
    assert node, "Node is required to execute the shipped frontend"
    attrs = {
        "local_date": DAY,
        "current_index": 40,
        "current_progress": 0.5,
        "price_unit": "€/kWh",
        "series": {"price_kwh": _price_curve()},
    }
    result = subprocess.run(
        [node, "--experimental-vm-modules", "-e", PANEL_HARNESS, str(PANEL)],
        input=json.dumps({"attrs": attrs, "lang": lang}),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_panel_draws_the_price_in_its_own_strip_with_its_own_axis():
    result = _run_panel("en")

    assert result["hasPrice"] is True
    assert result["firstPrice"] == 0.245
    assert result["stageHasPrice"] is True
    assert result["legendHidden"] is False
    assert result["axisHidden"] is False
    # The unit and its own scale, never the kWh axis.
    assert result["axisUnit"] == "€/kWh"
    assert result["axisMax"] == "0.300"
    assert result["axisMin"] == "-0.050"
    # Bars exist for priced quarters, a negative quarter is marked, and a
    # quarter without a price draws nothing at all.
    assert result["bars"][0]["height"] > 0
    assert result["bars"][0]["display"] == ""
    assert result["bars"][2]["negative"] is True
    assert result["gapBarDisplay"] == "none"


def test_panel_tooltip_shows_the_price_of_that_quarter():
    result = _run_panel("en")

    assert "Price" in result["tooltip"]
    assert "0.245 €/kWh" in result["tooltip"]


def test_panel_price_labels_are_translated():
    assert _run_panel("nl")["priceLabel"] == "Prijs"
    assert "Prijs" in _run_panel("nl")["tooltip"]
    assert _run_panel("es")["priceLabel"] == "Precio"
