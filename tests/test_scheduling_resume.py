"""A timed scheduling pause resumes by itself at its deadline (review M6)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from custom_components.thermosmart import triggers as trig
from custom_components.thermosmart.learning.clock import FakeClock
from tests.helpers import make_coordinator

UTC = timezone.utc
START = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)


def _coord():
    coord = make_coordinator(clock=FakeClock(START))
    coord.async_update_listeners = MagicMock()
    return coord


def _track():
    armed = []

    def track(hass, action, when):
        cancel = MagicMock()
        armed.append((action, when, cancel))
        return cancel

    return armed, patch(
        "custom_components.thermosmart.coordinator.async_track_point_in_utc_time", track)


def test_resume_timer_clears_pause_and_requests_pass():
    coord = _coord()
    armed, tracker = _track()
    until = START + timedelta(hours=2)
    with tracker:
        coord.set_scheduling_disabled_until(until)
    ((action, when, _),) = armed
    assert when == until
    assert coord.scheduling_active is False
    coord._pending_triggers.clear()
    action(until)
    assert coord.scheduling_disabled_until is None
    assert coord._pending_triggers == [trig.TRIGGER_SCHEDULING]
    coord.async_update_listeners.assert_called()


def test_changing_or_clearing_the_pause_cancels_the_old_timer():
    coord = _coord()
    armed, tracker = _track()
    with tracker:
        coord.set_scheduling_disabled_until(START + timedelta(hours=2))
        coord.set_scheduling_disabled_until(START + timedelta(hours=3))
        assert armed[0][2].called
        coord.set_scheduling_disabled_until(None)
        assert armed[1][2].called
    assert len(armed) == 2


def test_expired_deadline_is_not_a_pause():
    coord = _coord()
    armed, tracker = _track()
    with tracker:
        coord.set_scheduling_disabled_until(START - timedelta(minutes=1))
    assert armed == []
    assert coord.scheduling_disabled_until is None
    assert coord.scheduling_active is True


def test_unload_cancels_resume_timer():
    coord = _coord()
    armed, tracker = _track()
    with tracker:
        coord.set_scheduling_disabled_until(START + timedelta(hours=2))
    coord.cleanup_event_listeners()
    assert armed[0][2].called
