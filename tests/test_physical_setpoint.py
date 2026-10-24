"""Physical TRV setpoint changes become debounced zone overrides."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.thermosmart.const import (
    CONF_DEVICE_SETTINGS,
    CONF_DEV_ACCEPT_SETPOINT_CHANGES,
    CONF_TRV_SETPOINT_DEBOUNCE,
    CONF_TRV_SETPOINT_SETTLING,
)
from tests.helpers import make_coordinator


def _state(setpoint: float, state: str = "heat"):
    return SimpleNamespace(
        state=state,
        attributes={"temperature": setpoint},
    )


def _cfg(**options):
    device = options.pop("device", True)
    return {
        CONF_DEVICE_SETTINGS: {
            "climate.trv": {CONF_DEV_ACCEPT_SETPOINT_CHANGES: device},
        },
        CONF_TRV_SETPOINT_DEBOUNCE: 5,
        CONF_TRV_SETPOINT_SETTLING: 5,
        **options,
    }


def _coordinator():
    coordinator = make_coordinator()
    coordinator._active_control = True
    coordinator._is_summer = False
    coordinator.async_request_refresh = AsyncMock()

    def discard_task(coro):
        coro.close()

    coordinator.hass.async_create_task = discard_task
    return coordinator


class TestPhysicalSetpoint:
    def test_change_adopts_value_after_configured_debounce(self):
        coordinator = _coordinator()
        callbacks = []
        with patch(
            "custom_components.thermosmart.coordinator.async_call_later",
            side_effect=lambda hass, delay, callback: (
                callbacks.append((delay, callback)) or (lambda: None)
            ),
        ):
            coordinator._handle_trv_setpoint_change(
                "climate.trv", _state(19), _state(20), _cfg()
            )

        assert callbacks[0][0] == 5
        assert coordinator.get_override() is None
        callbacks[0][1](None)
        assert coordinator.get_override() == 20
        coordinator.async_request_refresh.assert_called_once()

    def test_debounce_uses_last_reported_value(self):
        coordinator = _coordinator()
        callbacks = []
        cancels = []

        def schedule(hass, delay, callback):
            callbacks.append(callback)
            cancel = MagicMock()
            cancels.append(cancel)
            return cancel

        with patch(
            "custom_components.thermosmart.coordinator.async_call_later",
            side_effect=schedule,
        ):
            coordinator._handle_trv_setpoint_change(
                "climate.trv", _state(19), _state(20), _cfg()
            )
            coordinator._handle_trv_setpoint_change(
                "climate.trv", _state(20), _state(21), _cfg()
            )

        cancels[0].assert_called_once()
        callbacks[0](None)
        assert coordinator.get_override() is None
        callbacks[1](None)
        assert coordinator.get_override() == 21

    def test_settling_suppresses_device_echoes(self):
        coordinator = _coordinator()
        coordinator._mark_trv_write("climate.trv", _cfg())
        with patch(
            "custom_components.thermosmart.coordinator.async_call_later"
        ) as schedule:
            coordinator._handle_trv_setpoint_change(
                "climate.trv", _state(19), _state(21), _cfg()
            )
        schedule.assert_not_called()

    def test_window_open_change_is_still_adopted(self):
        coordinator = _coordinator()
        coordinator.data = {
            "zone": {"window_open": True},
            "presence": {"vacation": False},
        }
        callbacks = []
        with patch(
            "custom_components.thermosmart.coordinator.async_call_later",
            side_effect=lambda hass, delay, callback: (
                callbacks.append(callback) or (lambda: None)
            ),
        ):
            coordinator._handle_trv_setpoint_change(
                "climate.trv", _state(5), _state(20, state="off"), _cfg()
            )
        callbacks[0](None)
        assert coordinator.get_override() == 20

    def test_disabled_setting_retains_corrective_refresh(self):
        coordinator = _coordinator()
        coordinator._last_written_setpoints["climate.trv"] = 19
        with patch(
            "custom_components.thermosmart.coordinator.async_call_later"
        ) as schedule:
            coordinator._handle_trv_setpoint_change(
                "climate.trv", _state(19), _state(21), _cfg(device=False)
            )
        schedule.assert_not_called()
        coordinator.async_request_refresh.assert_called_once()
        assert coordinator.get_override() is None

    @pytest.mark.parametrize("inactive", ["control_off", "vacation", "summer"])
    def test_inactive_zone_ignores_change(self, inactive):
        coordinator = _coordinator()
        if inactive == "control_off":
            coordinator._active_control = False
        elif inactive == "vacation":
            coordinator.data = {"presence": {"vacation": True}}
        else:
            coordinator._is_summer = True
        with patch(
            "custom_components.thermosmart.coordinator.async_call_later"
        ) as schedule:
            coordinator._handle_trv_setpoint_change(
                "climate.trv", _state(19), _state(21), _cfg()
            )
        schedule.assert_not_called()
        assert coordinator.get_override() is None

    def test_no_settling_delay_can_be_configured(self):
        coordinator = _coordinator()
        coordinator._mark_trv_write(
            "climate.trv", _cfg(**{CONF_TRV_SETPOINT_SETTLING: 0})
        )
        assert not coordinator._trv_write_is_settling("climate.trv")

    def test_sub_five_value_uses_same_override_semantics(self):
        coordinator = _coordinator()
        coordinator._override = 20
        callbacks = []
        with patch(
            "custom_components.thermosmart.coordinator.async_call_later",
            side_effect=lambda hass, delay, callback: (
                callbacks.append(callback) or (lambda: None)
            ),
        ):
            coordinator._handle_trv_setpoint_change(
                "climate.trv", _state(20), _state(4), _cfg()
            )
        callbacks[0](None)
        assert coordinator.get_override() is None
