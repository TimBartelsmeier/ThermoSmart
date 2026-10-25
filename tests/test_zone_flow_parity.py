"""Setup and options flow share one implementation of the zone steps."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermosmart import config_flow
from custom_components.thermosmart.const import DOMAIN
from tests.test_config_flow import (
    _CONTROL,
    _DEVICES,
    _DEVICE_SETTINGS,
    _PATCH,
    _SCHEDULE,
    _WEATHER_NONE,
)


def _step_methods(cls) -> set[str]:
    return {n for n in dir(cls) if n.startswith("async_step_")}


class TestSharedImplementation:
    def test_every_zone_step_is_defined_once_in_the_shared_mixin(self):
        for step in config_flow.ZONE_STEPS:
            name = f"async_step_{step}"
            assert name not in vars(config_flow.ThermoSmartConfigFlow)
            assert name not in vars(config_flow.ThermoSmartOptionsFlow)

    def test_both_flows_offer_the_same_zone_steps(self):
        shared = {f"async_step_{s}" for s in config_flow.ZONE_STEPS}
        assert shared <= _step_methods(config_flow.ThermoSmartConfigFlow)
        assert shared <= _step_methods(config_flow.ThermoSmartOptionsFlow)

    def test_strings_of_shared_steps_are_identical(self):
        import json
        from pathlib import Path

        strings = json.loads(
            (Path(config_flow.__file__).parent / "strings.json").read_text("utf-8")
        )
        setup, options = strings["config"]["step"], strings["options"]["step"]
        for step in (*config_flow.ZONE_STEPS, "state_menu", "state_add_device",
                     "state_add_any", "state_value", "state_remove"):
            assert setup[step] == options[step], step
        assert strings["config"]["error"] == strings["options"]["error"]


async def _setup_flow_data(hass) -> dict:
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "add_zone"})
    result = await flow.async_configure(result["flow_id"], _DEVICES)
    result = await flow.async_configure(result["flow_id"], _CONTROL)
    result = await flow.async_configure(result["flow_id"], _DEVICE_SETTINGS)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "state_done"})
    result = await flow.async_configure(result["flow_id"], _SCHEDULE)
    result = await flow.async_configure(result["flow_id"], _WEATHER_NONE)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    return dict(result["data"])


async def _options_flow_data(hass) -> dict:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"entry_type": "zone", "name": _DEVICES["name"],
              "climate_entities": _DEVICES["climate_entities"]},
        options={},
    )
    entry.add_to_hass(hass)
    options = hass.config_entries.options

    async def section(name, *payloads):
        hub = await options.async_init(entry.entry_id)
        result = await options.async_configure(hub["flow_id"], {"next_step_id": name})
        for payload in payloads:
            result = await options.async_configure(result["flow_id"], payload)
        assert result["type"] == FlowResultType.CREATE_ENTRY, (name, result)
        hass.config_entries.async_update_entry(entry, options=dict(result["data"]))

    await section("devices", _DEVICES)
    await section("control_options", _CONTROL)
    await section("device_settings", _DEVICE_SETTINGS, {"next_step_id": "state_done"})
    await section("temperatures", _SCHEDULE)
    await section("outdoor", _WEATHER_NONE)
    merged = {**entry.data, **entry.options}
    merged.pop("entry_type")
    return merged


class TestSameInputSameData:
    async def test_setup_and_options_produce_identical_zone_data(
        self, hass, enable_custom_integrations
    ):
        with patch(_PATCH, return_value=True):
            from_setup = await _setup_flow_data(hass)
        for entry in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_remove(entry.entry_id)
        from_options = await _options_flow_data(hass)
        assert from_options == from_setup


class TestNameWarnings:
    async def test_duplicate_schedule_key_warns_once_then_accepts(
        self, hass, enable_custom_integrations
    ):
        MockConfigEntry(
            domain=DOMAIN, data={"entry_type": "zone", "name": "Wohn Zimmer",
                                 "climate_entities": ["climate.a"]},
        ).add_to_hass(hass)
        flow = hass.config_entries.flow
        result = await flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert result["step_id"] == "devices"
        data = {**_DEVICES, "name": "wohn-zimmer"}
        warned = await flow.async_configure(result["flow_id"], data)
        assert warned["type"] == FlowResultType.FORM
        assert warned["step_id"] == "devices"
        assert "wohn_zimmer" in warned["description_placeholders"]["name_warnings"]
        accepted = await flow.async_configure(warned["flow_id"], data)
        assert accepted["step_id"] == "control_options"

    async def test_form_shows_schedule_key(self, hass, enable_custom_integrations):
        flow = hass.config_entries.flow
        result = await flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await flow.async_configure(result["flow_id"], {"next_step_id": "add_zone"})
        assert result["step_id"] == "devices"
        assert "schedule_key" in result["description_placeholders"]
