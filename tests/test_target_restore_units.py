"""Desired target restore is unit-safe (review M1)."""
from __future__ import annotations

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import IMPERIAL_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry, mock_restore_cache, mock_restore_cache_with_extra_data,
)

from custom_components.thermosmart.const import DOMAIN


def _entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="unit_zone", title="Unit Zone",
        data={"entry_type": "zone", "name": "Unit Zone",
              "climate_entities": ["climate.unit_trv"], "temp_sensors": [],
              "window_sensors": []},
    )
    entry.add_to_hass(hass)
    hass.states.async_set("climate.unit_trv", "heat", {
        "temperature": 68.0, "current_temperature": 68.0, "hvac_modes": ["heat", "off"]})
    climate = er.async_get(hass).async_get_or_create(
        "climate", DOMAIN, f"{entry.entry_id}_climate", suggested_object_id="unit_zone",
        config_entry=entry)
    return entry, climate.entity_id


async def _setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]["coordinator"]


async def test_legacy_fahrenheit_state_is_converted(hass, enable_custom_integrations):
    hass.config.units = IMPERIAL_SYSTEM
    entry, climate_id = _entry(hass)
    mock_restore_cache(hass, [State(climate_id, "heat", {"temperature": 68.0})])
    coord = await _setup(hass, entry)
    assert coord.target.value == 20.0


async def test_canonical_celsius_extra_data_wins(hass, enable_custom_integrations):
    hass.config.units = IMPERIAL_SYSTEM
    entry, climate_id = _entry(hass)
    mock_restore_cache_with_extra_data(hass, [(
        State(climate_id, "heat", {"temperature": 99.0, "preset_mode": "eco"}),
        {"target_c": 19.5, "preset": None},
    )])
    coord = await _setup(hass, entry)
    assert coord.target.value == 19.5
    assert coord.target.preset is None


async def test_extra_data_exports_celsius(hass, enable_custom_integrations):
    hass.config.units = IMPERIAL_SYSTEM
    entry, _ = _entry(hass)
    coord = await _setup(hass, entry)
    coord.target.restore(20.5, None)
    climate = next(
        e for e in hass.data["climate"].entities if e.unique_id == "unit_zone_climate")
    assert climate.extra_restore_state_data.as_dict() == {"target_c": 20.5, "preset": None}


async def test_unrecoverable_value_is_reported(hass, enable_custom_integrations, caplog):
    entry, climate_id = _entry(hass)
    mock_restore_cache(hass, [State(climate_id, "heat", {"temperature": 99.0})])
    coord = await _setup(hass, entry)
    assert coord.target.value == 19.0  # default Eco
    assert "stored target could not be restored" in caplog.text
