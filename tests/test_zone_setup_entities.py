"""Full integration setup: every entity of a new zone must write a valid state."""
from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermosmart.const import DOMAIN


async def _setup_zone(hass, extra=None):
    hass.states.async_set(
        "climate.test_trv", "heat",
        {"current_temperature": 19.0, "temperature": 20.0, "hvac_modes": ["heat", "off"],
         "min_temp": 5, "max_temp": 30, "target_temp_step": 0.5},
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"entry_type": "zone", "name": "Büro", "climate_entities": ["climate.test_trv"],
              "temp_sensors": [], "humidity_sensors": [], "window_sensors": [],
              "comfort_temp": 21.0, "night_temp": 18.0, "away_temp": 17.0,
              "vacation_temp": 12.0, "eco_temp": 18.0, **(extra or {})},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_all_zone_entities_have_a_state(hass, enable_custom_integrations):
    entry = await _setup_zone(hass)
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert any(e.domain == "climate" for e in entities)
    broken = []
    for ent in entities:
        if ent.disabled_by:
            continue
        state = hass.states.get(ent.entity_id)
        if state is None or state.state == STATE_UNAVAILABLE:
            broken.append(ent.entity_id)
    assert not broken, broken


async def test_climate_exposes_desired_target_source(hass, enable_custom_integrations):
    entry = await _setup_zone(hass)
    registry = er.async_get(hass)
    climate = next(
        e for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.domain == "climate"
    )
    assert hass.states.get(climate.entity_id).attributes["desired_target_source"]
