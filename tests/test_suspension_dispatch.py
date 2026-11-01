"""Suspensions share the normal dispatch path; an open window always wins."""
from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.thermosmart.const import TEMP_FROST_PROTECTION
from tests.helpers import make_coordinator, make_state, set_hass_states
from tests.helpers_ha_runtime import make_recording_coordinator


def _valve_calls(coord):
    return [call for call in coord.control_calls if call[0] == "set_valve_percent"]


async def test_summer_drives_direct_valve_through_tpi_towards_frost_protection():
    coord = make_recording_coordinator({"zone_control_mode": "direct_valve"}, indoor="8.0")
    coord._active_control = True
    coord._is_summer = True

    result = await coord._async_update_data()

    assert result["zone"]["suspension"] == "summer"
    assert result["zone"]["effective_target"] == TEMP_FROST_PROTECTION
    assert result["zone"]["tpi_duty_cycle"] > 0
    assert _valve_calls(coord) == [("set_valve_percent", result["zone"]["tpi_duty_cycle"])]


async def test_warm_summer_closes_direct_valve():
    coord = make_recording_coordinator({"zone_control_mode": "direct_valve"}, indoor="22.0")
    coord._active_control = True
    coord._is_summer = True

    await coord._async_update_data()

    assert _valve_calls(coord) == [("set_valve_percent", 0.0)]


async def test_open_window_overrides_vacation_and_summer_for_valves():
    for summer, vacation in ((False, True), (True, False), (True, True)):
        coord = make_recording_coordinator(
            {"zone_control_mode": "direct_valve", "vacation_temp": 15.0}, indoor="10.0"
        )
        coord._active_control = True
        coord.set_system_state(vacation=vacation, summer=summer)
        coord._check_window_open = lambda cfg, current_temp=None: True

        result = await coord._async_update_data()

        assert result["zone"]["suspension"] == "window_open"
        assert result["zone"]["effective_target"] is None
        assert _valve_calls(coord) == [("set_valve_percent", 0.0)]


async def test_summer_target_respects_observe_only_profile():
    from custom_components.thermosmart.device_profiles.profiles import TADO

    coord = make_coordinator({"climate_entities": ["climate.tado"]})
    coord.hass.services.async_call = AsyncMock()
    coord._device_profiles["climate.tado"] = TADO
    set_hass_states(coord, {"climate.tado": make_state("heat", {"temperature": 20.0})})

    await coord._apply_temperature(
        coord.zone_cfg,
        {"effective_target": TEMP_FROST_PROTECTION, "trv_setpoint": TEMP_FROST_PROTECTION},
    )

    coord.hass.services.async_call.assert_not_awaited()


async def test_summer_target_restores_trv_switched_off_for_window():
    coord = make_coordinator({"climate_entities": ["climate.trv"]})
    coord.hass.services.async_call = AsyncMock()
    coord._window_off_entities.add("climate.trv")
    set_hass_states(coord, {"climate.trv": make_state(
        "off", {"temperature": 5.0, "hvac_modes": ["heat", "off"]},
    )})

    await coord._apply_temperature(
        coord.zone_cfg,
        {"effective_target": TEMP_FROST_PROTECTION, "trv_setpoint": TEMP_FROST_PROTECTION},
    )

    calls = [call.args[:2] for call in coord.hass.services.async_call.await_args_list]
    assert ("climate", "set_hvac_mode") in calls
    assert "climate.trv" not in coord._window_off_entities


async def test_dial_during_vacation_is_held_then_applied_after():
    """Review M3: a dial change during vacation becomes the desired target but
    the TRVs keep the vacation target until vacation ends."""
    from types import SimpleNamespace
    from unittest.mock import patch

    coord = make_recording_coordinator({"vacation_temp": 15.0}, indoor="14.0")
    coord._active_control = True
    coord.set_system_state(vacation=True, summer=False)
    callbacks = []
    st = lambda v: SimpleNamespace(state="heat", attributes={"temperature": v})
    with patch("custom_components.thermosmart.coordinator.async_call_later",
               side_effect=lambda hass, d, cb: callbacks.append(cb) or (lambda: None)):
        coord._handle_trv_setpoint_change("climate.test_trv", st(15), st(23), coord.zone_cfg)
    callbacks[0](None)

    held = (await coord._async_update_data())["zone"]
    assert held["desired_target"] == 23.0
    assert held["effective_target"] == 15.0
    applied = [c for c in coord.control_calls if c[0] == "apply_temperature"]
    assert applied and applied[-1][2] == 15.0

    coord.set_system_state(vacation=False, summer=False)
    after = (await coord._async_update_data())["zone"]
    assert after["effective_target"] == 23.0
