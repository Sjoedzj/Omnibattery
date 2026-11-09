"""Per quarter-hour trace of the *real* discharge control path.

The ``charge_candidates`` microscope next door answers "why was this quarter not
a grid-charge candidate". It says nothing about the other direction, and that is
where the surprising decisions live: a battery that discharges at 15:30 for
44 ct while a dearer hour was still ahead, with the discharge reserve switched
on. Nothing in the integration recorded *why* the control path allowed the
quarter it discharged in, so the only way to look at that was to re-derive the
rule - which is exactly how a projection ends up disagreeing with the battery.

This module records the answer while it happens. :meth:`DischargeDecisionTrace.observe`
runs at the end of ``ChargeDischargeController._refresh_operation_blockers`` -
the same pass that rebuilds the blocker registry the dispatch reads - so every
value is the live state of the real path rather than a second implementation of
it:

* ``price`` and ``threshold`` are the two numbers ``apply_price_discharge_block``
  itself compared, read through that method's own accessors
  (``PricingManager._get_current_price`` and
  ``PricingManager._dp_price_discharge_threshold``);
* ``blockers`` is the union of the real registry (``get_discharge_blockers`` for
  the global scope plus ``get_battery_discharge_blockers``), so the names are the
  very sources the dispatch and the "stop blocked batteries" pass look at;
* ``reserved_floor`` and ``reserve_kwh`` come from the ``price_reserve`` blocker
  details and the reserve manager's own published status;
* ``discharge_kwh`` is integrated from the measured delivered power per battery,
  not from the plan.

What it cannot do, and does not pretend to: reconstruct a quarter nobody
observed. A restart, a reload or a sleeping host leaves a gap, and a gap is
reported as ``past`` with ``null`` fields - never as a fabricated ``0``. Energy
is only integrated over gaps up to :data:`MAX_INTEGRATION_GAP_S`; a longer gap is
left out rather than smeared over a quarter it may not belong to.

This is measurement only. Nothing here writes to the blocker registry, the
plan or the power dispatch, so a controller behaves identically with and without
the trace attached.
"""
from __future__ import annotations

import math
from datetime import datetime
from time import monotonic
from typing import Any, Callable

# One record per quarter of the day, and never a full series array.
MAX_RECORDS = 96
INTERVAL_MINUTES = 15
# A control cycle is ~2.5 s. A longer gap means nobody was observing (restart,
# sleep, reload); the energy of that gap is unknown and is dropped.
MAX_INTEGRATION_GAP_S = 300.0
# Below this a measured discharge is rounding noise, not a discharge.
DISCHARGE_EPSILON_KWH = 1e-3
# Why a quarter ended without discharging, in the order the real path decides:
# the price floor is economic, the reserve holds the dearer tail back, min_soc
# is the safety floor. Any other source is reported by name.
VERDICT_PRIORITY = (
    ("price_discharge", "price_blocked"),
    ("price_reserve", "reserve_blocked"),
    ("min_soc", "floor_blocked"),
)


def _system_now() -> datetime:
    """Return local 'now', preferring Home Assistant's clock when available."""
    try:
        from homeassistant.util import dt as dt_util

        return dt_util.now()
    except Exception:  # noqa: BLE001 - unit tests import this module without HA
        return datetime.now().astimezone()


def quarter_index(moment: datetime) -> int | None:
    """Return the quarter-hour index (0-95) of a local wall-clock time."""
    if not isinstance(moment, datetime):
        return None
    index = (moment.hour * 60 + moment.minute) // INTERVAL_MINUTES
    return index if 0 <= index < MAX_RECORDS else None


def quarter_start(moment: datetime, index: int) -> datetime:
    """Return the wall-clock start of a quarter on ``moment``'s local day."""
    return moment.replace(
        hour=index // 4, minute=(index % 4) * INTERVAL_MINUTES, second=0, microsecond=0
    )


def _number(value: object) -> float | None:
    """Return a finite float, else ``None`` - never a fabricated zero."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _blocker_state(controller: object) -> tuple[set[str], list[dict[str, Any]]]:
    """Return the active discharge sources and every registry's details.

    Uses the controller's own accessors, so the trace sees exactly the registry
    the dispatch reads. The global scope comes first, then one entry per battery,
    so a detail is found wherever the path recorded it. A duck-typed or
    half-built controller simply yields nothing instead of raising.
    """
    sources: set[str] = set()
    registries: list[dict[str, Any]] = []

    global_getter = getattr(controller, "get_discharge_blockers", None)
    if callable(global_getter):
        try:
            global_blockers = global_getter()
        except Exception:  # noqa: BLE001 - diagnostics must stay best-effort
            global_blockers = None
        if isinstance(global_blockers, dict):
            sources.update(str(name) for name in global_blockers)
            registries.append(global_blockers)

    battery_getter = getattr(controller, "get_battery_discharge_blockers", None)
    if callable(battery_getter):
        try:
            battery_blockers = battery_getter()
        except Exception:  # noqa: BLE001
            battery_blockers = None
        if isinstance(battery_blockers, dict):
            for blockers in battery_blockers.values():
                if not isinstance(blockers, dict):
                    continue
                sources.update(str(name) for name in blockers)
                registries.append(blockers)

    return sources, registries


def _detail(blockers: list[dict[str, Any]], source: str) -> dict[str, Any]:
    """Return the details of ``source`` from any registry that carries it."""
    for entry in blockers:
        record = entry.get(source)
        if isinstance(record, dict):
            details = record.get("details")
            if isinstance(details, dict):
                return details
    return {}


def _current_price(controller: object) -> float | None:
    """The price the real path compared against its threshold this cycle."""
    manager = getattr(controller, "_pricing_mgr", None)
    getter = getattr(manager, "_get_current_price", None)
    if not callable(getter):
        return None
    try:
        return _number(getter())
    except Exception:  # noqa: BLE001
        return None


def _threshold(
    controller: object, blockers: list[dict[str, Any]]
) -> float | None:
    """The discharge price floor the real path applied this cycle.

    ``_dp_price_discharge_threshold`` is the path's own accessor, so it answers
    even when the price was above the floor and no blocker was set - which is
    precisely the case a user asks about. When it declines (another pricing
    mode), the value the blocking cycle itself recorded is used.
    """
    manager = getattr(controller, "_pricing_mgr", None)
    getter = getattr(manager, "_dp_price_discharge_threshold", None)
    if callable(getter):
        try:
            value = _number(getter())
        except Exception:  # noqa: BLE001
            value = None
        if value is not None:
            return value
    recorded = _number(_detail(blockers, "price_discharge").get("threshold"))
    if recorded is not None:
        return recorded
    return _number(getattr(controller, "discharge_price_threshold", None))


def _reserve_kwh(controller: object) -> float | None:
    """The reserve energy the reserve manager published this cycle."""
    manager = getattr(controller, "_discharge_reserve_mgr", None)
    get_status = getattr(manager, "get_status", None)
    if not callable(get_status):
        return None
    try:
        status = get_status()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(status, dict):
        return None
    return _number(status.get("reserve_kwh"))


def _eligible_batteries(controller: object) -> list[Any]:
    """Batteries the blocker pass itself considers, else every coordinator."""
    helper = getattr(controller, "_known_batteries_for_block_summary", None)
    if callable(helper):
        try:
            batteries = helper()
        except Exception:  # noqa: BLE001
            batteries = None
        if isinstance(batteries, (list, tuple)):
            return list(batteries)
    coordinators = getattr(controller, "coordinators", None)
    return list(coordinators) if isinstance(coordinators, (list, tuple)) else []


def _soc_pct(controller: object) -> float | None:
    """The lowest battery SOC this cycle: the one that binds first."""
    readings: list[float] = []
    for coordinator in _eligible_batteries(controller):
        data = getattr(coordinator, "data", None)
        if not isinstance(data, dict):
            continue
        value = _number(data.get("battery_soc"))
        if value is not None:
            readings.append(value)
    return min(readings) if readings else None


def _reserved_floor(blockers: list[dict[str, Any]]) -> float | None:
    """The highest SOC floor a ``price_reserve`` blocker held this cycle.

    The strictest hold of the fleet, so one record still names the floor that
    stopped the batteries rather than an average of them. ``None`` whenever the
    reserve held nothing back.
    """
    floors = [
        _number(entry.get("price_reserve", {}).get("details", {}).get("reserved_floor"))
        for entry in blockers
        if isinstance(entry.get("price_reserve"), dict)
    ]
    known = [floor for floor in floors if floor is not None]
    return max(known) if known else None


def _delivered_watts(controller: object, coordinator: object) -> float | None:
    """Measured delivered power for one battery (controller sign convention)."""
    helper = getattr(controller, "_coordinator_delivered_power", None)
    if not callable(helper):
        return None
    try:
        return _number(helper(coordinator))
    except Exception:  # noqa: BLE001
        return None


def _verdict(record: dict[str, Any], sources: set[str]) -> str:
    """Name why the quarter ended the way it did, from the real blockers."""
    for source, verdict in VERDICT_PRIORITY:
        if source in sources:
            return verdict
    energy = record.get("_discharge_kwh") or 0.0
    if record.get("_energy_known") and energy > DISCHARGE_EPSILON_KWH:
        return "discharging"
    others = sorted(source for source in sources if source)
    if others:
        return f"other:{others[0]}"[:48]
    if not record.get("_energy_known"):
        # No blocker and no telemetry: claiming "no demand" would be a guess.
        return "other:no_telemetry"
    return "no_demand"


class DischargeDecisionTrace:
    """Bounded per-quarter record of the real discharge control path."""

    def __init__(
        self,
        controller: object,
        *,
        now: Callable[[], datetime] | None = None,
        monotonic_fn: Callable[[], float] = monotonic,
    ) -> None:
        self._controller = controller
        self._now = now or _system_now
        self._monotonic = monotonic_fn
        self._date: str | None = None
        self._first_index: int | None = None
        self._records: dict[int, dict[str, Any]] = {}
        self._last_monotonic: float | None = None

    # -- recording ---------------------------------------------------------
    def observe(self) -> None:
        """Record the current quarter from the live state of the real path.

        Called once per control cycle, so the quarter keeps the last state it
        had while it was current. Does nothing without a usable clock.
        """
        now = self._now()
        index = quarter_index(now)
        if index is None:
            return
        date = now.date().isoformat()
        if date != self._date:
            self._start_day(date)
        if self._first_index is None or index < self._first_index:
            self._first_index = index
        record = self._records.get(index)
        if record is None:
            record = self._blank(index, now)
            self._records[index] = record
        self._fill(record)

    def _start_day(self, date: str) -> None:
        """Drop yesterday: the trace answers for today only."""
        self._date = date
        self._first_index = None
        self._records = {}
        self._last_monotonic = None

    def _blank(self, index: int, now: datetime) -> dict[str, Any]:
        return {
            "index": index,
            "slot_start": quarter_start(now, index).isoformat(),
            "price": None,
            "threshold": None,
            "reserve_kwh": None,
            "reserved_floor": None,
            "soc_pct": None,
            "discharge_kwh": None,
            "verdict": None,
            "blockers": [],
            "_discharge_kwh": 0.0,
            "_energy_known": False,
        }

    def _fill(self, record: dict[str, Any]) -> None:
        sources, registries = _blocker_state(self._controller)
        record["price"] = _current_price(self._controller)
        record["threshold"] = _threshold(self._controller, registries)
        record["reserve_kwh"] = _reserve_kwh(self._controller)
        record["reserved_floor"] = _reserved_floor(registries)
        record["soc_pct"] = _soc_pct(self._controller)
        measured, known = self._measure_discharge()
        record["_discharge_kwh"] = record["_discharge_kwh"] + measured
        record["_energy_known"] = record["_energy_known"] or known
        record["discharge_kwh"] = (
            round(record["_discharge_kwh"], 3) if record["_energy_known"] else None
        )
        record["blockers"] = sorted(sources)
        record["verdict"] = _verdict(record, sources)

    def _measure_discharge(self) -> tuple[float, bool]:
        """Return (kWh discharged since the previous cycle, telemetry seen)."""
        now_monotonic = self._monotonic()
        previous = self._last_monotonic
        self._last_monotonic = now_monotonic
        gap = None if previous is None else now_monotonic - previous
        total = 0.0
        known = False
        for coordinator in getattr(self._controller, "coordinators", None) or ():
            delivered = _delivered_watts(self._controller, coordinator)
            if delivered is None:
                continue
            known = True
            if gap is None or gap <= 0 or gap > MAX_INTEGRATION_GAP_S:
                continue
            # Controller convention is +charge/-discharge, so a negative value is
            # the discharge. W * s / 3.6e6 is kWh.
            total += max(0.0, -delivered) * gap / 3_600_000.0
        return total, known

    # -- reading -----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Return the bounded, JSON-safe view the diagnostics payload uses."""
        if not self._records:
            return {
                "available": False,
                "date": None,
                "interval_minutes": INTERVAL_MINUTES,
                "record_count": 0,
                "records": [],
                "summary": {},
            }
        now = self._now()
        current = quarter_index(now)
        records: list[dict[str, Any]] = []
        for index in range(MAX_RECORDS):
            record = self._records.get(index)
            if record is not None:
                records.append(
                    {key: value for key, value in record.items() if not key.startswith("_")}
                )
            elif self._is_unobserved_past(index, current):
                records.append(self._past(index, now))
        summary: dict[str, int] = {}
        for record in records:
            verdict = record.get("verdict")
            key = verdict if isinstance(verdict, str) and verdict else "unknown"
            summary[key] = summary.get(key, 0) + 1
        return {
            "available": True,
            "date": self._date,
            "interval_minutes": INTERVAL_MINUTES,
            "record_count": len(records),
            "records": records,
            "summary": summary,
        }

    def _is_unobserved_past(self, index: int, current: int | None) -> bool:
        """Whether a quarter elapsed before this trace ever saw today."""
        if current is None or self._first_index is None:
            return False
        return index < self._first_index

    @staticmethod
    def _past(index: int, now: datetime) -> dict[str, Any]:
        """A quarter nobody observed: named, with every value left out."""
        return {
            "index": index,
            "slot_start": quarter_start(now, index).isoformat(),
            "price": None,
            "threshold": None,
            "reserve_kwh": None,
            "reserved_floor": None,
            "soc_pct": None,
            "discharge_kwh": None,
            "verdict": "past",
            "blockers": [],
        }
