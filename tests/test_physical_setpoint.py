"""Physical TRV setpoint changes become debounced desired-target changes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.thermosmart.const import (
    CONF_DEVICE_SETTINGS,
    CONF_DEV_ACCEPT_SETPOINT_CHANGES,
    CONF_TRV_SETPOINT_DEBOUNCE,
    CONF_TRV_SETPOINT_SETTLING,
    TEMP_ECO,
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
        assert coordinator.target.value == TEMP_ECO
        callbacks[0][1](None)
        assert coordinator.target.value == 20
        assert coordinator.target.current.source == "dial"
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
        assert coordinator.target.value == TEMP_ECO
        callbacks[1](None)
        assert coordinator.target.value == 21

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
        coordinator.data = {"zone": {"window_open": True}}
        callbacks = []
        with patch(
            "custom_components.thermosmart.coordinator.async_call_later",
            side_effect=lambda hass, delay, callback: (
                callbacks.append(callback) or (lambda: None)
            ),
        ):
            coordinator._handle_trv_setpoint_change(
                "climate.trv", _state(5), _state(25, state="off"), _cfg()
            )
        callbacks[0](None)
        assert coordinator.target.value == 25

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
        assert coordinator.target.value == TEMP_ECO

    def test_control_off_adopts_change_as_desired_target(self):
        """Review D2: in observation mode the dial change becomes the desired
        target; nothing is written because Active Control is off."""
        coordinator = _coordinator()
        coordinator._active_control = False
        callbacks = []
        with patch(
            "custom_components.thermosmart.coordinator.async_call_later",
            side_effect=lambda hass, delay, callback: (
                callbacks.append(callback) or (lambda: None)
            ),
        ):
            coordinator._handle_trv_setpoint_change(
                "climate.trv", _state(19), _state(25), _cfg()
            )
        callbacks[0](None)
        assert coordinator.target.value == 25
        assert coordinator.target.current.source == "dial"

    def test_control_off_rejected_change_is_not_corrected(self):
        coordinator = _coordinator()
        coordinator._active_control = False
        with patch(
            "custom_components.thermosmart.coordinator.async_call_later"
        ) as schedule:
            coordinator._handle_trv_setpoint_change(
                "climate.trv", _state(19), _state(25), _cfg(device=False)
            )
        schedule.assert_not_called()
        coordinator.async_request_refresh.assert_not_called()
        assert coordinator.target.value == TEMP_ECO

    @pytest.mark.parametrize("held", ["vacation", "summer"])
    def test_change_during_suspension_is_stored_and_corrected(self, held):
        """Review M3: the dial value becomes the desired target (applied after
        the hold); the following pass drives the TRV back to the held value."""
        coordinator = _coordinator()
        coordinator.set_system_state(vacation=held == "vacation", summer=held == "summer")
        callbacks = []
        with patch(
            "custom_components.thermosmart.coordinator.async_call_later",
            side_effect=lambda hass, delay, callback: (
                callbacks.append(callback) or (lambda: None)
            ),
        ):
            coordinator._handle_trv_setpoint_change(
                "climate.trv", _state(19), _state(25), _cfg()
            )
        callbacks[0](None)
        assert coordinator.target.value == 25
        assert coordinator._manual_change_pending is True
        coordinator.async_request_refresh.assert_called_once()

    def test_rejected_change_corrected_without_cached_write(self):
        """Review M4b: correction does not depend on a cached last write."""
        coordinator = _coordinator()
        assert "climate.trv" not in coordinator._last_written_setpoints
        coordinator._handle_trv_setpoint_change(
            "climate.trv", _state(19), _state(21), _cfg(device=False)
        )
        coordinator.async_request_refresh.assert_called_once()

    @pytest.mark.parametrize("newer", [22.0, 21.0])
    def test_target_write_during_debounce_wins(self, newer):
        """Review M2: a target write after the dial change was observed wins,
        even if it does not change the value (e.g. a schedule re-applying it)."""
        coordinator = _coordinator()
        callbacks = []
        with patch(
            "custom_components.thermosmart.coordinator.async_call_later",
            side_effect=lambda hass, delay, callback: (
                callbacks.append(callback) or (lambda: None)
            ),
        ):
            coordinator._handle_trv_setpoint_change(
                "climate.trv", _state(19), _state(25), _cfg()
            )
        coordinator.set_target(newer, "user")
        callbacks[0](None)
        assert coordinator.target.value == newer
        assert coordinator.target.current.source != "dial"
        assert not coordinator._manual_setpoint_is_pending("climate.trv")
        coordinator.async_request_refresh.assert_called_once()  # correct the TRV

    def test_echo_of_own_write_during_suspension_is_not_a_manual_change(self):
        coordinator = _coordinator()
        coordinator.set_system_state(vacation=True, summer=False)
        coordinator._mark_trv_write("climate.trv", _cfg())
        coordinator._handle_trv_setpoint_change(
            "climate.trv", _state(19), _state(12), _cfg()
        )
        assert coordinator._manual_change_pending is False
        coordinator.async_request_refresh.assert_not_called()

    def test_no_settling_delay_can_be_configured(self):
        coordinator = _coordinator()
        coordinator._mark_trv_write(
            "climate.trv", _cfg(**{CONF_TRV_SETPOINT_SETTLING: 0})
        )
        assert not coordinator._trv_write_is_settling("climate.trv")

    def test_out_of_range_value_is_rejected(self):
        coordinator = _coordinator()
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
        assert coordinator.target.value == TEMP_ECO


class TestTempTriggeredRefresh:
    def _coord(self):
        c = _coordinator()
        c._cycle_ref_temp = 20.0
        return c

    def _tasks(self, c):
        created = []
        c.hass.async_create_task = lambda coro: (created.append(1), coro.close())
        return created

    def test_refresh_when_delta_reaches_threshold(self):
        c = self._coord()
        created = self._tasks(c)
        c._maybe_refresh_on_temp_change(20.5, {"ext_temp_threshold": 0.5})
        assert created

    def test_no_refresh_below_threshold(self):
        c = self._coord()
        created = self._tasks(c)
        c._maybe_refresh_on_temp_change(20.3, {"ext_temp_threshold": 0.5})
        assert not created

    def test_no_refresh_when_inactive(self):
        c = self._coord()
        created = self._tasks(c)
        c._active_control = False
        c._maybe_refresh_on_temp_change(22.0, {})
        assert not created


class TestSlopeIndependentOfCycleCount:
    def test_stale_slope_is_not_recounted(self):
        c = make_coordinator()
        c._slope_window_active = False
        c._slope_win_ema = None
        c._slope_win_consec = 0
        c._indoor_temp_slope = -0.2
        c._slope_sample_fresh = True
        c._check_window_slope(18.0)
        c._slope_sample_fresh = False
        for _ in range(5):
            c._check_window_slope(18.0)
        assert c._slope_win_consec == 1
        assert c._slope_window_active is False
