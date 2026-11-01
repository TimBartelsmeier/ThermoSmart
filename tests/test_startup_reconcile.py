"""Startup reconciliation after restore (review H5)."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

from custom_components.thermosmart.const import DOMAIN
from custom_components.thermosmart.triggers import TRIGGER_STARTUP


async def test_zone_reconciles_once_restored_state_and_schedule_are_ready(
        hass, enable_custom_integrations):
    event_at = (dt_util.now() + timedelta(minutes=30)).replace(second=0, microsecond=0)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "entry_type": "zone",
            "name": "Audit",
            "climate_entities": ["climate.audit_trv"],
            "temp_sensors": [],
            "window_sensors": [],
            "schedule_events": [
                {"days": [event_at.weekday()], "time": event_at.strftime("%H:%M"),
                 "target": 22.0, "preheat": True}
            ],
        },
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    climate_entry = registry.async_get_or_create(
        "climate", DOMAIN, f"{entry.entry_id}_climate", suggested_object_id="audit_zone")
    switch_entry = registry.async_get_or_create(
        "switch", DOMAIN, f"{entry.entry_id}_active_control",
        suggested_object_id="audit_active_control")
    mock_restore_cache(hass, [
        State(switch_entry.entity_id, "on"),
        State(climate_entry.entity_id, "heat", {"temperature": 19.0}),
    ])
    hass.states.async_set(
        "climate.audit_trv", "heat",
        {"temperature": 20.0, "current_temperature": 19.0, "hvac_modes": ["heat", "off"]},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coord = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    assert coord.data["active_control"] is True
    assert coord.data["zone"]["desired_target"] == 19.0
    assert TRIGGER_STARTUP in coord.data["zone"]["pass_triggers"]
    assert coord.scheduler.next_event is not None
    assert coord.preheat_plan.reason != "no_event"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
