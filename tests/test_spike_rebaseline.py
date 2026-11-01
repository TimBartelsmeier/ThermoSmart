"""The spike filter accepts a sustained step as the new level (review M9)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.thermosmart.learning.clock import FakeClock
from tests.helpers import make_coordinator

SID = "sensor.room"


def _coord():
    clock = FakeClock(datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc))
    coord = make_coordinator(clock=clock)
    assert coord._filter_sensor_value(SID, 20.0) == 20.0
    return coord, clock


def test_isolated_spike_is_still_rejected():
    coord, _ = _coord()
    assert coord._filter_sensor_value(SID, 30.0, reported_at=1) is None
    assert coord._filter_sensor_value(SID, 20.0, reported_at=2) == 20.0
    assert coord._spike_candidates.get(SID) is None


def test_three_consistent_readings_rebaseline():
    coord, _ = _coord()
    assert coord._filter_sensor_value(SID, 15.0, reported_at=1) is None
    assert coord._filter_sensor_value(SID, 15.3, reported_at=2) is None
    assert coord._filter_sensor_value(SID, 15.2, reported_at=3) == 15.2
    assert coord._filter_sensor_value(SID, 15.1, reported_at=4) is not None


def test_inconsistent_readings_do_not_rebaseline():
    coord, _ = _coord()
    for i, value in enumerate((15.0, 26.0, 14.0, 27.0)):
        assert coord._filter_sensor_value(SID, value, reported_at=i) is None


def test_re_reading_the_same_report_does_not_count():
    coord, _ = _coord()
    for _ in range(5):
        assert coord._filter_sensor_value(SID, 15.0, reported_at="same") is None


def test_rebaseline_after_fifteen_minutes_of_rejection():
    coord, clock = _coord()
    assert coord._filter_sensor_value(SID, 15.0, reported_at="same") is None
    clock.advance(14 * 60)
    assert coord._filter_sensor_value(SID, 15.0, reported_at="same") is None
    clock.advance(60)
    assert coord._filter_sensor_value(SID, 15.0, reported_at="same") == 15.0


def test_accepted_reading_resets_the_rejection_streak():
    coord, clock = _coord()
    assert coord._filter_sensor_value(SID, 15.0, reported_at=1) is None
    clock.advance(10 * 60)
    assert coord._filter_sensor_value(SID, 20.0, reported_at=2) == 20.0
    clock.advance(10 * 60)
    assert coord._filter_sensor_value(SID, 15.0, reported_at=3) is None
