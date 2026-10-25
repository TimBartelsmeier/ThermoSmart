"""Control-cycle triggers: one test per row of triggers.TRIGGERS.

Every non-periodic refresh goes through ``coordinator.request_pass``; anything not
in the table must not cause a pass, and bursts coalesce.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.thermosmart import triggers as trig
from custom_components.thermosmart.climate import ThermoSmartClimate
from custom_components.thermosmart.const import (
    CONF_DEVICE_SETTINGS,
    CONF_DEV_ACCEPT_SETPOINT_CHANGES,
    CONF_TRV_SETPOINT_DEBOUNCE,
    CONF_TRV_SETPOINT_SETTLING,
)
from custom_components.thermosmart.datetime import ThermoSmartSchedulingDisabledUntil
from custom_components.thermosmart.learning.clock import FakeClock
from custom_components.thermosmart.select import ThermoSmartModeSelect
from custom_components.thermosmart.switch import (
    ThermoSmartActiveSwitch,
    ThermoSmartLearningSwitch,
    ThermoSmartSchedulingSwitch,
)
from tests.helpers import make_coordinator, make_state, set_hass_states
from tests.test_coordinator_integration import _make_testable

COMPONENT = Path(trig.__file__).parent
_T0 = datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)
_WINDOW = "binary_sensor.window"


def _zone(clock=None, **overrides):
    coord = make_coordinator(overrides or None, clock=clock)
    coord.hass.async_create_task = lambda coro: None
    coord.async_request_refresh = MagicMock()
    coord._pending_triggers = []
    coord._active_control = True
    return coord


def _entry():
    entry = MagicMock()
    entry.entry_id = "zone1"
    entry.data = {"name": "Bad"}
    return entry


def _entity_coord():
    return MagicMock()


class TestTable:
    def test_every_constant_is_in_the_table(self):
        names = {v for k, v in vars(trig).items() if k.startswith("TRIGGER_")}
        assert names == set(trig.TRIGGERS)

    def test_unknown_trigger_is_rejected(self):
        coord = _zone()
        with pytest.raises(ValueError):
            coord.request_pass("whatever")
        coord.async_request_refresh.assert_not_called()

    def test_nothing_else_requests_refreshes(self):
        """request_pass is the only caller of async_request_refresh."""
        offenders = []
        for path in COMPONENT.rglob("*.py"):
            if path.name == "triggers.py":
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"\.async_(request_)?refresh\(", line):
                    if path.name == "coordinator.py" and "def request_pass" not in line \
                            and "self.async_request_refresh()" in line:
                        continue
                    if path.name == "__init__.py" and "async_config_entry_first_refresh" in line:
                        continue
                    offenders.append(f"{path.name}:{n}")
        assert offenders == []

    def test_request_pass_records_trigger_and_requests_one_refresh(self):
        coord = _zone()
        coord.request_pass(trig.TRIGGER_SWITCH)
        assert coord._pending_triggers == [trig.TRIGGER_SWITCH]
        coord.async_request_refresh.assert_called_once()


class TestPeriodicAndStartup:
    async def test_startup_then_periodic(self):
        coord = _make_testable()
        assert coord._pending_triggers == [trig.TRIGGER_STARTUP]
        set_hass_states(coord, {"sensor.test_temp": make_state("20.0")})
        result = await coord._async_update_data()
        assert result["zone"]["pass_triggers"] == [trig.TRIGGER_STARTUP]
        result = await coord._async_update_data()
        assert result["zone"]["pass_triggers"] == [trig.TRIGGER_PERIODIC]

    async def test_bursts_are_reported_once_per_pass(self):
        coord = _make_testable()
        coord._pending_triggers = []
        set_hass_states(coord, {"sensor.test_temp": make_state("20.0")})
        coord.hass.async_create_task = lambda coro: None
        coord.async_request_refresh = MagicMock()
        for _ in range(5):
            coord.request_pass(trig.TRIGGER_SWITCH)
        coord.request_pass(trig.TRIGGER_TARGET_CHANGE)
        result = await coord._async_update_data()
        assert result["zone"]["pass_triggers"] == [trig.TRIGGER_SWITCH, trig.TRIGGER_TARGET_CHANGE]


class TestTargetChange:
    async def test_climate_target_and_preset(self):
        coord = _entity_coord()
        climate = ThermoSmartClimate(coord, _entry())
        climate.async_write_ha_state = MagicMock()
        await climate.async_set_temperature(temperature=21)
        coord.request_pass.assert_called_with(trig.TRIGGER_TARGET_CHANGE)
        coord.request_pass.reset_mock()
        await climate.async_set_preset_mode("eco")
        coord.request_pass.assert_called_once_with(trig.TRIGGER_TARGET_CHANGE)

    async def test_select(self):
        coord = _entity_coord()
        select = ThermoSmartModeSelect(coord, _entry())
        select.async_write_ha_state = MagicMock()
        await select.async_select_option("eco")
        coord.request_pass.assert_called_once_with(trig.TRIGGER_TARGET_CHANGE)


class TestSwitches:
    async def test_active_control_and_hvac_mode(self):
        coord = _entity_coord()
        switch = ThermoSmartActiveSwitch(coord, _entry())
        switch.async_write_ha_state = MagicMock()
        await switch.async_turn_off()
        coord.request_pass.assert_called_once_with(trig.TRIGGER_SWITCH)
        climate = ThermoSmartClimate(coord, _entry())
        climate.async_write_ha_state = MagicMock()
        coord.request_pass.reset_mock()
        await climate.async_turn_off()
        coord.request_pass.assert_called_once_with(trig.TRIGGER_SWITCH)

    async def test_learning(self):
        coord = _entity_coord()
        switch = ThermoSmartLearningSwitch(coord, _entry())
        switch.async_write_ha_state = MagicMock()
        switch._get_engine = lambda: None
        await switch.async_turn_on()
        coord.request_pass.assert_called_once_with(trig.TRIGGER_SWITCH)

    async def test_scheduling_switch_and_pause(self):
        coord = _entity_coord()
        switch = ThermoSmartSchedulingSwitch(coord, _entry())
        switch.async_write_ha_state = MagicMock()
        await switch.async_turn_off()
        coord.request_pass.assert_called_once_with(trig.TRIGGER_SCHEDULING)
        coord.request_pass.reset_mock()
        until = ThermoSmartSchedulingDisabledUntil(coord, _entry())
        until.async_write_ha_state = MagicMock()
        await until.async_set_value(_T0)
        coord.request_pass.assert_called_once_with(trig.TRIGGER_SCHEDULING)


class TestScheduleAndPreheat:
    def test_schedule_event(self):
        coord = _zone()
        occurrence = SimpleNamespace(
            event=SimpleNamespace(key="e1", target=22.0), at=_T0,
        )
        coord._on_schedule_event(occurrence)
        assert coord._pending_triggers == [trig.TRIGGER_SCHEDULE_EVENT]

    def test_preheat_start_timer(self):
        coord = _zone()
        captured = {}

        def fake_track(hass, action, when):
            captured["action"] = action
            return lambda: None

        with patch("custom_components.thermosmart.coordinator.async_track_point_in_utc_time",
                   side_effect=fake_track):
            coord._preheat_timer_at = None
            coord._sync_preheat_timer(SimpleNamespace(state="planned", start=_T0 + timedelta(hours=1)))
        captured["action"](_T0)
        assert coord._pending_triggers == [trig.TRIGGER_PREHEAT_START]


class TestWindow:
    def _wire(self, coord):
        captured = {}

        def fake_track(hass, entity_ids, cb):
            captured[tuple(entity_ids)] = cb
            return lambda: None

        with patch("custom_components.thermosmart.coordinator.async_track_state_change_event",
                   side_effect=fake_track):
            coord.setup_event_listeners()
        return next(cb for ents, cb in captured.items() if _WINDOW in ents)

    @staticmethod
    def _event(old, new):
        e = MagicMock()
        e.data = {"entity_id": _WINDOW, "old_state": MagicMock(state=old),
                  "new_state": MagicMock(state=new)}
        return e

    def test_only_the_delay_timers_trigger_a_pass(self):
        clock = FakeClock(_T0)
        coord = _zone(clock, window_sensors=[_WINDOW], window_open_delay=5,
                      window_close_delay=2, temp_sensors=["sensor.test_temp"])
        set_hass_states(coord, {_WINDOW: make_state("on"), "sensor.test_temp": make_state("19.0")})
        timers = []
        with patch("custom_components.thermosmart.coordinator.async_call_later",
                   side_effect=lambda hass, d, cb: timers.append((d, cb)) or (lambda: None)):
            cb = self._wire(coord)
            cb(self._event("off", "on"))
            assert coord._pending_triggers == []  # state change itself: no pass
            assert timers[0][0] == 300
            timers[0][1](None)
            assert coord._pending_triggers == [trig.TRIGGER_WINDOW]

            coord._pending_triggers = []
            clock.set_utc(_T0 + timedelta(minutes=6))
            cb(self._event("on", "off"))
            assert coord._pending_triggers == []
            assert timers[1][0] == 120
            timers[1][1](None)
            assert coord._pending_triggers == [trig.TRIGGER_WINDOW]


class TestRoomTemperature:
    def test_threshold_crossing_triggers_small_drift_does_not(self):
        coord = _zone()
        coord._cycle_ref_temp = 20.0
        from custom_components.thermosmart.const import CONF_EXT_TEMP_THRESHOLD
        cfg = {CONF_EXT_TEMP_THRESHOLD: 0.5}
        coord._maybe_refresh_on_temp_change(20.3, cfg)
        assert coord._pending_triggers == []
        coord._maybe_refresh_on_temp_change(20.6, cfg)
        assert coord._pending_triggers == [trig.TRIGGER_ROOM_TEMPERATURE]

    def test_suspended_or_observing_zone_does_not_trigger(self):
        from custom_components.thermosmart.const import CONF_EXT_TEMP_THRESHOLD
        cfg = {CONF_EXT_TEMP_THRESHOLD: 0.5}
        coord = _zone()
        coord._cycle_ref_temp = 20.0
        coord._active_control = False
        coord._maybe_refresh_on_temp_change(25.0, cfg)
        coord._active_control = True
        coord._is_summer = True
        coord._maybe_refresh_on_temp_change(25.0, cfg)
        assert coord._pending_triggers == []


class TestPhysicalSetpoint:
    @staticmethod
    def _state(setpoint, state="heat"):
        return SimpleNamespace(state=state, attributes={"temperature": setpoint})

    @staticmethod
    def _cfg(accept=True):
        return {
            CONF_DEVICE_SETTINGS: {"climate.trv": {CONF_DEV_ACCEPT_SETPOINT_CHANGES: accept}},
            CONF_TRV_SETPOINT_DEBOUNCE: 5,
            CONF_TRV_SETPOINT_SETTLING: 5,
        }

    def test_adopted_change_triggers_after_debounce_only(self):
        coord = _zone()
        callbacks = []
        with patch("custom_components.thermosmart.coordinator.async_call_later",
                   side_effect=lambda hass, d, cb: callbacks.append(cb) or (lambda: None)):
            coord._handle_trv_setpoint_change(
                "climate.trv", self._state(19), self._state(20), self._cfg())
        assert coord._pending_triggers == []
        callbacks[0](None)
        assert coord._pending_triggers == [trig.TRIGGER_PHYSICAL_SETPOINT]

    def test_correction_of_rejected_change_triggers(self):
        coord = _zone()
        coord._last_written_setpoints = {"climate.trv": 21.0}
        coord._handle_trv_setpoint_change(
            "climate.trv", self._state(21), self._state(25), self._cfg(accept=False))
        assert coord._pending_triggers == [trig.TRIGGER_PHYSICAL_SETPOINT]

    def test_correction_during_summer_triggers(self):
        coord = _zone()
        coord._is_summer = True
        coord._last_written_setpoints = {"climate.trv": 12.0}
        coord._handle_trv_setpoint_change(
            "climate.trv", self._state(12), self._state(22), self._cfg())
        assert coord._pending_triggers == [trig.TRIGGER_PHYSICAL_SETPOINT]

    def test_trv_attribute_noise_does_not_trigger(self):
        coord = _zone()
        coord._last_written_setpoints = {"climate.trv": 21.0}
        with patch("custom_components.thermosmart.coordinator.async_call_later") as later:
            coord._handle_trv_setpoint_change(
                "climate.trv", self._state(21), self._state(21, state="heat"), self._cfg())
        later.assert_not_called()
        assert coord._pending_triggers == []


class TestSystemState:
    async def test_zone_push_requests_pass_only_on_change(self):
        from tests.test_system import _controller
        controller, zones = _controller()
        await controller.async_set_vacation(True)
        for zone in zones:
            zone.request_pass.assert_called_once_with(trig.TRIGGER_SYSTEM_STATE)


class TestHvacMode:
    @staticmethod
    def _wire(coord):
        callbacks = {}

        def track(hass, entity_ids, cb):
            for entity_id in entity_ids:
                callbacks[entity_id] = cb
            return lambda: None

        with patch(
            "custom_components.thermosmart.coordinator.async_track_state_change_event",
            side_effect=track,
        ):
            coord.setup_event_listeners()
        return callbacks["climate.trv"]

    @staticmethod
    def _state(mode, *, heat_supported=True):
        return SimpleNamespace(state=mode, attributes={
            "temperature": 21.0,
            "hvac_modes": ["auto", "heat", "off"] if heat_supported else ["auto", "off"],
        })

    @pytest.mark.parametrize("mode", ["auto", "heat_cool", "cool", "dry", "fan_only", "off"])
    async def test_mode_only_change_requests_pass_and_watchdog_corrects(self, mode):
        coord = _zone(climate_entities=["climate.trv"])
        listener = self._wire(coord)
        state = self._state(mode)
        coord.hass.states.get = MagicMock(return_value=state)
        coord.hass.services.async_call = AsyncMock()
        original_target = coord.target.current
        listener(SimpleNamespace(data={
            "entity_id": "climate.trv",
            "old_state": self._state("heat"), "new_state": state,
        }))
        assert coord._pending_triggers == [trig.TRIGGER_HVAC_MODE]
        assert coord.target.current == original_target
        assert coord._pending_manual_setpoint is None
        await coord._watchdog_hvac(coord.zone_cfg, {"window_open": False})
        coord.hass.services.async_call.assert_awaited_once_with(
            "climate", "set_hvac_mode",
            {"entity_id": "climate.trv", "hvac_mode": "heat"}, blocking=False,
        )
        # The resulting heat echo must not request another pass.
        listener(SimpleNamespace(data={
            "entity_id": "climate.trv",
            "old_state": state, "new_state": self._state("heat"),
        }))
        assert coord._pending_triggers == [trig.TRIGGER_HVAC_MODE]

    @pytest.mark.parametrize(
        "case", ["observing", "summer", "profile", "unsupported", "noise", "unavailable"]
    )
    def test_ineligible_changes_do_not_request_a_pass(self, case):
        coord = _zone(climate_entities=["climate.trv"])
        listener = self._wire(coord)
        coord._active_control = case != "observing"
        coord._is_summer = case == "summer"
        if case == "profile":
            coord._device_profiles["climate.trv"] = SimpleNamespace(hvac_watchdog=False)
        new = self._state(
            "unavailable" if case == "unavailable" else "auto",
            heat_supported=case != "unsupported",
        )
        listener(SimpleNamespace(data={
            "entity_id": "climate.trv",
            "old_state": self._state("auto" if case == "noise" else "heat"),
            "new_state": new,
        }))
        assert coord._pending_triggers == []

    async def test_window_suspension_still_prevents_watchdog_write(self):
        coord = _zone(climate_entities=["climate.trv"])
        coord.hass.states.get = MagicMock(return_value=self._state("off"))
        coord.hass.services.async_call = AsyncMock()
        await coord._watchdog_hvac(coord.zone_cfg, {"window_open": True})
        coord.hass.services.async_call.assert_not_called()
