"""Per-device 'turn off when window is open' behaviour."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.thermosmart.const import (
    CONF_DEV_WINDOW_OFF,
    CONF_DEVICE_SETTINGS,
)
from tests.helpers import make_coordinator, make_state, set_hass_states

_REC_WINDOW = {"adjusted_target": None, "trv_setpoint": None, "window_open": True}
_REC_NORMAL = {"adjusted_target": 21.0, "trv_setpoint": 21.0, "window_open": False}


def _trv(state="heat", modes=("off", "heat"), setpoint=21.0):
    return make_state(
        state,
        {
            "temperature": setpoint,
            "current_temperature": 20.0,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "target_temp_step": 0.5,
            "hvac_modes": list(modes),
        },
    )


def _cfg(*entities, off=()):
    return {
        "climate_entities": list(entities),
        "temp_tolerance": 0.5,
        "window_open_temp": 5.0,
        CONF_DEVICE_SETTINGS: {
            e: {CONF_DEV_WINDOW_OFF: e in off} for e in entities
        },
    }


def _coord(states):
    coord = make_coordinator()
    coord.hass.services.async_call = AsyncMock(return_value=None)
    set_hass_states(coord, states)
    return coord


def _calls(coord):
    return [c.args[:3] for c in coord.hass.services.async_call.call_args_list]


class TestWindowOff:
    async def test_turns_off_instead_of_setpoint(self):
        coord = _coord({"climate.a": _trv()})
        await coord._apply_temperature(_cfg("climate.a", off=["climate.a"]), _REC_WINDOW)
        calls = _calls(coord)
        assert ("climate", "set_hvac_mode", {"entity_id": "climate.a", "hvac_mode": "off"}) in calls
        assert not any(c[1] == "set_temperature" for c in calls)
        assert "climate.a" in coord._window_off_entities

    async def test_already_off_no_call(self):
        coord = _coord({"climate.a": _trv(state="off")})
        await coord._apply_temperature(_cfg("climate.a", off=["climate.a"]), _REC_WINDOW)
        assert _calls(coord) == []

    async def test_falls_back_to_setpoint_without_off_mode(self):
        coord = _coord({"climate.a": _trv(modes=("heat",))})
        await coord._apply_temperature(_cfg("climate.a", off=["climate.a"]), _REC_WINDOW)
        calls = _calls(coord)
        assert not any(c[1] == "set_hvac_mode" for c in calls)
        assert any(c[1] == "set_temperature" and c[2]["temperature"] == 5.0 for c in calls)

    async def test_disabled_uses_window_setpoint(self):
        coord = _coord({"climate.a": _trv()})
        await coord._apply_temperature(_cfg("climate.a"), _REC_WINDOW)
        calls = _calls(coord)
        assert not any(c[1] == "set_hvac_mode" for c in calls)
        assert any(c[1] == "set_temperature" for c in calls)
        assert not coord._window_off_entities

    async def test_mixed_zone(self):
        coord = _coord({"climate.a": _trv(), "climate.b": _trv()})
        cfg = _cfg("climate.a", "climate.b", off=["climate.a"])
        await coord._apply_temperature(cfg, _REC_WINDOW)
        calls = _calls(coord)
        assert any(c[1] == "set_hvac_mode" and c[2]["entity_id"] == "climate.a" for c in calls)
        assert any(c[1] == "set_temperature" and c[2]["entity_id"] == "climate.b" for c in calls)
        assert coord._window_off_entities == {"climate.a"}

    async def test_restore_on_close(self):
        coord = _coord({"climate.a": _trv(state="off")})
        coord._window_off_entities = {"climate.a"}
        coord._last_written_setpoints["climate.a"] = 5.0
        await coord._apply_temperature(_cfg("climate.a", off=["climate.a"]), _REC_NORMAL)
        calls = _calls(coord)
        assert ("climate", "set_hvac_mode", {"entity_id": "climate.a", "hvac_mode": "heat"}) in calls
        assert not coord._window_off_entities
        assert "climate.a" not in coord._last_written_setpoints or (
            coord._last_written_setpoints["climate.a"] != 5.0
        )


def test_preset_defaults():
    from custom_components.thermosmart.device_profiles.registry import get_preset

    assert get_preset("TRVZB", "SONOFF").window_off is True
    assert get_preset("Unknown", "Nobody").window_off is False
