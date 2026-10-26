"""Weekly schedule events: validation, time computation and exact-time timers.

An event means "at time T on these weekdays, set the target to X" – nothing
more. There is no notion of a currently active event and nothing is caught up
after a restart or while scheduling is off.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time

from .const import PRESETS, PRESET_NIGHT
from .target import TARGET_MAX_C, TARGET_MIN_C

_LOGGER = logging.getLogger(__name__)

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


class ScheduleConflictError(ScheduleError):
    """Two events overlap on at least one weekday at the same time."""


# ── Parsing ───────────────────────────────────────────────────────────────────

def _parse_time(value: Any) -> int:
    try:
        parts = str(value).split(":")
        if len(parts) not in (2, 3) or (len(parts) == 3 and int(parts[2]) != 0):
            raise ValueError
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
    """Validate and normalize the saved event list."""
    if not isinstance(raw, list):
        raise ScheduleError("events must be a list")
    events: list[ScheduleEvent] = []
    for order, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ScheduleError(f"event {order + 1} must be a mapping")
        try:
            event = ScheduleEvent(
                days=_parse_days(item.get("days")),
                minute=_parse_time(item.get("time")),
                target=_parse_target(item.get("target")),
                preheat=item.get("preheat", False),
                order=order,
            )
        except ScheduleError as err:
            raise ScheduleError(f"event {order + 1}: {err}") from err
        if not isinstance(event.preheat, bool):
            raise ScheduleError(f"event {order + 1}: preheat must be true or false")
        for previous in events:
            if event.minute == previous.minute and set(event.days).intersection(previous.days):
                raise ScheduleConflictError(
                    f"event {order + 1}: conflicts with event {previous.order + 1} "
                    "on an overlapping weekday and time"
                )
        events.append(event)
    return events


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


def upcoming_occurrences(
    events: list[ScheduleEvent],
    now: datetime,
    *,
    enabled: bool,
    disabled_until: datetime | None = None,
    max_days: int | None = None,
    max_events: int | None = None,
) -> list[ScheduleOccurrence]:
    """List events that will run, bounded by calendar days and/or event count."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if disabled_until is not None and disabled_until.tzinfo is None:
        raise ValueError("disabled_until must be timezone-aware")
    for limit in (max_days, max_events):
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValueError("schedule limits must be positive integers or None")
    if not enabled or not events or (max_days is None and max_events is None):
        return []
    now_utc = now.astimezone(timezone.utc)
    after = now
    if disabled_until is not None and disabled_until.astimezone(timezone.utc) > now_utc:
        # An event exactly at the suspension deadline runs.
        after = (disabled_until.astimezone(timezone.utc) - timedelta(microseconds=1)).astimezone(now.tzinfo)
    end = _local_instant(now.date() + timedelta(days=max_days), 0, now.tzinfo) if max_days else None
    result: list[ScheduleOccurrence] = []
    while max_events is None or len(result) < max_events:
        occurrence = next_occurrence(events, after)
        if occurrence is None or (end is not None and occurrence.at.astimezone(timezone.utc) >= end.astimezone(timezone.utc)):
            break
        seconds = (occurrence.at.astimezone(timezone.utc) - now_utc).total_seconds()
        result.append(ScheduleOccurrence(occurrence.event, occurrence.at, max(1, int((seconds + 59) // 60))))
        after = occurrence.at
    return result


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

    @callback
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
