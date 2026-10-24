"""Coordinator-level scheduling: events through the desired target, enable/pause."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.thermosmart import schedule as sched
from custom_components.thermosmart.learning.clock import FakeClock
from custom_components.thermosmart.schedule import ScheduleError, parse_events
from custom_components.thermosmart.target import SOURCE_EVENT, SOURCE_USER
from tests.helpers import make_coordinator
from tests.test_schedule import _Timers

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture(autouse=True)
def berlin_timezone():
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(BERLIN)
    yield
    dt_util.set_default_time_zone(previous)


@pytest.fixture
def timers():
    t = _Timers()
    with patch.object(sched, "async_track_point_in_utc_time", t):
        yield t


def _coord(events, *, overrides=None, start=datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN)):
    clock = FakeClock(start)
    coord = make_coordinator(overrides, clock=clock)
    coord.hass.async_create_task = lambda coro: coro.close()
    coord.async_update_listeners = MagicMock()
    coord.scheduler.set_events(parse_events(events))
    coord.start_scheduler()
    return coord, clock


def _fire_next(coord, clock, timers):
    (timer,) = timers.live
    timer["cancelled"] = True
    clock.set_utc(timer["when"])
    timer["action"](timer["when"])


WED_15 = [{"days": ["wed"], "time": "15:00", "target": 23}]


class TestEventsApplyThroughDesiredTarget:
    def test_numeric_event_sets_target_from_event_source(self, timers):
        coord, clock = _coord(WED_15)
        coord._manual_change_pending = False
        _fire_next(coord, clock, timers)
        assert coord.target.value == 23.0
        assert coord.target.current.source == SOURCE_EVENT
        assert coord.target.preset is None
        assert coord._manual_change_pending is False

    def test_preset_event_uses_configured_temperature_and_preset(self, timers):
        coord, clock = _coord(
            [{"days": ["wed"], "time": "15:00", "target": "night"}],
            overrides={"night_temp": 17.5},
        )
        _fire_next(coord, clock, timers)
        assert coord.target.value == 17.5
        assert coord.target.preset == "night"

    def test_event_fires_at_exact_time_not_next_cycle(self, timers):
        coord, clock = _coord(WED_15)
        (timer,) = timers.live
        assert timer["when"] == datetime(2026, 5, 6, 13, 0, tzinfo=timezone.utc)
        assert coord.target.value != 23.0

    def test_event_overrides_earlier_manual_change(self, timers):
        coord, clock = _coord(WED_15)
        coord.set_target(19.0, SOURCE_USER)
        _fire_next(coord, clock, timers)
        assert coord.target.value == 23.0

    def test_manual_change_after_event_wins(self, timers):
        coord, clock = _coord(WED_15)
        _fire_next(coord, clock, timers)
        coord.set_target(19.0, SOURCE_USER)
        assert coord.target.value == 19.0

    def test_event_during_vacation_updates_target_to_restore(self, timers):
        coord, clock = _coord(WED_15)
        coord.set_system_state(vacation=True, summer=False)
        _fire_next(coord, clock, timers)
        assert coord.target.value == 23.0

    def test_event_during_summer_updates_target_to_restore(self, timers):
        coord, clock = _coord(WED_15)
        coord._is_summer = True
        _fire_next(coord, clock, timers)
        assert coord.target.value == 23.0

    def test_unusable_preset_temperature_is_rejected_and_logged(self, timers, caplog):
        coord, clock = _coord(
            [{"days": ["wed"], "time": "15:00", "target": "vacation"}],
            overrides={"vacation_temp": 3.0},
        )
        before = coord.target.value
        _fire_next(coord, clock, timers)
        assert coord.target.value == before
        assert "ignored" in caplog.text

    def test_restart_does_not_apply_missed_events(self, timers):
        coord, clock = _coord(WED_15, start=datetime(2026, 5, 6, 16, 0, tzinfo=BERLIN))
        assert coord.target.value == 21.0
        assert coord.scheduler.next_event.at == datetime(2026, 5, 13, 15, 0, tzinfo=BERLIN)


class TestEnableAndPause:
    def test_disabled_skips_event_but_keeps_following_ones(self, timers):
        coord, clock = _coord([
            {"days": ["wed"], "time": "15:00", "target": 23},
            {"days": ["wed"], "time": "16:00", "target": 18},
        ])
        coord.set_scheduling_enabled(False)
        _fire_next(coord, clock, timers)
        assert coord.target.value == 21.0
        coord.set_scheduling_enabled(True)  # nothing is applied retroactively
        assert coord.target.value == 21.0
        _fire_next(coord, clock, timers)
        assert coord.target.value == 18.0

    def test_disabled_until_in_the_future_pauses(self, timers):
        coord, clock = _coord([
            {"days": ["wed"], "time": "15:00", "target": 23},
            {"days": ["wed"], "time": "18:00", "target": 18},
        ])
        coord.set_scheduling_disabled_until(datetime(2026, 5, 6, 16, 0, tzinfo=BERLIN))
        assert coord.scheduling_active is False
        _fire_next(coord, clock, timers)
        assert coord.target.value == 21.0  # skipped, not caught up later
        _fire_next(coord, clock, timers)
        assert coord.target.value == 18.0  # pause expired by itself

    def test_disabled_until_boundary_is_inclusive(self, timers):
        coord, clock = _coord(WED_15)
        coord.set_scheduling_disabled_until(datetime(2026, 5, 6, 15, 0, tzinfo=BERLIN))
        _fire_next(coord, clock, timers)
        assert coord.target.value == 23.0

    def test_switch_off_takes_precedence_over_expired_pause(self):
        coord, _ = _coord(WED_15)
        coord.set_scheduling_enabled(False)
        coord.set_scheduling_disabled_until(datetime(2026, 5, 1, tzinfo=BERLIN))
        assert coord.scheduling_active is False

    def test_clearing_the_pause_activates_again(self):
        coord, _ = _coord(WED_15)
        coord.set_scheduling_disabled_until(datetime(2026, 5, 9, tzinfo=BERLIN))
        assert coord.scheduling_active is False
        coord.set_scheduling_disabled_until(None)
        assert coord.scheduling_active is True

    def test_manual_changes_still_work_while_disabled(self):
        coord, _ = _coord(WED_15)
        coord.set_scheduling_enabled(False)
        coord.set_target(19.0, SOURCE_USER)
        assert coord.target.value == 19.0


class TestLoadAndLifecycle:
    async def test_load_failure_runs_without_events_and_exposes_error(self, timers, caplog):
        coord, _ = _coord(WED_15)
        with patch("custom_components.thermosmart.coordinator.async_load_schedule",
                   AsyncMock(side_effect=ScheduleError("zone 'x', event 1: bad time"))):
            await coord.async_load_schedule()
        assert coord.schedule_error == "zone 'x', event 1: bad time"
        assert coord.scheduler.events == [] and coord.scheduler.next_event is None
        assert "schedule not loaded" in caplog.text

    async def test_reload_replaces_events_and_clears_error(self, timers):
        coord, _ = _coord([])
        coord.schedule_error = "old"
        events = parse_events(WED_15)
        with patch("custom_components.thermosmart.coordinator.async_load_schedule",
                   AsyncMock(return_value=events)):
            await coord.async_load_schedule()
        assert coord.schedule_error is None
        assert coord.scheduler.next_event is not None

    def test_cleanup_cancels_timer(self, timers):
        coord, _ = _coord(WED_15)
        coord.cleanup_event_listeners()
        assert timers.live == []

    def test_listeners_notified_when_next_event_changes(self, timers):
        coord, clock = _coord(WED_15)
        coord.async_update_listeners.reset_mock()
        _fire_next(coord, clock, timers)
        assert coord.async_update_listeners.called
