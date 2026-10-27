"""Active control stays synchronized across HA entity startup and commands."""
from datetime import timedelta
import logging
from unittest.mock import MagicMock

import pytest

from homeassistant.components.climate import HVACMode
from homeassistant.core import State
from homeassistant.helpers.entity_component import EntityComponent
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

from custom_components.thermosmart.climate import ThermoSmartClimate
from custom_components.thermosmart.const import DOMAIN
from custom_components.thermosmart.coordinator import ThermoSmartCoordinator
from custom_components.thermosmart.switch import ThermoSmartActiveSwitch
from custom_components.thermosmart.triggers import TRIGGER_SWITCH
from tests.helpers import make_zone_config

_SWITCH = "switch.test_active_control"
_CLIMATE = "climate.test_zone"


@pytest.fixture
def zone(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=make_zone_config())
    coordinator = ThermoSmartCoordinator(hass, entry, MagicMock(), MagicMock())
    coordinator.data = {"zone": {}}
    coordinator.request_pass = MagicMock()
    switch = ThermoSmartActiveSwitch(coordinator, entry)
    switch.entity_id = _SWITCH
    climate = ThermoSmartClimate(coordinator, entry)
    climate.entity_id = _CLIMATE
    return coordinator, switch, climate


async def _add(hass, entity):
    component = EntityComponent(
        logging.getLogger(__name__), entity.entity_id.split(".")[0], hass,
        timedelta(seconds=30),
    )
    await component.async_add_entities([entity])
    assert hass.states.get(entity.entity_id) is not None


def _assert_states(hass, coordinator, switch, active):
    assert coordinator._active_control is active
    assert coordinator._active_control_initialized is True
    assert switch.is_on is active
    state = hass.states.get(_SWITCH)
    assert state.state == ("on" if active else "off")
    assert state.attributes["mode"] == ("active_control" if active else "observation")
    climate = hass.states.get(_CLIMATE)
    assert climate.state == ("heat" if active else "off")
    assert climate.attributes["observation_mode"] is not active


@pytest.mark.parametrize("switch_first", [False, True])
@pytest.mark.parametrize("saved", [None, "on", "off", "unavailable"])
async def test_startup_restoration_in_either_entity_order(hass, zone, saved, switch_first):
    coordinator, switch, climate = zone
    if saved is not None:
        mock_restore_cache(hass, [State(_SWITCH, saved)])
    for entity in ((switch, climate) if switch_first else (climate, switch)):
        await _add(hass, entity)
    _assert_states(hass, coordinator, switch, saved == "on")
    coordinator.request_pass.assert_not_called()
    await switch.async_remove()
    await climate.async_remove()


@pytest.mark.parametrize("source", ["switch", "climate", "coordinator"])
async def test_commands_publish_both_states_without_a_control_pass(hass, zone, source):
    coordinator, switch, climate = zone
    await _add(hass, climate)
    await _add(hass, switch)
    for active in (True, False, True, True):
        coordinator.request_pass.reset_mock()
        if source == "switch":
            if active:
                await switch.async_turn_on()
            else:
                await switch.async_turn_off()
        elif source == "climate":
            await climate.async_set_hvac_mode(HVACMode.HEAT if active else HVACMode.OFF)
        else:
            coordinator.set_active_control(active)
        # Assert immediately: no refresh or event-loop turn is needed to publish.
        _assert_states(hass, coordinator, switch, active)
        if source == "coordinator":
            coordinator.request_pass.assert_not_called()
        else:
            coordinator.request_pass.assert_called_once_with(TRIGGER_SWITCH)
    await switch.async_remove()
    await climate.async_remove()


async def test_unload_removes_listeners_and_readd_does_not_duplicate_them(hass, zone):
    coordinator, switch, climate = zone
    for entity in (climate, switch):
        await _add(hass, entity)
    assert len(coordinator._listeners) == 2
    await switch.async_remove()
    await climate.async_remove()
    assert not coordinator._listeners
    coordinator.set_active_control(True)
    assert hass.states.get(_SWITCH).state == "unavailable"
    assert hass.states.get(_CLIMATE).state == "unavailable"

    replacement_switch = ThermoSmartActiveSwitch(coordinator, coordinator.entry)
    replacement_switch.entity_id = _SWITCH
    replacement_climate = ThermoSmartClimate(coordinator, coordinator.entry)
    replacement_climate.entity_id = _CLIMATE
    for entity in (replacement_climate, replacement_switch):
        await _add(hass, entity)
    assert len(coordinator._listeners) == 2
    _assert_states(hass, coordinator, replacement_switch, False)
    await replacement_switch.async_remove()
    await replacement_climate.async_remove()
    assert not coordinator._listeners
