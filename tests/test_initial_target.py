"""New zones start at Eco; restored preset labels must still match (review M10)."""
from __future__ import annotations

from homeassistant.core import State
from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

from custom_components.thermosmart.target import initial_target, preset_matches
from tests.helpers import make_coordinator
from tests.test_target_restore_units import _entry, _setup


def test_new_zone_starts_at_configured_eco():
    coord = make_coordinator({"eco_temp": 17.5})
    assert coord.target.value == 17.5
    assert coord.target.preset == "eco"


def test_unusable_eco_falls_back_without_preset():
    target = initial_target({"eco_temp": 99})
    assert target.value == 19.0
    assert target.preset is None


def test_preset_matches():
    assert preset_matches({"comfort_temp": 22.0}, "comfort", 22.0)
    assert not preset_matches({"comfort_temp": 22.0}, "comfort", 21.0)
    assert not preset_matches({}, "unknown", 21.0)


async def test_restored_preset_kept_when_value_matches(hass, enable_custom_integrations):
    entry, climate_id = _entry(hass)
    mock_restore_cache_with_extra_data(hass, [(
        State(climate_id, "heat", {}), {"target_c": 21.0, "preset": "comfort"},
    )])
    coord = await _setup(hass, entry)
    assert (coord.target.value, coord.target.preset) == (21.0, "comfort")


async def test_restored_preset_dropped_after_reconfiguration(hass, enable_custom_integrations):
    entry, climate_id = _entry(hass)
    hass.config_entries.async_update_entry(entry, options={"comfort_temp": 22.5})
    mock_restore_cache_with_extra_data(hass, [(
        State(climate_id, "heat", {}), {"target_c": 21.0, "preset": "comfort"},
    )])
    coord = await _setup(hass, entry)
    assert (coord.target.value, coord.target.preset) == (21.0, None)
