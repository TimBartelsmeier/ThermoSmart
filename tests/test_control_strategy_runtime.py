"""Runtime coverage for configurable control strategies.

Covers per-device state settings (all domains), per-device valve gating,
legacy migration, per-device valve maintenance, interval wiring and the
recommendation/dispatch contract for zones with mixed strategies.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.thermosmart.const import (
    CONF_DEVICE_SETTINGS,
    CONF_ZONE_CONTROL_MODE,
    CONF_CONTROL_INTERVAL,
    CONF_DEV_CONTROL_MODE,
    CONF_DEV_VALVE_ENTITY,
    CONF_DEV_STATE_SETTINGS,
    CONF_DEV_FEED_EXT_TEMP,
    CONF_DEV_EXT_TEMP_ENTITY,
    CONF_DEV_CALIBRATION_INVERT,
    CONF_DEV_VALVE_MAINTENANCE,
    CONTROL_MODE_DIRECT_VALVE,
    CONTROL_MODE_INHERIT,
    CONTROL_MODE_SETPOINT,
    CONTROL_MODE_TPI_BOOST,
)
from tests.helpers import make_coordinator, make_state, set_hass_states
from tests.test_coordinator_integration import _make_testable
from tests.test_maintenance import _run_maintenance


def _coord():
    coord = make_coordinator()
    coord.hass.services.async_call = AsyncMock(return_value=None)
    return coord


def _cfg_with_rules(*rules, climate="climate.trv"):
    return {
        "climate_entities": [climate],
        CONF_DEVICE_SETTINGS: {climate: {CONF_DEV_STATE_SETTINGS: list(rules)}},
    }


def _calls(coord, domain):
    return [c[0] for c in coord.hass.services.async_call.call_args_list if c[0][0] == domain]


class TestStateSettingDomains:
    async def test_switch_turned_on_only_when_off(self):
        coord = _coord()
        cfg = _cfg_with_rules({"entity_id": "switch.child_lock", "value": "on"})
        set_hass_states(coord, {"switch.child_lock": make_state("on")})
        await coord._async_enforce_device_state_settings("climate.trv", cfg)
        assert _calls(coord, "switch") == []

        coord._state_settings_enforced = set()
        set_hass_states(coord, {"switch.child_lock": make_state("off")})
        await coord._async_enforce_device_state_settings("climate.trv", cfg)
        assert _calls(coord, "switch")[0][1:] == (
            "turn_on", {"entity_id": "switch.child_lock"}
        )

    async def test_switch_turned_off(self):
        coord = _coord()
        cfg = _cfg_with_rules({"entity_id": "switch.window_detection", "value": "off"})
        set_hass_states(coord, {"switch.window_detection": make_state("on")})
        await coord._async_enforce_device_state_settings("climate.trv", cfg)
        assert _calls(coord, "switch")[0][1] == "turn_off"

    async def test_unavailable_entity_is_not_written(self):
        coord = _coord()
        cfg = _cfg_with_rules({"entity_id": "select.src", "value": "external"})
        set_hass_states(coord, {"select.src": make_state("unavailable")})
        await coord._async_enforce_device_state_settings("climate.trv", cfg)
        assert not coord.hass.services.async_call.called

    async def test_rule_applied_once_per_cycle(self):
        coord = _coord()
        cfg = _cfg_with_rules({"entity_id": "select.src", "value": "external"})
        set_hass_states(coord, {"select.src": make_state("internal")})
        await coord._async_enforce_device_state_settings("climate.trv", cfg)
        await coord._async_enforce_device_state_settings("climate.trv", cfg)
        assert len(_calls(coord, "select")) == 1

    async def test_failed_write_is_retried_and_does_not_raise(self):
        coord = _coord()
        coord.hass.services.async_call = AsyncMock(side_effect=Exception("boom"))
        cfg = _cfg_with_rules({"entity_id": "select.src", "value": "external"})
        set_hass_states(coord, {"select.src": make_state("internal")})
        await coord._async_enforce_device_state_settings("climate.trv", cfg)
        await coord._async_enforce_device_state_settings("climate.trv", cfg)
        assert coord.hass.services.async_call.await_count == 2

    async def test_no_rules_means_no_calls(self):
        coord = _coord()
        await coord._async_enforce_device_state_settings(
            "climate.trv", {"climate_entities": ["climate.trv"]}
        )
        assert not coord.hass.services.async_call.called


class TestValveGatingPerDevice:
    def _cfg(self, zone_mode, device_mode):
        return {
            "climate_entities": ["climate.trv"],
            CONF_ZONE_CONTROL_MODE: zone_mode,
            CONF_DEVICE_SETTINGS: {"climate.trv": {
                CONF_DEV_CONTROL_MODE: device_mode,
                CONF_DEV_VALVE_ENTITY: "number.valve",
            }},
        }

    async def _write(self, cfg):
        coord = _coord()
        set_hass_states(coord, {"number.valve": make_state("0")})
        await coord._async_set_valve_percent(cfg, 50.0)
        return _calls(coord, "number")

    async def test_inherit_uses_zone_direct_valve(self):
        calls = await self._write(self._cfg(CONTROL_MODE_DIRECT_VALVE, CONTROL_MODE_INHERIT))
        assert calls and calls[0][2]["value"] == 50

    async def test_device_override_to_tpi_blocks_valve_write(self):
        assert await self._write(
            self._cfg(CONTROL_MODE_DIRECT_VALVE, CONTROL_MODE_TPI_BOOST)) == []

    async def test_zone_setpoint_blocks_valve_write(self):
        assert await self._write(
            self._cfg(CONTROL_MODE_SETPOINT, CONTROL_MODE_INHERIT)) == []


class TestRecommendationControlStrategy:
    def _coord(self, zone_mode, devices):
        coord = _make_testable({
            "climate_entities": list(devices),
            CONF_ZONE_CONTROL_MODE: zone_mode,
            CONF_DEVICE_SETTINGS: devices,
        })
        coord.hass.services.async_call = AsyncMock(return_value=None)
        set_hass_states(coord, {"sensor.test_temp": make_state("15.0")})
        return coord

    async def test_direct_valve_zone_advertises_unboosted_setpoint(self):
        coord = self._coord(CONTROL_MODE_DIRECT_VALVE, {
            "climate.a": {CONF_DEV_CONTROL_MODE: CONTROL_MODE_INHERIT},
        })
        zone = (await coord._async_update_data())["zone"]
        assert zone["tpi_valve_direct"] is True
        assert zone["zone_control_type"] == "direct_valve"
        assert zone["trv_setpoint"] == zone["effective_target"]
        assert coord._boost_active == {}

    async def test_mixed_zone_reports_mixed_and_lists_boosted_devices(self):
        coord = self._coord(CONTROL_MODE_TPI_BOOST, {
            "climate.a": {CONF_DEV_CONTROL_MODE: CONTROL_MODE_DIRECT_VALVE},
            "climate.b": {CONF_DEV_CONTROL_MODE: CONTROL_MODE_INHERIT},
        })
        zone = (await coord._async_update_data())["zone"]
        assert zone["zone_control_type"] == "mixed"
        assert zone["tpi_valve_direct"] is False
        assert zone["tpi_control_entities"] == ["climate.b"]

    async def test_setpoint_zone_never_boosts(self):
        coord = self._coord(CONTROL_MODE_SETPOINT, {
            "climate.a": {CONF_DEV_CONTROL_MODE: CONTROL_MODE_INHERIT},
        })
        zone = (await coord._async_update_data())["zone"]
        assert zone["trv_setpoint"] == zone["effective_target"]


class TestControlInterval:
    def test_default_interval_is_five_minutes(self):
        assert make_coordinator().update_interval == timedelta(minutes=5)

    def test_configured_interval_is_used(self):
        coord = make_coordinator({CONF_CONTROL_INTERVAL: 2})
        assert coord.update_interval == timedelta(minutes=2)

    def test_season_window_covers_72h_for_any_interval(self):
        coord = make_coordinator({CONF_CONTROL_INTERVAL: 2})
        assert coord._outdoor_temp_history.maxlen == 72 * 30


class TestPerDeviceValveMaintenance:
    async def _run(self, cfg):
        coord = make_coordinator()
        coord._is_summer = True
        coord._active_control = True
        return await _run_maintenance(coord, cfg, {"effective_target": 21.0})

    def _cfg(self, enabled, **extra):
        return {
            "climate_entities": ["climate.trv"],
            "vacation_temp": 12.0,
            "comfort_temp": 21.0,
            CONF_DEVICE_SETTINGS: {
                "climate.trv": {CONF_DEV_VALVE_MAINTENANCE: enabled},
            },
            **extra,
        }

    async def test_disabled_device_is_skipped(self):
        assert await self._run(self._cfg(False)) == []

    async def test_enabled_device_is_exercised(self):
        assert len(await self._run(self._cfg(True))) == 2


class TestCalibrationSettings:
    def _setup(self, **settings):
        coord = _coord()
        coord._get_trv_state = lambda _eid: make_state("heat", {"current_temperature": 19.0})
        set_hass_states(coord, {"number.cal": make_state("0")})
        cfg = {
            "climate_entities": ["climate.trv"],
            CONF_DEVICE_SETTINGS: {"climate.trv": settings},
        }
        return coord, cfg

    async def _calibrate(self, coord, cfg):
        await coord._async_calibrate_trvs(cfg, {"current_temp": 21.0})
        return _calls(coord, "number")

    async def test_enabled_device_writes_offset(self):
        coord, cfg = self._setup(calibration_entity="number.cal", calibration_sync=True)
        calls = await self._calibrate(coord, cfg)
        assert calls and calls[0][2]["entity_id"] == "number.cal"

    async def test_sync_off_writes_nothing(self):
        coord, cfg = self._setup(calibration_entity="number.cal", calibration_sync=False)
        assert await self._calibrate(coord, cfg) == []

    async def test_missing_entity_writes_nothing(self):
        coord, cfg = self._setup(calibration_sync=True)
        assert await self._calibrate(coord, cfg) == []

    async def test_invert_flips_sign(self):
        coord, cfg = self._setup(
            calibration_entity="number.cal", calibration_sync=True,
            calibration_invert=True)
        calls = await self._calibrate(coord, cfg)
        assert calls[0][2]["value"] < 0


class TestNoRuntimeAutoDetection:
    async def test_cleared_valve_entity_disables_direct_writes(self):
        coord = _coord()
        cfg = {
            "climate_entities": ["climate.trv"],
            CONF_DEVICE_SETTINGS: {"climate.trv": {
                CONF_DEV_CONTROL_MODE: CONTROL_MODE_DIRECT_VALVE,
                CONF_DEV_VALVE_ENTITY: None,
            }},
        }
        stats = await coord._async_set_valve_percent(cfg, 50.0)
        assert stats.status == "not_attempted"
        assert not coord.hass.services.async_call.called

    async def test_ext_temp_not_written_without_feed_flag(self):
        coord = _coord()
        set_hass_states(coord, {"number.ext": make_state("10")})
        cfg = {
            "climate_entities": ["climate.trv"],
            CONF_DEVICE_SETTINGS: {"climate.trv": {
                CONF_DEV_FEED_EXT_TEMP: False,
                CONF_DEV_EXT_TEMP_ENTITY: "number.ext",
            }},
        }
        await coord._async_write_external_temp(cfg, {"current_temp": 21.0})
        assert not coord.hass.services.async_call.called


class TestPresetDetection:
    def _detect(self, model, manufacturer, entities):
        from custom_components.thermosmart import config_flow
        ent = lambda eid, **kw: MagicMock(entity_id=eid, translation_key=kw.get("tk"), original_name=kw.get("name"))
        reg = MagicMock()
        reg.async_get.return_value = MagicMock(device_id="dev")
        dev = MagicMock(model=model, manufacturer=manufacturer)
        with patch.object(config_flow.er, "async_get", return_value=reg), \
             patch.object(config_flow.er, "async_entries_for_device",
                          return_value=[ent(e) for e in entities]), \
             patch.object(config_flow.dr, "async_get") as dr_get:
            dr_get.return_value.async_get.return_value = dev
            return config_flow._detect_device_settings(MagicMock(), "climate.trv")["settings"]

    def test_trvzb_gets_source_rule_quirks_and_no_calibration_write(self):
        s = self._detect("TRVZB", "SONOFF", [
            "select.trv_temperature_sensor_select", "switch.trv_child_lock",
            "number.trv_local_temperature_calibration", "number.trv_external_temperature_input",
        ])
        rules = {r["entity_id"]: r["value"] for r in s[CONF_DEV_STATE_SETTINGS]}
        assert "switch.trv_child_lock" not in rules
        assert rules["select.trv_temperature_sensor_select"] == "remote_sensor"
        assert s["calibration_sync"] is False
        assert s["calibration_entity"] == "number.trv_local_temperature_calibration"

    def test_generic_device_gets_no_rules_and_sync_follows_entity(self):
        s = self._detect("Unknown", "Nobody", ["switch.x_child_lock"])
        assert s[CONF_DEV_STATE_SETTINGS] == []
        assert s["calibration_sync"] is False
