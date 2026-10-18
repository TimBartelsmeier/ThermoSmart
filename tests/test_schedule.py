from datetime import datetime
from types import SimpleNamespace

import pytest

import custom_components.thermosmart.schedule as schedule_module
from custom_components.thermosmart.schedule import (
    ScheduleError,
    async_manage_schedule_yaml,
    async_schedule_zone_warnings,
    legacy_events,
    next_occurrence,
    parse_events,
    resolve_event,
    resolve_target,
    zone_key,
)


def test_latest_event_carries_across_days_and_week_boundary():
    events = parse_events([
        {"days": ["mon"], "time": "06:00", "target": "comfort"},
        {"days": ["mon"], "time": "22:00", "target": "night"},
        {"days": ["tue"], "time": "07:30", "target": 20},
    ])

    assert resolve_event(events, datetime(2026, 10, 20, 5, 0)).target == "night"
    assert resolve_event(events, datetime(2026, 10, 20, 8, 0)).target == 20.0
    assert resolve_event(events, datetime(2026, 10, 19, 5, 0)).target == 20.0


def test_duplicate_timestamp_uses_last_configured_event():
    events = parse_events([
        {"days": ["mon"], "time": "06:00", "target": 18},
        {"days": ["mon"], "time": "06:00", "target": 21},
    ])
    assert resolve_event(events, datetime(2026, 10, 19, 6, 0)).target == 21.0


def test_targets_and_legacy_conversion():
    cfg = {"comfort_temp": 21, "night_temp": 18, "eco_temp": 19}
    assert resolve_target("sleep", cfg) == 18
    assert legacy_events({"sched_wd_morning": "06:00"})[0].target == "comfort"


def test_zone_key_is_human_readable_and_yaml_safe():
    assert zone_key("Living Room") == "living_room"
    assert zone_key("Mädchen's Büro") == "madchen_s_buro"
    assert zone_key("   ") == "zone"


@pytest.mark.parametrize("target", [3, 36, "unknown", True])
def test_invalid_targets_are_rejected(target):
    with pytest.raises(ScheduleError):
        parse_events([{"days": ["mon"], "time": "06:00", "target": target}])


def test_next_occurrence_returns_concrete_time_and_target_event():
    events = parse_events([
        {"days": ["mon"], "time": "06:00", "target": "comfort", "preheat": True},
    ])

    occurrence = next_occurrence(events, datetime(2026, 10, 19, 5, 30))

    assert occurrence is not None
    assert occurrence.at == datetime(2026, 10, 19, 6, 0)
    assert occurrence.minutes_until == 30
    assert occurrence.event.preheat is True


def test_next_occurrence_crosses_week_boundary():
    events = parse_events([
        {"days": ["mon"], "time": "06:00", "target": 21},
    ])

    occurrence = next_occurrence(events, datetime(2026, 10, 19, 7, 0))

    assert occurrence is not None
    assert occurrence.at == datetime(2026, 10, 26, 6, 0)
    assert occurrence.minutes_until == 10020


def test_next_occurrence_duplicate_timestamp_uses_last_configured_event():
    events = parse_events([
        {"days": ["mon"], "time": "06:00", "target": 18},
        {"days": ["mon"], "time": "06:00", "target": 21, "preheat": True},
    ])

    occurrence = next_occurrence(events, datetime(2026, 10, 19, 5, 0))

    assert occurrence is not None
    assert occurrence.event.target == 21.0


@pytest.mark.asyncio
async def test_manage_schedule_yaml_adds_missing_zones_and_sorts(monkeypatch):
    data = {
        "other": {"preserved": True},
        "zones": {"zulu": {"events": [{"target": 21}]}, "orphan": {"custom": "value"}},
    }
    writes = []

    async def read(_hass):
        return data

    async def write(_hass, value):
        writes.append(value)

    monkeypatch.setattr(schedule_module, "_read_yaml", read)
    monkeypatch.setattr(schedule_module, "_write_yaml", write)
    entries = [
        SimpleNamespace(entry_id="one", data={"name": "Living Room"}),
        SimpleNamespace(entry_id="two", data={"name": "Bedroom"}),
    ]

    await async_manage_schedule_yaml(object(), entries)

    assert list(data["zones"]) == ["bedroom", "living_room", "orphan", "zulu"]
    assert data["zones"]["bedroom"] == {"events": []}
    assert data["zones"]["living_room"] == {"events": []}
    assert data["zones"]["zulu"] == {"events": [{"target": 21}]}
    assert data["other"] == {"preserved": True}
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_manage_schedule_yaml_allows_duplicate_normalized_keys(monkeypatch):
    async def read(_hass):
        return {}

    async def write(_hass, _data):
        return None

    monkeypatch.setattr(schedule_module, "_read_yaml", read)
    monkeypatch.setattr(schedule_module, "_write_yaml", write)
    entries = [
        SimpleNamespace(entry_id="one", data={"name": "Living Room"}),
        SimpleNamespace(entry_id="two", data={"name": "living-room"}),
    ]

    await async_manage_schedule_yaml(object(), entries)


@pytest.mark.asyncio
async def test_schedule_zone_warnings_report_duplicate_and_unused_yaml_zone(monkeypatch):
    async def read(_hass):
        return {"zones": {"living_room": {"events": []}}}

    monkeypatch.setattr(schedule_module, "_read_yaml", read)
    entries = [
        SimpleNamespace(entry_id="one", data={"name": "Living Room"}),
    ]

    warnings = await async_schedule_zone_warnings(
        object(), entries, "Living-Room", current_entry_id="new"
    )

    assert warnings == ("duplicate:living_room:Living Room",)

    warnings = await async_schedule_zone_warnings(
        object(), [], "Living Room", current_entry_id="new"
    )
    assert warnings == ("existing:living_room",)
