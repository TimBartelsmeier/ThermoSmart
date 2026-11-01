"""A System entry added after the zones reaches the existing zones (review C2)."""
from __future__ import annotations

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermosmart.const import DOMAIN
from custom_components.thermosmart.system import SYSTEM_KEY


async def test_system_added_after_zone_controls_that_zone(hass, enable_custom_integrations):
    zone = MockConfigEntry(
        domain=DOMAIN, entry_id="zone",
        data={"entry_type": "zone", "name": "Zone", "climate_entities": ["climate.trv"],
              "temp_sensors": [], "window_sensors": []},
    )
    hass.states.async_set("climate.trv", "heat", {
        "temperature": 20.0, "current_temperature": 19.0, "hvac_modes": ["heat", "off"]})
    zone.add_to_hass(hass)
    assert await hass.config_entries.async_setup(zone.entry_id)
    await hass.async_block_till_done()
    coord = hass.data[DOMAIN][zone.entry_id]["coordinator"]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "add_system"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    controller = hass.data[DOMAIN][SYSTEM_KEY]
    await controller.async_set_vacation(True)
    assert coord._vacation_mode
    assert coord.suspended
