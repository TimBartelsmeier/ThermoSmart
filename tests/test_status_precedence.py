"""Status sensor precedence (review M11)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.thermosmart.sensor import ThermoSmartStatusSensor


def _status(zone, *, active=True, learn=True):
    coord = MagicMock()
    coord._active_control = active
    coord.learn_enabled = learn
    coord.data = {"zone": zone}
    entry = MagicMock()
    entry.entry_id = "z"
    entry.data = {"name": "Zone"}
    return ThermoSmartStatusSensor(coord, entry).native_value


ALL = {"heating_failure": True, "window_open": True, "is_summer": True,
       "vacation_mode": True, "preheat_active": True,
       "current_temp": 18.0, "effective_target": 21.0}


@pytest.mark.parametrize(("drop", "expected"), [
    ((), "heating_failure"),
    (("heating_failure",), "window_open"),
    (("heating_failure", "window_open"), "summer"),
    (("heating_failure", "window_open", "is_summer"), "vacation"),
    (("heating_failure", "window_open", "is_summer", "vacation_mode"), "preheating"),
])
@pytest.mark.parametrize("active", [True, False])
def test_failure_then_holds_then_preheat_regardless_of_switches(drop, expected, active):
    zone = {k: v for k, v in ALL.items() if k not in drop}
    assert _status(zone, active=active, learn=False) == expected


def test_observation_and_disabled():
    assert _status({"current_temp": 18.0}, active=False, learn=True) == "observation_mode"
    assert _status({"current_temp": 18.0}, active=False, learn=False) == "disabled"


@pytest.mark.parametrize("learn", [True, False])
def test_controlling_does_not_depend_on_learning(learn):
    zone = {"current_temp": 18.0, "effective_target": 21.0}
    assert _status(zone, learn=learn) == "heating"
    zone["current_temp"] = 21.0
    assert _status(zone, learn=learn) == "idle"
