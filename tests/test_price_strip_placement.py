"""End-to-end wiring of the daily-chart price strip.

The strip was invisible in the field while the predictive-charging status
sensor reported ``price_data_status: ok (91 slots)``. The cause was not the
prices: the timeline adapter read ``controller.pricing``, a name the controller
never sets (it keeps the manager as ``_pricing_mgr``), so every price read
silently degraded to "no slots": 96 ``None`` values, a ``None`` unit, and a
panel that hid the whole band.

These tests pin the whole chain (pricing manager -> sensor attributes -> panel
DOM) and the two things the strip must keep:

* it lives inside the day chart's single horizontal scroll container, directly
  under the chart and above that container's scrollbar, sharing one x-scale;
* its container is always rendered (axis and unit included) even when not a
  single quarter carries a price, because an invisible band cannot be told
  apart from a bug.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components import omnibattery
from custom_components.omnibattery.const import PRICE_INTEGRATION_EPEX
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.sensor import _daily_operation_timeline_attributes

DAY = "2026-08-30"
COMPONENT = Path(omnibattery.__file__).parent
PANEL = COMPONENT / "frontend/marstek-panel.js"
MANIFEST = COMPONENT / "manifest.json"
EXPECTED_VERSION = "1.6.4b1"


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 30, hour, minute)


# ----------------------------------------------------------------------
# The whole chain: real PricingManager -> published attributes
# ----------------------------------------------------------------------

def _fixed_clock(monkeypatch, now: datetime) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(pricing_engine, "datetime", FixedDateTime)


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


def _real_controller(monkeypatch, entries) -> tuple[SimpleNamespace, PricingManager]:
    """Build the controller exactly as the integration stores it.

    The pricing manager is reachable only as ``_pricing_mgr``; there is no
    ``pricing`` attribute on the live controller.
    """
    monkeypatch.setattr(
        pricing_engine, "resolve_official_nordpool_source", lambda *_args: None
    )
    _fixed_clock(monkeypatch, _at(12))
    state = SimpleNamespace(state="0.10", attributes={"data": entries})
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: state))
    ctrl = SimpleNamespace(
        hass=hass,
        price_sensor="sensor.dynamic_price",
        price_integration_type=PRICE_INTEGRATION_EPEX,
        _price_data_status="not_evaluated",
        daily_operation_timeline=SimpleNamespace(snapshot=lambda: {"local_date": DAY}),
    )
    ctrl._pricing_mgr = PricingManager(hass, ctrl)
    return ctrl, ctrl._pricing_mgr


def test_prices_reach_the_published_96_quarter_series(monkeypatch):
    """The live controller must publish filled quarters and a unit.

    Before the fix this failed with ``price_kwh`` = 96 x ``None`` and
    ``price_unit`` = ``None`` while the price manager held a full day of slots.
    """
    controller, manager = _real_controller(monkeypatch, _epex_entries(0, 24))

    attributes = _daily_operation_timeline_attributes(controller)
    series = attributes["series"]["price_kwh"]

    assert len(series) == 96
    filled = [value for value in series if value is not None]
    assert len(filled) == 96, (
        "the timeline adapter did not reach the controller's pricing manager: "
        f"{96 - len(filled)} of 96 quarters stayed null"
    )
    assert filled[:4] == [0.21, 0.21, 0.21, 0.21]
    assert attributes["price_unit"] == "\u20ac/kWh"


def test_the_timeline_reads_the_same_window_as_the_status_sensor(monkeypatch):
    """One manager, one horizon: the status sensor's "ok (N slots)" comes from it."""
    controller, manager = _real_controller(monkeypatch, _epex_entries(0, 24))

    _daily_operation_timeline_attributes(controller)

    # _parse_price_data writes this on the controller, exactly what
    # predictive_charging_status publishes. 12 future slots at noon; the
    # elapsed half is returned to the chart but must not mask the health status.
    assert controller._price_data_status == "ok (12 slots)"
    assert len(manager.get_future_price_slots(include_past=True)) == 24
    assert len(manager.get_future_price_slots()) == 12


def test_include_past_returns_priced_slots_for_a_realistic_feed(monkeypatch):
    """include_past=True works; it was never the reason the strip stayed empty."""
    _controller, manager = _real_controller(monkeypatch, _epex_entries(0, 24))

    slots = manager.get_future_price_slots(include_past=True)

    assert [slot.start.hour for slot in slots] == list(range(24))
    assert all(slot.price == 0.21 for slot in slots)


# ----------------------------------------------------------------------
# The panel: one scroll container, one x-scale
# ----------------------------------------------------------------------

PANEL_HARNESS = r"""
(async () => {
  const fs = require('node:fs');
  const vm = require('node:vm');

  class Node {
    constructor(tag, ns) {
      this.tagName = tag;
      this.namespaceURI = ns || null;
      this.children = [];
      this.parentNode = null;
      this.attributes = {};
      this.style = {};
      this.dataset = {};
      this.hidden = false;
      this.textContent = '';
      this._className = '';
      this._html = '';
      const set = new Set();
      this.classList = {
        add: (...names) => names.forEach((name) => set.add(name)),
        remove: (...names) => names.forEach((name) => set.delete(name)),
        contains: (name) => set.has(name),
        toggle: (name, force) => {
          const on = force === undefined ? !set.has(name) : !!force;
          if (on) set.add(name); else set.delete(name);
          return on;
        },
      };
    }
    set className(value) { this._className = String(value); }
    get className() { return this._className; }
    set innerHTML(value) { this._html = String(value); this.children = []; }
    get innerHTML() { return this._html; }
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
    append(...nodes) { nodes.forEach((node) => this.appendChild(node)); }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    getAttribute(key) { return this.attributes[key]; }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener() {}
    removeEventListener() {}
    focus() {}
    blur() {}
    querySelector(selector) {
      if (!this._qs) this._qs = new Map();
      if (!this._qs.has(selector)) this._qs.set(selector, new Node('div'));
      return this._qs.get(selector);
    }
    querySelectorAll(selector) {
      if (!this._qsa) this._qsa = new Map();
      if (!this._qsa.has(selector)) this._qsa.set(selector, [new Node('span'), new Node('span')]);
      return this._qsa.get(selector);
    }
    contains(other) {
      for (let node = other; node; node = node.parentNode) if (node === this) return true;
      return false;
    }
  }

  let Panel;
  const context = vm.createContext({
    HTMLElement: class {},
    customElements: { get() {}, define(_name, cls) { Panel = cls; } },
    document: {
      createElement: (tag) => new Node(tag),
      createElementNS: (ns, tag) => new Node(tag, ns),
    },
    window: { addEventListener() {}, removeEventListener() {}, visualViewport: null },
    requestAnimationFrame: (callback) => { callback(); return 1; },
  });
  const runtime = new vm.SourceTextModule(fs.readFileSync(process.argv[1], 'utf8'), { context });
  await runtime.link(() => { throw new Error('Unexpected external import'); });
  await runtime.evaluate();

  const scenario = JSON.parse(fs.readFileSync(0, 'utf8'));
  const entityId = 'sensor.test_daily_operation_timeline';
  const panel = Object.create(Panel.prototype);
  panel._panelConfig = {};
  panel._hass = {
    states: { [entityId]: { entity_id: entityId, state: 'ready', attributes: scenario.attrs } },
    entities: { [entityId]: { entity_id: entityId, platform: 'omnibattery', translation_key: 'daily_operation_timeline' } },
    devices: {}, config: { time_zone: 'UTC' },
    locale: { language: scenario.lang || 'nl', time_zone: 'server', time_format: '24' },
  };
  panel._card = () => {
    const card = new Node('div');
    const head = new Node('div');
    card.appendChild(head);
    return { card, head };
  };
  panel._r = {};
  const card = panel._buildDailyOperationTimelineCard();
  const ref = panel._r.dailyOperation;
  panel._patchDailyOperationTimeline();

  const viewBoxWidth = (node) => Number(String(node.attributes.viewBox || '').trim().split(/\s+/)[2]);
  const byClass = (className) => {
    const found = [];
    const walk = (node) => {
      if (node.className && String(node.className).split(/\s+/).includes(className)) found.push(node);
      for (const child of node.children) walk(child);
    };
    walk(card);
    return found;
  };
  const stageIndex = (node) => ref.stage.children.indexOf(node);

  process.stdout.write(JSON.stringify({
    viewportCount: byClass('daily-op-viewport').length,
    stageCount: byClass('daily-op-stage').length,
    stripCount: byClass('daily-op-price-strip').length,
    viewportHasStrip: ref.viewport.contains(ref.priceStrip),
    viewportHasChart: ref.viewport.contains(ref.svg),
    stripParentIsChartParent: ref.priceStrip.parentNode === ref.svg.parentNode,
    stripParentIsStage: ref.priceStrip.parentNode === ref.stage,
    stageParentIsViewport: ref.stage.parentNode === ref.viewport,
    chartWidth: viewBoxWidth(ref.svg),
    stripWidth: viewBoxWidth(ref.priceSvg),
    stripBars: ref.priceBars.length,
    stripZeroX2: Number(ref.priceZero.attributes.x2),
    stripIndexAfterChart: stageIndex(ref.priceStrip) === stageIndex(ref.svg) + 1,
    stripAriaHidden: ref.priceStrip.attributes['aria-hidden'] || null,
    stripTabIndex: ref.priceStrip.tabIndex === undefined ? null : ref.priceStrip.tabIndex,
    stripTabindexAttribute: ref.priceStrip.attributes.tabindex || null,
    stripInlineOverflowX: ref.priceStrip.style.overflowX || null,
    stripHidden: ref.priceStrip.hidden === true,
    axisHidden: ref.priceAxis.hidden === true,
    axisUnit: ref.priceAxisUnit.textContent,
    hasPrice: panel._dailyOperationSnapshot.hasPrice,
    barDisplays: ref.priceBars.slice(0, 4).map((bar) => bar.style.display === undefined ? '' : bar.style.display),
    legendHidden: ref.priceLegend.hidden === true,
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def _price_curve():
    return [round(0.2 + (index % 7) * 0.01, 6) for index in range(96)]


def _run_panel(attrs):
    node = shutil.which("node")
    assert node, "Node is required to execute the shipped frontend"
    result = subprocess.run(
        [node, "--experimental-vm-modules", "-e", PANEL_HARNESS, str(PANEL)],
        input=json.dumps({"attrs": attrs, "lang": "nl"}),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _priced_attrs():
    return {
        "local_date": DAY,
        "current_index": 40,
        "current_progress": 0.5,
        "price_unit": "\u20ac/kWh",
        "series": {"price_kwh": _price_curve()},
    }


def _unpriced_attrs():
    return {
        "local_date": DAY,
        "current_index": 40,
        "current_progress": 0.5,
        "series": {},
    }


def test_price_strip_is_a_child_of_the_charts_scroll_container():
    """The core wish: one horizontally scrolled timeline, strip included."""
    result = _run_panel(_priced_attrs())

    # A single scroll container owns both the chart and the strip.
    assert result["viewportCount"] == 1
    assert result["stageCount"] == 1
    assert result["viewportHasChart"] is True
    assert result["viewportHasStrip"] is True
    # The strip is a sibling of the chart SVG, inside the scrolled stage, and
    # comes directly after the chart.
    assert result["stripParentIsChartParent"] is True
    assert result["stripParentIsStage"] is True
    assert result["stageParentIsViewport"] is True
    assert result["stripIndexAfterChart"] is True


def test_price_strip_uses_the_same_width_and_x_scale_as_the_chart():
    result = _run_panel(_priced_attrs())

    assert result["chartWidth"] == 1440
    assert result["stripWidth"] == result["chartWidth"]
    assert result["stripZeroX2"] == result["chartWidth"]
    # 96 quarter bars, one per cell of the chart above.
    assert result["stripBars"] == 96


def test_price_strip_does_not_add_its_own_scrollbar_or_tabstop():
    result = _run_panel(_priced_attrs())

    assert result["stripInlineOverflowX"] is None
    assert result["stripAriaHidden"] == "true"
    assert result["stripTabIndex"] is None
    assert result["stripTabindexAttribute"] is None


def test_price_strip_container_is_rendered_without_any_price_data():
    """An empty band with its axis and unit beats an invisible band."""
    result = _run_panel(_unpriced_attrs())

    assert result["hasPrice"] is False
    assert result["stripCount"] == 1
    assert result["stripHidden"] is False
    assert result["axisHidden"] is False
    assert result["axisUnit"] == "\u20ac/kWh"
    # Only the bars are empty.
    assert result["barDisplays"] == ["none", "none", "none", "none"]
    # Still the chart's sibling inside the one scroll container.
    assert result["stripParentIsStage"] is True
    assert result["viewportCount"] == 1


def test_price_strip_css_is_never_display_none():
    css = PANEL.read_text(encoding="utf-8")
    rule = re.search(r"\.daily-op-price-strip\s*\{(?P<body>[^}]*)\}", css)
    assert rule is not None
    assert "display: none" not in rule.group("body")
    # The has-price flag is a state marker in JS only; no stylesheet may use it
    # to hide the band.
    assert re.search(r"\.daily-op-has-price", css) is None
    stage = re.search(r"\.daily-op-stage\s*\{(?P<body>[^}]*)\}", css)
    assert stage is not None
    # The strip band is always reserved, so the strip can never fall outside
    # the vertically clipped viewport.
    assert "height: 236px" in stage.group("body")
    # The viewport must reserve room for the horizontal scrollbar, otherwise
    # the bar eats the bottom of the strip instead of sitting below it.
    viewport = re.search(r"\.daily-op-viewport\s*\{(?P<body>[^}]*)\}", css)
    assert viewport is not None
    assert "height: 252px" in viewport.group("body")


# ----------------------------------------------------------------------
# Cache bust: the manifest version, not mtime, must load the new JS
# ----------------------------------------------------------------------

async def test_panel_cache_bust_uses_the_manifest_version(monkeypatch):
    from homeassistant import loader

    from custom_components.omnibattery import _panel_cache_bust

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    seen = {}

    async def fake_integration(hass, domain):
        seen["domain"] = domain
        return SimpleNamespace(version=manifest["version"])

    monkeypatch.setattr(loader, "async_get_integration", fake_integration)
    js_file = SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=1234567890))

    token = await _panel_cache_bust(None, js_file)

    assert seen["domain"] == "omnibattery"
    assert token == manifest["version"]
    assert token == EXPECTED_VERSION


async def test_panel_cache_bust_falls_back_to_mtime_without_a_manifest(monkeypatch):
    from homeassistant import loader

    from custom_components.omnibattery import _panel_cache_bust

    async def broken(_hass, _domain):
        raise RuntimeError("no integration loader")

    monkeypatch.setattr(loader, "async_get_integration", broken)
    js_file = SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=1700000000))

    assert await _panel_cache_bust(None, js_file) == "1700000000"


def test_manifest_version_is_bumped_and_has_no_bom():
    raw = MANIFEST.read_bytes()
    assert raw[:3] == b"{\n "
    assert raw[:3] != b"\xef\xbb\xbf"
    assert json.loads(raw.decode("utf-8"))["version"] == EXPECTED_VERSION
