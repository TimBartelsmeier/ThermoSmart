"""System-wide state: vacation, forced and automatically detected summer."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.thermosmart.const import DOMAIN
from custom_components.thermosmart.switch import (
    ThermoSmartAutoSummerSwitch,
    ThermoSmartForceSummerSwitch,
    ThermoSmartGlobalVacationSwitch,
)
from custom_components.thermosmart.system import (
    SYSTEM_KEY,
    SystemController,
    iter_zone_coordinators,
)

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _zone_coordinator():
    coord = MagicMock()
    state = {"vacation": False, "summer": False}

    def set_state(*, vacation, summer):
        changed = (vacation, summer) != (state["vacation"], state["summer"])
        state.update(vacation=vacation, summer=summer)
        return changed

    coord.set_system_state.side_effect = set_state
    coord.request_pass = MagicMock()
    return coord


def _controller(*, options=None, temperature=None, zones=2):
    hass = MagicMock()
    coords = [_zone_coordinator() for _ in range(zones)]
    hass.data = {DOMAIN: {f"zone{i}": {"coordinator": c} for i, c in enumerate(coords)}}
    hass.data[DOMAIN]["sys"] = {"type": "system"}
    entry = SimpleNamespace(
        entry_id="sys", data={"entry_type": "system"}, options=options or {},
    )
    controller = SystemController(hass, entry)
    controller.outdoor = SimpleNamespace(
        read=lambda: {"temperature": controller._test_temp}
    )
    controller._test_temp = temperature
    return controller, coords


async def _sample_hours(controller, temperature, hours, step_h=1, start=T0):
    controller._test_temp = temperature
    for i in range(0, int(hours / step_h) + 1):
        await controller.async_sample(start + timedelta(hours=i * step_h))


class TestIterZoneCoordinators:
    def test_skips_system_entry_and_non_zone_keys(self):
        controller, coords = _controller()
        controller.hass.data[DOMAIN][SYSTEM_KEY] = controller
        controller.hass.data[DOMAIN]["learning_engine"] = object()
        assert list(iter_zone_coordinators(controller.hass)) == coords


class TestSummerPrecedence:
    async def test_force_wins_even_with_auto_off(self):
        controller, _ = _controller()
        controller.auto_summer = False
        await controller.async_set_force_summer(True)
        assert controller.summer_active and controller.summer_source == "forced"

    async def test_auto_off_ignores_detected_summer(self):
        controller, _ = _controller(options={"outdoor_temp_sensor": "sensor.out"})
        await _sample_hours(controller, 25.0, 30)
        assert controller.detected_summer is True
        await controller.async_set_auto_summer(False)
        assert controller.summer_active is False

    async def test_auto_on_uses_detected_summer(self):
        controller, _ = _controller(options={"outdoor_temp_sensor": "sensor.out"})
        await _sample_hours(controller, 25.0, 30)
        assert controller.summer_active and controller.summer_source == "detected"

    async def test_force_off_does_not_cancel_detected_summer(self):
        controller, _ = _controller(options={"outdoor_temp_sensor": "sensor.out"})
        await _sample_hours(controller, 25.0, 30)
        await controller.async_set_force_summer(True)
        await controller.async_set_force_summer(False)
        assert controller.summer_active is True


class TestAutomaticDetection:
    async def test_never_enables_without_outdoor_source(self):
        controller, _ = _controller()
        assert controller.has_outdoor_source is False
        with patch("custom_components.thermosmart.system.async_track_time_interval") as track:
            await controller.async_start()
        track.assert_not_called()
        assert controller.summer_active is False

    async def test_never_enables_when_source_has_no_data(self):
        controller, _ = _controller(options={"outdoor_temp_sensor": "sensor.out"})
        await _sample_hours(controller, None, 100)
        assert controller.summer_active is False

    async def test_summer_starts_and_ends_with_hysteresis(self):
        controller, _ = _controller(options={"outdoor_temp_sensor": "sensor.out"})
        await _sample_hours(controller, 25.0, 30)
        assert controller.summer_active is True
        await _sample_hours(controller, 16.5, 30, start=T0 + timedelta(hours=31))
        assert controller.summer_active is True  # average between thresholds
        await _sample_hours(controller, 5.0, 100, start=T0 + timedelta(hours=62))
        assert controller.summer_active is False

    async def test_custom_thresholds_from_options(self):
        controller, _ = _controller(options={
            "outdoor_temp_sensor": "sensor.out",
            "summer_on_threshold": 22.0, "summer_off_threshold": 20.0,
        })
        await _sample_hours(controller, 21.0, 30)
        assert controller.summer_active is False
        await _sample_hours(controller, 24.0, 100, start=T0 + timedelta(hours=31))
        assert controller.summer_active is True

    async def test_detected_summer_is_reset_after_72h_without_data(self):
        controller, zones = _controller(options={"outdoor_temp_sensor": "sensor.out"})
        await _sample_hours(controller, 25.0, 30)
        assert controller.summer_active is True
        await _sample_hours(controller, None, 80, start=T0 + timedelta(hours=31))
        assert controller.summer_active is False

    async def test_detected_summer_survives_short_data_gap(self):
        controller, _ = _controller(options={"outdoor_temp_sensor": "sensor.out"})
        await _sample_hours(controller, 25.0, 30)
        await _sample_hours(controller, None, 24, start=T0 + timedelta(hours=31))
        assert controller.summer_active is True

    async def test_restored_detected_state_is_kept_until_enough_data(self):
        controller, _ = _controller(options={"outdoor_temp_sensor": "sensor.out"})
        controller.restore_detected_summer(True)
        await _sample_hours(controller, 10.0, 5)
        assert controller.summer_active is True


class TestDistribution:
    async def test_vacation_reaches_all_zones_and_refreshes_on_change(self):
        controller, zones = _controller()
        await controller.async_set_vacation(True)
        for zone in zones:
            zone.set_system_state.assert_called_with(vacation=True, summer=False)
            zone.request_pass.assert_called_once()
        await controller.async_set_vacation(True)
        for zone in zones:
            zone.request_pass.assert_called_once()  # unchanged: no refresh

    async def test_detected_summer_is_pushed_when_it_flips(self):
        controller, zones = _controller(options={"outdoor_temp_sensor": "sensor.out"})
        await _sample_hours(controller, 25.0, 30)
        for zone in zones:
            zone.set_system_state.assert_called_with(vacation=False, summer=True)

    def test_late_loading_zone_pulls_current_state(self):
        controller, _ = _controller()
        controller.vacation = True
        late = _zone_coordinator()
        assert controller.apply_to(late) is True
        late.set_system_state.assert_called_once_with(vacation=True, summer=False)


class TestSwitches:
    def _entity(self, cls, controller, last_state):
        entity = cls(controller)
        entity.hass = controller.hass
        entity.async_write_ha_state = MagicMock()
        entity.async_get_last_state = AsyncMock(return_value=last_state)
        return entity

    async def _added(self, entity):
        with patch("homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass",
                   new=AsyncMock()):
            await entity.async_added_to_hass()

    @pytest.mark.parametrize("cls, attr, default", [
        (ThermoSmartGlobalVacationSwitch, "vacation", False),
        (ThermoSmartForceSummerSwitch, "force_summer", False),
        (ThermoSmartAutoSummerSwitch, "auto_summer", True),
    ])
    async def test_defaults_without_history(self, cls, attr, default):
        controller, _ = _controller()
        await self._added(self._entity(cls, controller, None))
        assert getattr(controller, attr) is default

    @pytest.mark.parametrize("cls, attr", [
        (ThermoSmartGlobalVacationSwitch, "vacation"),
        (ThermoSmartForceSummerSwitch, "force_summer"),
    ])
    async def test_restores_on(self, cls, attr):
        controller, zones = _controller()
        await self._added(self._entity(cls, controller, SimpleNamespace(state="on", attributes={})))
        assert getattr(controller, attr) is True
        assert zones[0].set_system_state.called

    async def test_auto_summer_restores_off_and_detected_state(self):
        controller, _ = _controller()
        last = SimpleNamespace(state="off", attributes={"summer_detected": True})
        await self._added(self._entity(ThermoSmartAutoSummerSwitch, controller, last))
        assert controller.auto_summer is False
        assert controller.detected_summer is True

    async def test_turn_on_and_off(self):
        controller, _ = _controller()
        entity = self._entity(ThermoSmartForceSummerSwitch, controller, None)
        await entity.async_turn_on()
        assert entity.is_on is True and controller.summer_active is True
        await entity.async_turn_off()
        assert entity.is_on is False

    def test_auto_switch_exposes_transparent_attributes(self):
        controller, _ = _controller()
        attrs = self._entity(ThermoSmartAutoSummerSwitch, controller, None).extra_state_attributes
        assert attrs["summer_detected"] is False
        assert attrs["outdoor_source_configured"] is False
        assert attrs["summer_on_threshold"] == 18.0
        assert attrs["summer_off_threshold"] == 15.0
        assert attrs["average_outdoor_temperature"] is None
