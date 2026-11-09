"""Placement of the daily-chart price strip.

The strip is added on top of FFunes' ``release/v1.6.0``, which already publishes
the quarter price of the whole day (``series.price_per_kwh`` plus
``extended_horizon.price_per_kwh``) and draws it as a stepped line with
high/low labels. Our addition is the band *under* the chart: one bar per
quarter, coloured by the action the planner gave that quarter.

These tests pin the two things the strip must keep:

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
from pathlib import Path

import pytest

from custom_components import omnibattery

DAY = "2026-08-30"
COMPONENT = Path(omnibattery.__file__).parent
PANEL = COMPONENT / "frontend/marstek-panel.js"
MANIFEST = COMPONENT / "manifest.json"
EXPECTED_VERSION = "1.6.0b9"

# Geometry of the day chart and the price strip under it. PRICE_STRIP_HEIGHT_PX is
# the user-visible knob (the strip was 46 px, too flat to show any price
# difference); the stage and the scroll container follow from it, so a future
# height change only touches this one number.
CHART_HEIGHT_PX = 190
PRICE_STRIP_HEIGHT_PX = 100
SCROLLBAR_ALLOWANCE_PX = 16
STAGE_HEIGHT_PX = CHART_HEIGHT_PX + PRICE_STRIP_HEIGHT_PX
VIEWPORT_HEIGHT_PX = STAGE_HEIGHT_PX + SCROLLBAR_ALLOWANCE_PX

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
    devices: {}, config: { time_zone: 'UTC', currency: 'EUR' },
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
    totalIntervals: ref.totalIntervals,
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
    hasPrice: !!(ref.stage && ref.stage.classList
      && ref.stage.classList.contains('daily-op-has-price')),
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
        "series": {"price_per_kwh": _price_curve()},
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


def _panel_const(name: str) -> int:
    """Read one integer constant out of the shipped panel module."""
    match = re.search(
        rf"const {name} = (\d+);", PANEL.read_text(encoding="utf-8")
    )
    assert match is not None, f"{name} is no longer a plain integer constant"
    return int(match.group(1))


def test_price_strip_uses_the_same_width_and_x_scale_as_the_chart():
    result = _run_panel(_priced_attrs())

    base = _panel_const("DAILY_OPERATION_BASE_INTERVALS")
    # One x-scale for the whole stage: the strip's viewBox is the chart's, and
    # both follow the span the snapshot actually reaches (the panel resizes the
    # chart when the plan runs past its default extension, and the strip has to
    # move with it or quarter N of the strip no longer sits under quarter N of
    # the cells above). Deriving the width from the probed span keeps this test
    # honest when that span changes.
    assert result["totalIntervals"] >= base
    assert result["chartWidth"] == result["totalIntervals"] * 10
    assert result["stripWidth"] == result["chartWidth"]
    assert result["stripZeroX2"] == result["chartWidth"]
    # One bar per quarter cell of the day above.
    assert result["stripBars"] == base


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
    # The band is deliberately tall and its own height is the source of truth:
    # a strip that silently drops back to the unreadable 46 px sliver must fail.
    assert PRICE_STRIP_HEIGHT_PX > 70
    assert f"height: {PRICE_STRIP_HEIGHT_PX}px" in rule.group("body")
    stage = re.search(r"\.daily-op-stage\s*\{(?P<body>[^}]*)\}", css)
    assert stage is not None
    # The strip band is always reserved, so the strip can never fall outside
    # the vertically clipped viewport.
    assert f"height: {STAGE_HEIGHT_PX}px" in stage.group("body")
    # The viewport must reserve room for the horizontal scrollbar, otherwise
    # the bar eats the bottom of the strip instead of sitting below it.
    viewport = re.search(r"\.daily-op-viewport\s*\{(?P<body>[^}]*)\}", css)
    assert viewport is not None
    assert f"height: {VIEWPORT_HEIGHT_PX}px" in viewport.group("body")


def test_manifest_version_is_bumped_and_has_no_bom():
    raw = MANIFEST.read_bytes()
    assert raw[:3] == b"{\n "
    assert raw[:3] != b"\xef\xbb\xbf"
    assert json.loads(raw.decode("utf-8"))["version"] == EXPECTED_VERSION
