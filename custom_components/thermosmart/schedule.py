"""Weekly schedule events: parsing, YAML management and exact-time timers.

An event means "at time T on these weekdays, set the target to X" – nothing
more. There is no notion of a currently active event and nothing is caught up
after a restart or while scheduling is off.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import yaml

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_point_in_utc_time

from .const import DOMAIN, PRESETS, PRESET_NIGHT
from .target import TARGET_MAX_C, TARGET_MIN_C

_LOGGER = logging.getLogger(__name__)

SCHEDULE_FILENAME = "thermosmart_schedules.yaml"
_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# "sleep" is accepted as an alias of the night preset (HA's own preset name).
_PRESET_ALIASES = {"sleep": PRESET_NIGHT}


@dataclass(frozen=True, slots=True)
class ScheduleEvent:
    days: tuple[int, ...]
    minute: int
    target: float | str
    preheat: bool
    order: int

    @property
    def key(self) -> str:
        return f"{','.join(_DAY_NAMES[d] for d in self.days)}@{self.minute:04d}#{self.order}"


@dataclass(frozen=True, slots=True)
class ScheduleOccurrence:
    """A concrete occurrence of a recurring schedule event."""

    event: ScheduleEvent
    at: datetime
    minutes_until: int


class ScheduleError(ValueError):
    """Invalid schedule configuration."""


# ── Parsing ───────────────────────────────────────────────────────────────────

def _parse_time(value: Any) -> int:
    try:
        parts = str(value).split(":")
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError, IndexError) as err:
        raise ScheduleError(f"invalid time {value!r}; expected HH:MM") from err
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ScheduleError(f"invalid time {value!r}; expected HH:MM")
    return hour * 60 + minute


def _parse_days(value: Any) -> tuple[int, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ScheduleError("days must be a non-empty list")
    result: list[int] = []
    for day in value:
        if isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6:
            index = day
        elif isinstance(day, str) and day.strip().lower() in _DAY_NAMES:
            index = _DAY_NAMES.index(day.strip().lower())
        else:
            raise ScheduleError(f"invalid weekday {day!r}")
        if index not in result:
            result.append(index)
    return tuple(sorted(result))


def _parse_target(value: Any) -> float | str:
    if isinstance(value, bool):
        raise ScheduleError("target must be a temperature or preset")
    if isinstance(value, (int, float)):
        target = float(value)
        if not TARGET_MIN_C <= target <= TARGET_MAX_C:
            raise ScheduleError(
                f"numeric target {value!r} must be between {TARGET_MIN_C:g} and "
                f"{TARGET_MAX_C:g}°C"
            )
        return target
    if isinstance(value, str):
        name = _PRESET_ALIASES.get(value.strip().lower(), value.strip().lower())
        if name in PRESETS:
            return name
    raise ScheduleError(
        f"invalid target {value!r}; use a temperature or one of {', '.join(PRESETS)}"
    )


def parse_events(raw: Any) -> list[ScheduleEvent]:
    """Validate and normalize the ``events`` YAML list."""
    if not isinstance(raw, list):
        raise ScheduleError("events must be a list")
    events: list[ScheduleEvent] = []
    for order, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ScheduleError(f"event {order + 1} must be a mapping")
        try:
            events.append(ScheduleEvent(
                days=_parse_days(item.get("days")),
                minute=_parse_time(item.get("time")),
                target=_parse_target(item.get("target")),
                preheat=bool(item.get("preheat", False)),
                order=order,
            ))
        except ScheduleError as err:
            raise ScheduleError(f"event {order + 1}: {err}") from err
    return events


def zone_key(name: str) -> str:
    """Return the human-readable, YAML-safe key for a zone name."""
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")
    return slug or "zone"


def _entry_name(entry: Any) -> str:
    """Zone name as currently configured (options override the initial data)."""
    merged = {**entry.data, **(getattr(entry, "options", None) or {})}
    return str(merged.get("name", "Zone"))


def entry_zone_key(entry: Any) -> str:
    return zone_key(_entry_name(entry))


def event_preset(target: float | str) -> str | None:
    """The preset an event refers to, or None for a numeric target."""
    return target if isinstance(target, str) else None


# ── Time computation ──────────────────────────────────────────────────────────

def _local_instant(date, minute: int, tz) -> datetime:
    """Wall-clock ``minute`` of ``date`` in ``tz`` as an unambiguous local time.

    A time skipped by a DST jump resolves to the equivalent instant after the
    jump; an ambiguous time resolves to its first occurrence.
    """
    wall = datetime(date.year, date.month, date.day, minute // 60, minute % 60, tzinfo=tz)
    return wall.astimezone(timezone.utc).astimezone(tz)


def next_occurrence(
    events: list[ScheduleEvent], now: datetime
) -> ScheduleOccurrence | None:
    """Return the next concrete occurrence strictly after ``now``.

    ``now`` must be timezone-aware in the schedule's (local) timezone. Several
    events at the same instant resolve to the last-listed one.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    tz = now.tzinfo
    now_utc = now.astimezone(timezone.utc)
    best: tuple[datetime, ScheduleEvent] | None = None
    for day_offset in range(9):
        date = (now + timedelta(days=day_offset)).date()
        for event in events:
            if date.weekday() not in event.days:
                continue
            at = _local_instant(date, event.minute, tz)
            at_utc = at.astimezone(timezone.utc)
            if at_utc <= now_utc:
                continue
            if best is None:
                best = (at, event)
                continue
            best_utc = best[0].astimezone(timezone.utc)
            if at_utc < best_utc or (at_utc == best_utc and event.order > best[1].order):
                best = (at, event)
    if best is None:
        return None
    at, event = best
    seconds = (at.astimezone(timezone.utc) - now_utc).total_seconds()
    return ScheduleOccurrence(event, at, max(1, int((seconds + 59) // 60)))


def previous_occurrence_time(
    events: list[ScheduleEvent], now: datetime
) -> datetime | None:
    """Time of the latest occurrence at or before ``now`` (None without events)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    tz = now.tzinfo
    now_utc = now.astimezone(timezone.utc)
    best: datetime | None = None
    for day_offset in range(0, -9, -1):
        date = (now + timedelta(days=day_offset)).date()
        for event in events:
            if date.weekday() not in event.days:
                continue
            at = _local_instant(date, event.minute, tz)
            if at.astimezone(timezone.utc) > now_utc:
                continue
            if best is None or at.astimezone(timezone.utc) > best.astimezone(timezone.utc):
                best = at
    return best


def resolve_target(target: float | str, cfg: dict) -> float:
    """Temperature of an event target (preset names resolve via the zone config)."""
    if isinstance(target, float):
        return target
    from .target import preset_temperature

    return preset_temperature(cfg, target)


# ── Timer runner ──────────────────────────────────────────────────────────────

class ScheduleRunner:
    """Fires schedule events at their exact time.

    The runner only keeps one timer armed: the next occurrence. It always
    re-arms, also while scheduling is off, so the next-event information stays
    available; ``is_active`` decides at fire time whether the event is applied.
    Nothing is caught up: after ``start`` only future occurrences are armed.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        now: Callable[[], datetime],
        is_active: Callable[[datetime], bool],
        on_event: Callable[[ScheduleOccurrence], None],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._hass = hass
        self._now = now
        self._is_active = is_active
        self._on_event = on_event
        self._on_change = on_change
        self._events: list[ScheduleEvent] = []
        self._cancel: Callable[[], None] | None = None
        self._next: ScheduleOccurrence | None = None
        self._running = False

    @property
    def events(self) -> list[ScheduleEvent]:
        return list(self._events)

    @property
    def next_event(self) -> ScheduleOccurrence | None:
        """The armed occurrence (None when there are no events or not started)."""
        return self._next

    def set_events(self, events: list[ScheduleEvent]) -> None:
        self._events = list(events)
        if self._running:
            self._arm(self._now())

    def start(self) -> None:
        self._running = True
        self._arm(self._now())

    def stop(self) -> None:
        self._running = False
        self._disarm()
        self._next = None

    def _disarm(self) -> None:
        if self._cancel is not None:
            self._cancel()
            self._cancel = None

    def _arm(self, after: datetime) -> None:
        self._disarm()
        self._next = next_occurrence(self._events, after)
        if self._next is not None:
            self._cancel = async_track_point_in_utc_time(
                self._hass, self._fire, self._next.at.astimezone(timezone.utc)
            )
        if self._on_change is not None:
            self._on_change()

    def _fire(self, _now_utc: datetime) -> None:
        occurrence = self._next
        self._cancel = None
        if occurrence is None or not self._running:
            return
        now = self._now()
        # Search from the occurrence itself so a slightly early callback can
        # never fire the same occurrence twice.
        after = occurrence.at if now.astimezone(timezone.utc) < occurrence.at.astimezone(
            timezone.utc) else now
        try:
            if self._is_active(now):
                self._on_event(occurrence)
            else:
                _LOGGER.debug("schedule event %s skipped (scheduling not active)",
                              occurrence.event.key)
        except Exception:  # an event handler must never break the timer chain
            _LOGGER.exception("schedule event %s failed", occurrence.event.key)
        finally:
            self._arm(after)


# ── YAML file ─────────────────────────────────────────────────────────────────

def _path(hass: HomeAssistant) -> str:
    return hass.config.path(SCHEDULE_FILENAME)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as file:
        return file.read()


async def _read_yaml(hass: HomeAssistant) -> dict:
    path = _path(hass)
    if not os.path.exists(path):
        return {}
    text = await hass.async_add_executor_job(_read_text, path)
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as err:
        raise ScheduleError(f"cannot parse {SCHEDULE_FILENAME}: {err}") from err
    if not isinstance(data, dict):
        raise ScheduleError(f"{SCHEDULE_FILENAME} must contain a mapping")
    return data


def _atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f"{os.path.basename(path)}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


async def _write_yaml(hass: HomeAssistant, data: dict) -> None:
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
    await hass.async_add_executor_job(_atomic_write, _path(hass), text)


def _zone_entries(entries: list[ConfigEntry]) -> list[ConfigEntry]:
    return [e for e in entries if e.data.get("entry_type") != "system"]


async def _manage_locked(hass: HomeAssistant, entries: list[ConfigEntry]) -> None:
    file_exists = os.path.exists(_path(hass))
    data = await _read_yaml(hass)
    zones = data.setdefault("zones", {})
    if not isinstance(zones, dict):
        raise ScheduleError("zones in schedule YAML must be a mapping")
    managed = dict(zones)
    for entry in _zone_entries(entries):
        managed.setdefault(entry_zone_key(entry), {"events": []})
    sorted_zones = dict(sorted(managed.items()))
    if sorted_zones != zones or not file_exists:
        data["zones"] = sorted_zones
        await _write_yaml(hass, data)


async def async_manage_schedule_yaml(
    hass: HomeAssistant, entries: list[ConfigEntry]
) -> None:
    """Add missing zone blocks and keep the zone keys sorted.

    Existing blocks – including ones that belong to no zone any more – are
    never modified or deleted.
    """
    lock = hass.data.setdefault(DOMAIN, {}).setdefault("schedule_write_lock", asyncio.Lock())
    async with lock:
        await _manage_locked(hass, entries)


async def async_schedule_zone_warnings(
    hass: HomeAssistant,
    entries: list[ConfigEntry],
    name: str,
    *,
    current_entry_id: str | None = None,
) -> tuple[str, ...]:
    """Warnings for a zone name: ``duplicate:<key>:<names>`` / ``existing:<key>``."""
    key = zone_key(name)
    matching = [
        e for e in _zone_entries(entries)
        if e.entry_id != current_entry_id and entry_zone_key(e) == key
    ]
    warnings: list[str] = []
    if matching:
        warnings.append(f"duplicate:{key}:{', '.join(_entry_name(e) for e in matching)}")
    try:
        zones = (await _read_yaml(hass)).get("zones", {})
    except ScheduleError:
        return tuple(warnings)
    if isinstance(zones, dict) and key in zones and current_entry_id is None and not matching:
        warnings.append(f"existing:{key}")
    return tuple(warnings)


async def async_orphaned_zone_keys(
    hass: HomeAssistant, entries: list[ConfigEntry]
) -> tuple[str, ...]:
    """Schedule blocks that belong to no configured zone (reported, never removed)."""
    zones = (await _read_yaml(hass)).get("zones", {})
    if not isinstance(zones, dict):
        return ()
    known = {entry_zone_key(e) for e in _zone_entries(entries)}
    return tuple(sorted(str(k) for k in zones if k not in known))


async def async_load_schedule(hass: HomeAssistant, entry: ConfigEntry) -> list[ScheduleEvent]:
    """Events of the zone, or raise ScheduleError with a user-readable message."""
    zones = (await _read_yaml(hass)).get("zones", {})
    if not isinstance(zones, dict):
        raise ScheduleError("zones in schedule YAML must be a mapping")
    key = entry_zone_key(entry)
    zone = zones.get(key)
    if not zone:
        return []
    if not isinstance(zone, dict):
        raise ScheduleError(f"zone {key!r} must be a mapping")
    try:
        return parse_events(zone.get("events", []))
    except ScheduleError as err:
        raise ScheduleError(f"zone {key!r}, {err}") from err
