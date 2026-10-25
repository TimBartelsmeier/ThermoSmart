"""Scheduling entities: switch, pause datetime and next-event sensor."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from custom_components.thermosmart.datetime import ThermoSmartSchedulingDisabledUntil
from custom_components.thermosmart.schedule import ScheduleOccurrence, parse_events
from custom_components.thermosmart.sensor import ThermoSmartNextEventSensor
from custom_components.thermosmart.switch import ThermoSmartSchedulingSwitch

BERLIN = ZoneInfo("Europe/Berlin")


def _entry():
    entry = MagicMock()
    entry.entry_id = "zone1"
    entry.data = {"name": "Bad"}
    return entry


def _coord():
    coord = MagicMock()
    return coord


async def _added(entity, last_state):
    with patch("homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass",
               new=AsyncMock()), \
         patch.object(entity, "async_get_last_state", new=AsyncMock(return_value=last_state)):
        await entity.async_added_to_hass()


class TestSwitch:
    async def test_defaults_to_on_without_history(self):
        coord = _coord()
        switch = ThermoSmartSchedulingSwitch(coord, _entry())
        await _added(switch, None)
        assert switch.is_on is True
        coord.set_scheduling_enabled.assert_called_once_with(True)

    async def test_restores_off(self):
        coord = _coord()
        switch = ThermoSmartSchedulingSwitch(coord, _entry())
        await _added(switch, SimpleNamespace(state="off"))
        assert switch.is_on is False
        coord.set_scheduling_enabled.assert_called_once_with(False)

    async def test_unavailable_history_keeps_default_on(self):
        switch = ThermoSmartSchedulingSwitch(_coord(), _entry())
        await _added(switch, SimpleNamespace(state="unavailable"))
        assert switch.is_on is True

    async def test_turn_off_and_on_update_coordinator(self):
        coord = _coord()
        switch = ThermoSmartSchedulingSwitch(coord, _entry())
        switch.async_write_ha_state = MagicMock()
        await switch.async_turn_off()
        coord.set_scheduling_enabled.assert_called_with(False)
        await switch.async_turn_on()
        coord.set_scheduling_enabled.assert_called_with(True)
        assert coord.request_pass.call_count == 2


class TestDisabledUntil:
    async def test_restores_value_as_local_time(self):
        coord = _coord()
        entity = ThermoSmartSchedulingDisabledUntil(coord, _entry())
        await _added(entity, SimpleNamespace(state="2026-05-06T14:00:00+00:00"))
        assert entity.native_value == datetime(2026, 5, 6, 14, 0, tzinfo=timezone.utc)
        coord.set_scheduling_disabled_until.assert_called_once_with(entity.native_value)

    async def test_no_history_means_not_paused(self):
        coord = _coord()
        entity = ThermoSmartSchedulingDisabledUntil(coord, _entry())
        await _added(entity, None)
        assert entity.native_value is None
        coord.set_scheduling_disabled_until.assert_called_once_with(None)

    async def test_garbage_history_means_not_paused(self):
        entity = ThermoSmartSchedulingDisabledUntil(_coord(), _entry())
        await _added(entity, SimpleNamespace(state="not a date"))
        assert entity.native_value is None

    async def test_set_value_updates_coordinator(self):
        coord = _coord()
        entity = ThermoSmartSchedulingDisabledUntil(coord, _entry())
        entity.async_write_ha_state = MagicMock()
        value = datetime(2026, 5, 7, 8, 0, tzinfo=BERLIN)
        await entity.async_set_value(value)
        assert entity.native_value == value
        coord.set_scheduling_disabled_until.assert_called_once()
        coord.request_pass.assert_called_once()


class TestNextEventSensor:
    def _sensor(self, occurrence, **coord_attrs):
        coord = MagicMock()
        coord.scheduler.next_event = occurrence
        coord.scheduling_active = coord_attrs.get("active", True)
        coord.schedule_error = coord_attrs.get("error")
        coord.zone_cfg = {"night_temp": 17.0}
        return ThermoSmartNextEventSensor(coord, _entry())

    def _occurrence(self, target, preheat=False):
        (event,) = parse_events([
            {"days": ["wed"], "time": "22:00", "target": target, "preheat": preheat}])
        return ScheduleOccurrence(event, datetime(2026, 5, 6, 22, 0, tzinfo=BERLIN), 90)

    def test_value_and_attributes_for_preset_event(self):
        sensor = self._sensor(self._occurrence("night", preheat=True))
        assert sensor.native_value == datetime(2026, 5, 6, 22, 0, tzinfo=BERLIN)
        assert sensor.extra_state_attributes == {
            "scheduling_active": True, "schedule_error": None, "preset": "night",
            "target_temperature": 17.0, "minutes_until": 90, "preheat": True,
        }

    def test_numeric_event_has_no_preset(self):
        sensor = self._sensor(self._occurrence(22))
        assert sensor.extra_state_attributes["preset"] is None
        assert sensor.extra_state_attributes["target_temperature"] == 22.0

    def test_no_events_gives_unknown_and_error_is_visible(self):
        sensor = self._sensor(None, active=False, error="bad yaml")
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {
            "scheduling_active": False, "schedule_error": "bad yaml"}


class TestPreheatStartSensor:
    EVENT_AT = datetime(2026, 5, 6, 22, 0, tzinfo=BERLIN)

    def _sensor(self, plan, zone=None):
        from custom_components.thermosmart.sensor import ThermoSmartPreheatStartSensor

        coord = MagicMock()
        coord.preheat_plan = plan
        coord.data = {"zone": {"preheat_status": "deterministic_baseline", **(zone or {})}}
        return ThermoSmartPreheatStartSensor(coord, _entry())

    def _plan(self, state, reason=None):
        from custom_components.thermosmart.preheat import PreheatPlan

        return PreheatPlan(state, reason, self.EVENT_AT - timedelta(minutes=85), 85.0,
                           self.EVENT_AT, 21.0)

    def test_planned(self):
        sensor = self._sensor(self._plan("planned"))
        assert sensor.native_value == self.EVENT_AT - timedelta(minutes=85)
        assert sensor.extra_state_attributes == {
            "state": "planned", "reason": None, "event_at": self.EVENT_AT,
            "event_target_temperature": 21.0, "duration_minutes": 85.0,
            "duration_source": "deterministic_baseline",
        }

    def test_active(self):
        assert self._sensor(self._plan("active")).extra_state_attributes["state"] == "active"

    def test_active_but_suspended_reports_the_suspension(self):
        attrs = self._sensor(
            self._plan("active"), {"suspension": "window_open"}).extra_state_attributes
        assert (attrs["state"], attrs["reason"]) == ("suspended", "window_open")

    def test_no_plan_has_no_value_but_a_reason(self):
        from custom_components.thermosmart.preheat import PreheatPlan

        sensor = self._sensor(PreheatPlan("none", "target_not_higher"))
        assert sensor.native_value is None
        assert sensor.extra_state_attributes["reason"] == "target_not_higher"

    def test_before_first_cycle(self):
        sensor = self._sensor(None)
        assert sensor.native_value is None
        assert sensor.extra_state_attributes["reason"] == "no_event"
