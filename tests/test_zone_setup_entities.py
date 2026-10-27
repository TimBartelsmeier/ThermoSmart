"""Full integration setup: every entity of a new zone must write a valid state."""
from __future__ import annotations

from datetime import timedelta
from threading import get_ident
from unittest.mock import AsyncMock, patch

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.thermosmart.const import DOMAIN
from custom_components.thermosmart.learning.clock import FakeClock
from custom_components.thermosmart.target import SOURCE_EVENT


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


async def test_schedule_timer_updates_entities_on_event_loop(
    hass, enable_custom_integrations, caplog
):
    start = dt_util.now()
    first = (start + timedelta(minutes=1)).replace(second=0, microsecond=0)
    second = first + timedelta(minutes=1)
    entry = await _setup_zone(hass, {
        "schedule_events": [
            {"days": [at.weekday()], "time": at.strftime("%H:%M"), "target": preset}
            for at, preset in ((first, "night"), (second, "eco"))
        ],
    })
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    clock = FakeClock(start)
    coordinator._clock = clock
    loop_thread = get_ident()
    target_threads = []
    remove_listener = coordinator.target.add_listener(
        lambda new, previous: target_threads.append(get_ident())
    )
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    climate_id = next(e.entity_id for e in entities if e.domain == "climate")
    preset_id = next(e.entity_id for e in entities if e.domain == "select")
    next_event_id = next(
        e.entity_id for e in entities
        if e.unique_id == f"{entry.entry_id}_next_scheduled_event"
    )
    try:
        with patch.object(
            coordinator, "async_request_refresh", new_callable=AsyncMock
        ) as refresh:
            for at, preset in ((first, "night"), (second, "eco")):
                clock.set_utc(at)
                async_fire_time_changed(hass, at)
                await hass.async_block_till_done()
                assert coordinator.target.current.source == SOURCE_EVENT
                assert coordinator.target.preset == preset
                assert hass.states.get(climate_id).attributes["temperature"] == 18.0
                assert hass.states.get(preset_id).state == preset
                assert hass.states.get(next_event_id).state == (
                    dt_util.as_utc(coordinator.scheduler.next_event.at).isoformat()
                )
            assert refresh.await_count == 2
        assert target_threads == [loop_thread, loop_thread]
        assert not any(
            record.name == "custom_components.thermosmart.schedule"
            and record.levelname == "ERROR"
            for record in caplog.records
        )
        assert "thread other than the event loop" not in caplog.text
        assert "Unexpected error updating listener" not in caplog.text
    finally:
        remove_listener()
        coordinator.scheduler.stop()
