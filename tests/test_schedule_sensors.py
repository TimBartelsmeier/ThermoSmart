from datetime import datetime
from unittest.mock import patch

from custom_components.thermosmart.sensor import (
    ThermoSmartNextScheduleEventSensor,
    ThermoSmartPreheatStartSensor,
)

from tests.helpers import make_coordinator


def _sensor_data():
    coordinator = make_coordinator()
    coordinator.data = {
        "zone": {
            "next_schedule_event": "mon@0600#0",
            "next_schedule_event_at": datetime(2026, 10, 19, 6, 0),
            "next_schedule_minutes": 30,
            "next_schedule_target": "comfort",
            "next_schedule_target_temperature": 21.0,
            "next_schedule_preheat": True,
            "preheat_start_at": datetime(2026, 10, 19, 5, 0),
            "preheat_minutes": 60,
            "preheat_state": "scheduled",
            "preheat_suppression_reason": "outside_preheat_window",
        }
    }
    with patch("homeassistant.helpers.frame.report_usage"):
        return (
            ThermoSmartNextScheduleEventSensor(coordinator, coordinator.entry),
            ThermoSmartPreheatStartSensor(coordinator, coordinator.entry),
        )


def test_next_schedule_event_sensor_exposes_timing_and_target():
    event_sensor, _ = _sensor_data()

    assert event_sensor.native_value == datetime(2026, 10, 19, 6, 0)
    assert event_sensor.extra_state_attributes == {
        "event_key": "mon@0600#0",
        "minutes_until": 30,
        "target": "comfort",
        "target_temperature": 21.0,
        "preheat": True,
    }


def test_preheat_start_sensor_keeps_duration_and_state_distinct():
    _, start_sensor = _sensor_data()

    assert start_sensor.native_value == datetime(2026, 10, 19, 5, 0)
    assert start_sensor.extra_state_attributes == {
        "event_at": datetime(2026, 10, 19, 6, 0),
        "duration_minutes": 60,
        "state": "scheduled",
        "suppression_reason": "outside_preheat_window",
    }
