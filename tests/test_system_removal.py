"""Removing the System entry releases its holds; reloading does not (review H7)."""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermosmart.const import DOMAIN
from custom_components.thermosmart.system import SYSTEM_KEY


async def _setup(hass):
    system = MockConfigEntry(domain=DOMAIN, entry_id="sys", data={"entry_type": "system"})
    zone = MockConfigEntry(
        domain=DOMAIN, entry_id="zone",
        data={"entry_type": "zone", "name": "Zone", "climate_entities": ["climate.trv"],
              "temp_sensors": [], "window_sensors": []},
    )
    hass.states.async_set("climate.trv", "heat", {
        "temperature": 20.0, "current_temperature": 19.0, "hvac_modes": ["heat", "off"]})
    for entry in (system, zone):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coord = hass.data[DOMAIN][zone.entry_id]["coordinator"]
    controller = hass.data[DOMAIN][SYSTEM_KEY]
    controller.force_summer = True
    await controller.async_set_vacation(True)
    assert coord.suspended
    return system, coord


async def test_removing_system_entry_releases_holds(hass, enable_custom_integrations):
    system, coord = await _setup(hass)
    coord._pending_triggers.clear()
    assert await hass.config_entries.async_remove(system.entry_id)
    await hass.async_block_till_done()
    assert not coord._vacation_mode and not coord._is_summer
    assert not coord.suspended


async def test_reloading_system_entry_keeps_holds(hass, enable_custom_integrations):
    system, coord = await _setup(hass)
    assert await hass.config_entries.async_unload(system.entry_id)
    await hass.async_block_till_done()
    assert coord._vacation_mode and coord._is_summer
