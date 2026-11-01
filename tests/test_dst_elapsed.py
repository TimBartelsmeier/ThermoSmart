"""Elapsed-time arithmetic is DST-safe (review M5)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.thermosmart import preheat
from custom_components.thermosmart.learning.clock import FakeClock
from tests.helpers import make_coordinator, make_state, set_hass_states

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


@pytest.fixture(autouse=True)
def berlin_timezone():
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(BERLIN)
    yield
    dt_util.set_default_time_zone(previous)


def _plan(now, event_at, minutes=120.0):
    return preheat.plan(
        now=now, scheduling_active=True, event_at=event_at, event_target=22.0,
        event_preheat=True, desired_target=18.0, previous_event_at=None,
        duration_min=minutes, duration_status="ok", cancelled=False, started_at=None,
    )


@pytest.mark.parametrize("event_at", [
    datetime(2026, 3, 29, 3, 30, tzinfo=BERLIN),  # spring forward (02:00 → 03:00)
    datetime(2026, 10, 25, 3, 30, tzinfo=BERLIN),  # fall back (03:00 → 02:00)
])
def test_preheat_lead_is_real_elapsed_time(event_at):
    now = event_at - timedelta(days=1)
    result = _plan(now, event_at)
    assert result.state == preheat.PLANNED
    assert event_at.astimezone(UTC) - result.start == timedelta(minutes=120)


@pytest.mark.parametrize("fold", [0, 1])
def test_preheat_activation_in_repeated_hour(fold):
    event_at = datetime(2026, 10, 25, 3, 0, tzinfo=BERLIN)  # 02:00 UTC
    now = datetime(2026, 10, 25, 2, 30, fold=fold, tzinfo=BERLIN)  # 00:30 / 01:30 UTC
    result = _plan(now, event_at, minutes=60.0)  # starts 01:00 UTC
    assert result.state == (preheat.ACTIVE if fold else preheat.PLANNED)


def _window_coordinator(start_utc):
    clock = FakeClock(start_utc)
    coord = make_coordinator(
        {"window_sensors": ["binary_sensor.w"], "window_open_delay": 20,
         "window_close_delay": 20},
        clock=clock,
    )
    set_hass_states(coord, {"binary_sensor.w": make_state("on")})
    return coord, clock


def test_window_open_delay_across_fall_back():
    # 02:59:50 CEST → 20 real seconds later it is 02:00:10 CET.
    coord, clock = _window_coordinator(datetime(2026, 10, 25, 0, 59, 50, tzinfo=UTC))
    coord._window_open_at["binary_sensor.w"] = coord._now_utc()
    assert coord._check_window_open(coord.zone_cfg) is False
    clock.advance(20)
    assert coord._check_window_open(coord.zone_cfg) is True


def test_window_close_delay_across_spring_forward():
    # 01:59:50 CET → 20 real seconds later it is 03:00:10 CEST.
    coord, clock = _window_coordinator(datetime(2026, 3, 29, 0, 59, 50, tzinfo=UTC))
    set_hass_states(coord, {"binary_sensor.w": make_state("off")})
    coord._window_close_at["binary_sensor.w"] = coord._now_utc()
    clock.advance(10)
    assert coord._check_window_open(coord.zone_cfg) is True
    clock.advance(10)
    assert coord._check_window_open(coord.zone_cfg) is False
