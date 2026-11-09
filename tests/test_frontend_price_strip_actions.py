"""The price strip must be readable *and* coloured like the day chart above it.

The user asked for three things at once:

* a visibly taller band (the 46 px strip showed almost no price difference);
* a y-axis that runs from the lowest to the highest price of the shown quarters
  instead of from 0, while a negative quarter still has to stay inside the axis
  (lower bound ``min(0, lowest)``);
* one bar colour per planned action, reusing the day chart's own variables:
  purple = grid charge, green = solar charge, blue = discharge, grey = no action,
  plus the action named in the tooltip.

These tests execute the shipped panel in Node with a DOM double, exactly like
``test_price_strip_placement.py``, and read the real bar geometry, axis labels,
fill classes and tooltip from that run. The strip keeping the chart's width,
x-scale and single scroll container stays pinned by that other file.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from custom_components import omnibattery

DAY = "2026-08-30"
PANEL = Path(omnibattery.__file__).parent / "frontend/marstek-panel.js"
# The day chart band above the strip; the strip starts right under it.
CHART_HEIGHT = 190
# Room the scroll container reserves for its one horizontal scrollbar.
SCROLLBAR_ROOM = 16

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
        list: () => [...set],
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
    locale: { language: scenario.lang || 'en', time_zone: 'server', time_format: '24' },
  };
  panel._card = () => {
    const card = new Node('div');
    card.appendChild(new Node('div'));
    return { card, head: new Node('div') };
  };
  panel._r = {};
  panel._buildDailyOperationTimelineCard();
  const ref = panel._r.dailyOperation;
  panel._patchDailyOperationTimeline();
  const snapshot = panel._dailyOperationSnapshot;

  const viewBox = (node) => String(node.attributes.viewBox || '').trim().split(/\s+/);
  const ACTION_CLASSES = [
    'daily-op-price-solar', 'daily-op-price-grid', 'daily-op-price-discharge', 'daily-op-price-neutral',
  ];
  const barAt = (index) => {
    const bar = ref.priceBars[index];
    return {
      y: Number(bar.attributes.y),
      height: Number(bar.attributes.height),
      display: bar.style.display === undefined ? '' : bar.style.display,
      negative: bar.classList.contains('daily-op-price-negative'),
      classes: bar.classList.list().filter((name) => ACTION_CLASSES.includes(name)),
    };
  };
  process.stdout.write(JSON.stringify({
    chartWidth: Number(viewBox(ref.svg)[2]),
    stripWidth: Number(viewBox(ref.priceSvg)[2]),
    stripHeight: Number(viewBox(ref.priceSvg)[3]),
    axisUnit: ref.priceAxisUnit.textContent,
    axisMax: ref.priceAxisTicks[0].textContent,
    axisMin: ref.priceAxisTicks[1].textContent,
    zeroY: Number(ref.priceZero.attributes.y1),
    bars: Object.fromEntries((scenario.probe || []).map((index) => [index, barAt(index)])),
    tooltips: Object.fromEntries((scenario.tooltip || []).map((index) => [
      index,
      panel._dailyOperationTooltipHTML(snapshot, panel._dailyOperationItem(snapshot, index)),
    ])),
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def _curve(**overrides) -> list:
    """96 quarters at 0.50, with the given index -> price overrides."""
    values = [0.5] * 96
    for index, price in overrides.items():
        values[int(index)] = price
    return values


def _masks(**overrides) -> list:
    """96 planned action masks; the panel's bits are 1=solar, 2=grid, 4=discharge."""
    values = [0] * 96
    for index, mask in overrides.items():
        values[int(index)] = mask
    return values


def _attrs(prices, masks, actual=None) -> dict:
    operations = {"planned_action_mask": masks}
    if actual is not None:
        # An elapsed quarter renders the action that was actually observed.
        operations["actual_action_mask"] = actual
    return {
        "local_date": DAY,
        "current_index": 40,
        "current_progress": 0.5,
        "price_unit": "\u20ac/kWh",
        "series": {"price_per_kwh": prices},
        "operations": operations,
    }


def _run(attrs, probe=(), tooltip=(), lang="en") -> dict:
    node = shutil.which("node")
    assert node, "Node is required to execute the shipped frontend"
    result = subprocess.run(
        [node, "--experimental-vm-modules", "-e", PANEL_HARNESS, str(PANEL)],
        input=json.dumps({"attrs": attrs, "lang": lang, "probe": list(probe), "tooltip": list(tooltip)}),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _rule(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{(?P<body>[^}]*)\}", css)
    assert match is not None, selector
    return match.group("body")


def _px(rule: str, name: str = "height") -> int:
    match = re.search(rf"{name}:\s*(\d+)px", rule)
    assert match is not None, (name, rule)
    return int(match.group(1))


def test_the_strip_is_much_taller_and_the_scrollbar_stays_below_it():
    """The old 46 px band hid the price differences the user wanted to see."""
    css = PANEL.read_text(encoding="utf-8")
    strip = _rule(css, ".daily-op-price-strip")
    strip_height = _px(strip)
    result = _run(_attrs(_curve(), _masks()), probe=[10])

    # One source of truth: the drawn SVG band and the CSS box agree.
    assert result["stripHeight"] == strip_height
    assert result["stripHeight"] > 70
    # Directly under the chart band, and the price axis is exactly as tall.
    assert f"top: {CHART_HEIGHT}px" in strip
    assert _px(_rule(css, ".daily-op-price-axis")) == strip_height
    # The stage reserves chart + strip and the viewport adds the scrollbar room,
    # so the bar keeps sitting *below* the strip instead of clipping it.
    assert _px(_rule(css, ".daily-op-stage")) == CHART_HEIGHT + strip_height
    assert _px(_rule(css, ".daily-op-viewport")) == CHART_HEIGHT + strip_height + SCROLLBAR_ROOM


def test_the_axis_runs_from_the_lowest_to_the_highest_price():
    """A day between 0.44 and 0.66 must use the whole band, not a third of it."""
    prices = _curve(**{"10": 0.66, "20": 0.44})
    result = _run(_attrs(prices, _masks()), probe=[10, 20])

    assert result["axisMax"] == "0.660"
    assert result["axisMin"] == "0.440"
    # Never a zero-based axis: the bottom label is the lowest price, not 0.000.
    assert result["axisMin"] != "0.000"
    # Geometry proves it: the dearest quarter touches the top of the band and
    # the cheapest one sits on the bottom edge.
    assert result["bars"]["10"]["y"] == 8
    assert result["bars"]["20"]["y"] == 92
    assert result["bars"]["20"]["height"] < 1
    # 0 is outside the range, so the bottom edge is the baseline and is labelled
    # with the lowest price - no zero line is drawn inside the band.
    assert result["zeroY"] == 92


def test_a_negative_price_stays_inside_the_axis_under_its_zero_line():
    prices = _curve(**{"5": -0.05})
    result = _run(_attrs(prices, _masks()), probe=[5, 6])

    # min(0, lowest) == lowest here: the axis starts at the negative price.
    assert result["axisMin"] == "-0.050"
    assert result["axisMax"] == "0.500"
    # 0 is inside the range, so the zero line is drawn and the negative bar
    # hangs from it down to the bottom edge, without leaving the band.
    assert 8 < result["zeroY"] < 92
    negative = result["bars"]["5"]
    assert negative["negative"] is True
    assert abs(negative["y"] - result["zeroY"]) <= 0.01
    assert abs(negative["y"] + negative["height"] - 92) <= 0.01


def test_each_bar_reuses_the_day_charts_action_colour():
    """Bit 1 = solar, 2 = grid, 4 = discharge; no action stays neutral."""
    masks = _masks(**{"20": 4, "30": 2, "40": 0, "50": 1})
    result = _run(_attrs(_curve(), masks), probe=[20, 30, 40, 50])

    assert result["bars"]["20"]["classes"] == ["daily-op-price-discharge"]
    assert result["bars"]["30"]["classes"] == ["daily-op-price-grid"]
    assert result["bars"]["40"]["classes"] == ["daily-op-price-neutral"]
    assert result["bars"]["50"]["classes"] == ["daily-op-price-solar"]


def test_the_action_colours_are_the_charts_own_variables():
    """No new palette: the fills point at the day chart's existing tokens."""
    css = PANEL.read_text(encoding="utf-8")
    for selector, variable in (
        (".daily-op-price-bar.daily-op-price-solar", "--daily-op-solar-charge"),
        (".daily-op-price-bar.daily-op-price-grid", "--daily-op-grid"),
        (".daily-op-price-bar.daily-op-price-discharge", "--daily-op-discharge"),
        (".daily-op-price-bar.daily-op-price-neutral", "--daily-op-not-needed"),
    ):
        assert variable in _rule(css, selector), selector


def test_a_multi_action_quarter_follows_the_documented_priority():
    """mask 7 = solar | grid | discharge: one bar, so one fixed priority."""
    result = _run(_attrs(_curve(), _masks(**{"60": 7})), probe=[60])

    # Same order as the day chart's own action list (bit 1 -> 2 -> 4): solar
    # charging wins over grid charging, which wins over discharge.
    assert result["bars"]["60"]["classes"] == ["daily-op-price-solar"]


def test_a_bar_without_a_price_stays_empty():
    prices = _curve()
    prices[8] = None
    result = _run(_attrs(prices, _masks()), probe=[8])

    assert result["bars"]["8"]["display"] == "none"


def test_the_price_legend_explains_that_the_colour_is_the_action():
    css = PANEL.read_text(encoding="utf-8")
    result = _run(_attrs(_curve(), _masks()))

    # The swatch shows the four action colours instead of one flat strip colour.
    swatch = _rule(css, ".daily-op-swatch-price")
    for variable in ("--daily-op-solar-charge", "--daily-op-grid", "--daily-op-discharge", "--daily-op-not-needed"):
        assert variable in swatch
    # ... and the label says so.
    assert 'this._t("dailyOperationPriceAction")' in PANEL.read_text(encoding="utf-8")
    dictionaries = PANEL.read_text(encoding="utf-8").split("const I18N = {", 1)[1]
    for language in ("en", "es", "ca", "de", "fr", "nl"):
        block = dictionaries.split(f"  {language}: {{", 1)[1].split("\n  },", 1)[0]
        assert "dailyOperationPriceAction:" in block, language
