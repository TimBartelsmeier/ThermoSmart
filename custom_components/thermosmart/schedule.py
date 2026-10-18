"""Flexible weekly setpoint-change schedules."""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import yaml

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_SCHED_WD_MORNING,
    CONF_SCHED_WD_NIGHT,
    CONF_SCHED_WE_MORNING,
    CONF_SCHED_WE_NIGHT,
    DOMAIN,
)

SCHEDULE_FILENAME = "thermosmart_schedules.yaml"
_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_PRESETS = {"comfort", "night", "sleep", "eco", "away", "vacation"}
_LEGACY_KEYS = (
    "schedule_enabled",
    CONF_SCHED_WD_MORNING,
    CONF_SCHED_WD_NIGHT,
    CONF_SCHED_WE_MORNING,
    CONF_SCHED_WE_NIGHT,
)
_LEGACY_TIME_KEYS = _LEGACY_KEYS[1:]


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
        if isinstance(day, int) and 0 <= day <= 6:
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
        if not 4.0 <= target <= 35.0:
            raise ScheduleError("numeric target must be between 4 and 35°C")
        return target
    if isinstance(value, str) and value.lower() in _PRESETS:
        return value.lower()
    raise ScheduleError(f"invalid target {value!r}")


def zone_key(name: str) -> str:
    """Return the human-readable, YAML-safe key for a zone name."""
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")
    return slug or "zone"


def parse_events(raw: Any) -> list[ScheduleEvent]:
    """Validate and normalize the ``events`` YAML list."""
    if not isinstance(raw, list):
        raise ScheduleError("events must be a list")
    events: list[ScheduleEvent] = []
    for order, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ScheduleError(f"event {order + 1} must be a mapping")
        events.append(ScheduleEvent(
            days=_parse_days(item.get("days")),
            minute=_parse_time(item.get("time")),
            target=_parse_target(item.get("target")),
            preheat=bool(item.get("preheat", False)),
            order=order,
        ))
    return events


def legacy_events(cfg: dict) -> list[ScheduleEvent]:
    """Convert the old four time fields to equivalent preset events."""
    values = (
        (range(5), cfg.get(CONF_SCHED_WD_MORNING, "06:00"), "comfort"),
        (range(5), cfg.get(CONF_SCHED_WD_NIGHT, "22:00"), "night"),
        (range(5, 7), cfg.get(CONF_SCHED_WE_MORNING, "08:00"), "comfort"),
        (range(5, 7), cfg.get(CONF_SCHED_WE_NIGHT, "23:00"), "night"),
    )
    return [
        ScheduleEvent(tuple(days), _parse_time(time), target, False, order)
        for order, (days, time, target) in enumerate(values)
    ]


def resolve_event(events: list[ScheduleEvent], now: datetime) -> ScheduleEvent | None:
    """Return the latest event in the repeating weekly timeline."""
    if not events:
        return None
    current = now.weekday() * 1440 + now.hour * 60 + now.minute
    candidates = [
        (day * 1440 + event.minute, event)
        for event in events
        for day in event.days
    ]
    prior = [item for item in candidates if item[0] <= current]
    if not prior:
        return max(candidates, key=lambda item: (item[0], item[1].order))[1]
    return max(prior, key=lambda item: (item[0], item[1].order))[1]


def next_occurrence(
    events: list[ScheduleEvent], now: datetime
) -> ScheduleOccurrence | None:
    """Return the next concrete occurrence after ``now``.

    Occurrences at the same weekday/time retain the existing last-configured
    event precedence. ``now`` is expected to use the local timezone used by the
    schedule configuration.
    """
    candidates: list[tuple[timedelta, ScheduleEvent, datetime]] = []
    for day_offset in range(8):
        date = (now + timedelta(days=day_offset)).date()
        weekday = date.weekday()
        for event in events:
            if weekday not in event.days:
                continue
            occurrence = now.replace(
                year=date.year,
                month=date.month,
                day=date.day,
                hour=event.minute // 60,
                minute=event.minute % 60,
                second=0,
                microsecond=0,
            )
            delta = occurrence - now
            if delta.total_seconds() > 0:
                candidates.append((delta, event, occurrence))
    if not candidates:
        return None
    delta, event, occurrence = min(
        candidates, key=lambda item: (item[0], -item[1].order)
    )
    return ScheduleOccurrence(
        event=event,
        at=occurrence,
        minutes_until=max(1, int((delta.total_seconds() + 59) // 60)),
    )


def resolve_target(target: float | str, cfg: dict) -> float:
    if isinstance(target, float):
        return target
    values = {
        "comfort": cfg.get("comfort_temp", 21.0),
        "night": cfg.get("night_temp", 18.0),
        "sleep": cfg.get("night_temp", 18.0),
        "eco": cfg.get("eco_temp", 19.0),
        "away": cfg.get("away_temp", 17.0),
        "vacation": cfg.get("vacation_temp", 12.0),
    }
    return float(values[target])


def _path(hass: HomeAssistant) -> str:
    return hass.config.path(SCHEDULE_FILENAME)


async def _read_yaml(hass: HomeAssistant) -> dict:
    path = _path(hass)
    if not os.path.exists(path):
        return {}
    text = await hass.async_add_executor_job(_read_text, path)
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as err:
        raise ScheduleError(f"cannot parse {path}: {err}") from err
    if not isinstance(data, dict):
        raise ScheduleError(f"{path} must contain a mapping")
    return data


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as file:
        return file.read()


async def _write_yaml(hass: HomeAssistant, data: dict) -> None:
    path = _path(hass)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
    await hass.async_add_executor_job(_atomic_write, path, text)


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


async def _async_manage_schedule_yaml_locked(
    hass: HomeAssistant, entries: list[ConfigEntry]
) -> None:
    path = _path(hass)
    file_exists = os.path.exists(path)
    data = await _read_yaml(hass)
    zones = data.setdefault("zones", {})
    if not isinstance(zones, dict):
        raise ScheduleError("zones in schedule YAML must be a mapping")

    managed_keys: dict[str, ConfigEntry] = {}
    for entry in entries:
        if entry.data.get("entry_type") == "system":
            continue
        key = zone_key(entry.data.get("name", "Zone"))
        managed_keys.setdefault(key, entry)

    managed_zones = dict(zones)
    for key in managed_keys:
        managed_zones.setdefault(key, {"events": []})

    sorted_zones = dict(sorted(managed_zones.items()))
    if sorted_zones != zones or not file_exists:
        data["zones"] = sorted_zones
        await _write_yaml(hass, data)


async def async_manage_schedule_yaml(
    hass: HomeAssistant, entries: list[ConfigEntry]
) -> None:
    """Add missing zone blocks and keep the configured zone keys sorted."""
    if not hasattr(hass, "data"):
        await _async_manage_schedule_yaml_locked(hass, entries)
        return

    domain_data = hass.data.setdefault(DOMAIN, {})
    lock = domain_data.setdefault("schedule_write_lock", asyncio.Lock())
    async with lock:
        await _async_manage_schedule_yaml_locked(hass, entries)


async def async_schedule_zone_warnings(
    hass: HomeAssistant,
    entries: list[ConfigEntry],
    name: str,
    *,
    current_entry_id: str | None = None,
) -> tuple[str, ...]:
    """Return warnings for duplicate or already-used schedule zone keys."""
    key = zone_key(name)
    matching_entries = [
        entry for entry in entries
        if entry.entry_id != current_entry_id
        and entry.data.get("entry_type") != "system"
        and zone_key(entry.data.get("name", "Zone")) == key
    ]
    warnings: list[str] = []
    if matching_entries:
        names = ", ".join(
            str(entry.data.get("name", entry.entry_id)) for entry in matching_entries
        )
        warnings.append(f"duplicate:{key}:{names}")

    data = await _read_yaml(hass)
    zones = data.get("zones", {})
    if isinstance(zones, dict) and key in zones:
        if current_entry_id is None and not matching_entries:
            warnings.append(f"existing:{key}")
    return tuple(warnings)


async def async_load_schedule(hass: HomeAssistant, entry: ConfigEntry) -> list[ScheduleEvent]:
    data = await _read_yaml(hass)
    zones = data.get("zones", {})
    if not isinstance(zones, dict):
        raise ScheduleError("zones in schedule YAML must be a mapping")
    key = zone_key(entry.data.get("name", "Zone"))
    zone = zones.get(key, {})
    if zone == {}:
        return []
    if not isinstance(zone, dict):
        raise ScheduleError(f"zone {key} must be a mapping")
    return parse_events(zone.get("events", []))


async def async_migrate_legacy(hass: HomeAssistant, entries: list[ConfigEntry]) -> None:
    """Migrate legacy schedule fields to events under the zone key."""
    affected = [
        entry for entry in entries
        if entry.data.get("entry_type") != "system"
        and any(key in {**entry.data, **entry.options} for key in _LEGACY_KEYS)
    ]
    candidates = [
        entry for entry in affected
        if any(key in {**entry.data, **entry.options} for key in _LEGACY_TIME_KEYS)
    ]
    data = await _read_yaml(hass)
    zones = data.setdefault("zones", {})
    if not isinstance(zones, dict):
        raise ScheduleError("zones in schedule YAML must be a mapping")
    changed = False
    for entry in candidates:
        key = zone_key(entry.data.get("name", "Zone"))
        zone = zones.setdefault(key, {})
        if not isinstance(zone, dict):
            raise ScheduleError(f"zone {key} must be a mapping")
        zone.setdefault("events", [
            {"days": [_DAY_NAMES[day] for day in event.days],
             "time": f"{event.minute // 60:02d}:{event.minute % 60:02d}",
             "target": event.target, "preheat": event.preheat}
            for event in legacy_events({**entry.data, **entry.options})
        ])
        changed = True
    if changed:
        await _write_yaml(hass, data)
    for entry in affected:
        new_data = {key: value for key, value in entry.data.items() if key not in _LEGACY_KEYS}
        new_options = {
            key: value for key, value in entry.options.items() if key not in _LEGACY_KEYS
        }
        hass.config_entries.async_update_entry(entry, data=new_data, options=new_options)
