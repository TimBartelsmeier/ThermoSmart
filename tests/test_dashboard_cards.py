"""Scheduling limits, suspension shortcuts and registry-backed card data."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError

from custom_components.thermosmart.dashboard import _limit, suspension_deadline
from custom_components.thermosmart.schedule import parse_events, upcoming_occurrences

TZ = ZoneInfo("Europe/Berlin")


def _events():
    return parse_events([
        {"days": list(range(7)), "time": "08:00", "target": "comfort"},
        {"days": list(range(7)), "time": "22:00", "target": 18},
    ])


def test_event_and_calendar_limits_intersect():
    now = datetime(2026, 10, 26, 9, tzinfo=TZ)
    events = upcoming_occurrences(_events(), now, enabled=True, max_days=2, max_events=5)
    assert [e.at for e in events] == [
        datetime(2026, 10, 26, 22, tzinfo=TZ),
        datetime(2026, 10, 27, 8, tzinfo=TZ),
        datetime(2026, 10, 27, 22, tzinfo=TZ),
    ]
    assert len(upcoming_occurrences(_events(), now, enabled=True, max_events=5)) == 5
    assert len(upcoming_occurrences(_events(), now, enabled=True, max_days=1)) == 1
    assert len(upcoming_occurrences(_events(), now, enabled=True, max_days=2, max_events=1)) == 1


def test_disabled_and_unbounded_show_no_events():
    now = datetime(2026, 10, 26, 9, tzinfo=TZ)
    assert upcoming_occurrences(_events(), now, enabled=False, max_events=5) == []
    assert upcoming_occurrences(_events(), now, enabled=True) == []
    assert upcoming_occurrences([], now, enabled=True, max_events=5) == []


def test_pause_includes_event_exactly_at_deadline_and_horizon_stays_anchored_today():
    now = datetime(2026, 10, 26, 9, tzinfo=TZ)
    until = datetime(2026, 10, 27, 8, tzinfo=TZ)
    events = upcoming_occurrences(_events(), now, enabled=True, disabled_until=until, max_events=2)
    assert events[0].at == until
    assert events[0].minutes_until == 23 * 60
    assert upcoming_occurrences(_events(), now, enabled=True, disabled_until=until, max_days=1) == []
    skipped = upcoming_occurrences(
        _events(), now, enabled=True, disabled_until=until + timedelta(seconds=1), max_events=1
    )
    assert skipped[0].at.hour == 22


def test_dst_schedule_uses_existing_gap_and_fold_policy():
    events = parse_events([
        {"days": ["sun"], "time": "02:30", "target": 20},
        {"days": ["sun"], "time": "03:30", "target": 21},
    ])
    spring = upcoming_occurrences(
        events, datetime(2026, 3, 29, 0, tzinfo=TZ), enabled=True, max_days=1
    )
    assert len(spring) == 1
    assert spring[0].at.hour == 3
    assert spring[0].event.target == 21
    autumn = upcoming_occurrences(
        events, datetime(2026, 10, 25, 0, tzinfo=TZ), enabled=True, max_days=1
    )
    assert len(autumn) == 2
    assert autumn[0].at.fold == 0
    assert autumn[0].at.astimezone(timezone.utc).hour == 0


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "5"])
def test_limits_reject_invalid_values_without_coercion(value):
    with pytest.raises(vol.Invalid):
        _limit(value)
    with pytest.raises(ValueError):
        upcoming_occurrences(_events(), datetime.now(TZ), enabled=True, max_events=value)


def _coordinator(now, until=None):
    return SimpleNamespace(
        _now_local=lambda: now,
        scheduling_enabled=True,
        scheduling_disabled_until=until,
        scheduler=SimpleNamespace(events=_events()),
    )


@pytest.mark.parametrize("date", [(2026, 3, 28), (2026, 10, 24)])
def test_hour_shortcuts_are_elapsed_hours_across_dst(date):
    now = datetime(*date, 18, tzinfo=TZ)
    coordinator = _coordinator(now)
    for hours in (12, 24):
        deadline = suspension_deadline(coordinator, f"{hours}h")
        assert (deadline.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds() == hours * 3600
    assert suspension_deadline(coordinator, "midnight").date() == now.date() + timedelta(days=1)
    assert suspension_deadline(coordinator, "midnight").hour == 0


def test_skip_next_skips_next_runnable_event_without_shortening_pause():
    now = datetime(2026, 10, 26, 9, tzinfo=TZ)
    until = datetime(2026, 10, 27, 8, tzinfo=TZ)
    coordinator = _coordinator(now, until)
    deadline = suspension_deadline(coordinator, "skip_next")
    assert deadline == until.astimezone(timezone.utc) + timedelta(seconds=1)
    with pytest.raises(ServiceValidationError, match="shorten"):
        suspension_deadline(coordinator, "12h")
    coordinator.scheduling_enabled = False
    with pytest.raises(ServiceValidationError, match="disabled"):
        suspension_deadline(coordinator, "skip_next")


def test_skip_without_events_reports_error():
    coordinator = _coordinator(datetime.now(TZ))
    coordinator.scheduler.events = []
    with pytest.raises(ServiceValidationError, match="no next"):
        suspension_deadline(coordinator, "skip_next")


def test_suspension_active_check_distinguishes_dst_fold():
    from tests.helpers import make_coordinator

    coordinator = make_coordinator()
    deadline = datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=1)
    coordinator.set_scheduling_disabled_until(deadline)
    # The first 02:45 precedes the second 02:30, despite its wall-clock text.
    assert not coordinator._scheduling_active_at(datetime(2026, 10, 25, 2, 45, tzinfo=TZ, fold=0))
    assert coordinator._scheduling_active_at(deadline)
    assert coordinator._scheduling_active_at(datetime(2026, 10, 25, 2, 45, tzinfo=TZ, fold=1))
