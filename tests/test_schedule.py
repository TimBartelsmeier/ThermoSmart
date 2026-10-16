from datetime import datetime

import pytest

from custom_components.thermosmart.schedule import (
    ScheduleError,
    legacy_events,
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
