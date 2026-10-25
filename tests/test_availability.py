"""Entity Repairs follow availability transitions without polling or TRV writes."""
from __future__ import annotations

from datetime import timedelta
import logging
from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)

from custom_components.thermosmart.availability import (
    EntityAvailabilityMonitor,
    async_call_later,
    configured_entities,
)
from custom_components.thermosmart.const import DOMAIN


def test_discovers_all_used_zone_entities():
    cfg = {
        "weather_entity": "weather.home",
        "outdoor_temp_sensor": "sensor.outdoor",
        "outdoor_humidity_sensor": "sensor.humidity",
        "outdoor_wind_sensor": "sensor.wind",
        "outdoor_solar_sensor": "sensor.solar",
        "outdoor_rain_sensor": "sensor.rain",
        "temp_sensors": ["sensor.room", "sensor.room"],
        "humidity_sensors": ["sensor.humidity"],
        "window_sensors": ["binary_sensor.window"],
        "climate_entities": ["climate.trv"],
        "zone_control_mode": "direct_valve",
        "device_settings": {"climate.trv": {
            "control_mode": "inherit",
            "valve_entity": "number.valve",
            "feed_external_temp": True,
            "ext_temp_entity": "number.external",
            "calibration_sync": True,
            "calibration_entity": "number.calibration",
            "state_settings": [{"entity_id": "select.mode", "value": "external"}],
        }},
    }
    entities = configured_entities(cfg)
    assert set(entities) == {
        "weather.home", "sensor.outdoor", "sensor.humidity", "sensor.wind", "sensor.solar",
        "sensor.rain", "sensor.room", "binary_sensor.window", "climate.trv",
        "number.valve", "number.external", "number.calibration", "select.mode",
    }
    assert entities["sensor.room"] == "room temperature sensor"
    assert entities["sensor.humidity"] == "outdoor humidity sensor, room humidity sensor"


def test_does_not_monitor_disabled_device_features_or_orphaned_settings():
    entities = configured_entities({
        "climate_entities": ["climate.trv"],
        "zone_control_mode": "direct_valve",
        "device_settings": {
            "climate.trv": {
                "control_mode": "setpoint",
                "valve_entity": "number.valve",
                "feed_external_temp": False,
                "ext_temp_entity": "number.external",
                "calibration_sync": False,
                "calibration_entity": "number.calibration",
            },
            "climate.old": {
                "state_settings": [{"entity_id": "switch.old", "value": "off"}],
            },
        },
    })
    assert entities == {"climate.trv": "TRV"}
    assert configured_entities({}) == {}


def test_system_monitors_only_its_configured_sources():
    assert configured_entities({
        "entry_type": "system",
        "weather_entity": "weather.home",
        "outdoor_temp_sensor": "sensor.out",
        "temp_sensors": ["sensor.unused"],
    }) == {"weather.home": "weather source", "sensor.out": "outdoor temperature sensor"}


@pytest.fixture
def monitor(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Room",
        data={"name": "Room", "temp_sensors": ["sensor.room"]},
    )
    monitor = EntityAvailabilityMonitor(hass, entry)
    yield monitor
    monitor.stop()


def _issue(monitor):
    return ir.async_get(monitor.hass).async_get_issue(
        DOMAIN, monitor._issue_id("sensor.room")
    )


def _warnings(caplog):
    return [
        record for record in caplog.records
        if record.name == "custom_components.thermosmart.availability"
        and record.levelno >= logging.WARNING
    ]


async def test_startup_waits_and_ignores_entities_that_load_before_started(hass, monitor, caplog):
    hass.state = CoreState.starting
    monitor.start()
    assert _issue(monitor) is None
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(minutes=10))
    await hass.async_block_till_done()
    assert not _warnings(caplog)
    hass.states.async_set("sensor.room", "20")
    hass.state = CoreState.running
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert _issue(monitor) is None


async def test_missing_entity_after_started_creates_repair(hass, monitor):
    hass.state = CoreState.starting
    monitor.start()
    assert _issue(monitor) is None
    hass.state = CoreState.running
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    issue = _issue(monitor)
    assert issue is not None
    assert issue.translation_placeholders == {
        "name": "Room", "entity_id": "sensor.room", "roles": "room temperature sensor",
    }
    assert not issue.is_persistent
    assert not issue.is_fixable


async def test_warning_timer_starts_only_after_startup(hass, monitor, caplog):
    hass.state = CoreState.starting
    monitor.start()
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(minutes=10))
    await hass.async_block_till_done()
    assert _issue(monitor) is None
    assert not _warnings(caplog)
    hass.state = CoreState.running
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert _issue(monitor) is not None
    assert not _warnings(caplog)
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(seconds=181))
    await hass.async_block_till_done()
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize("state", [None, "unknown", "unavailable"])
async def test_short_outage_clears_without_warning(hass, monitor, caplog, state):
    caplog.set_level(logging.INFO, logger="custom_components.thermosmart.availability")
    if state is not None:
        hass.states.async_set("sensor.room", state)
    monitor.start()
    assert _issue(monitor) is not None
    hass.states.async_set("sensor.room", "21")
    await hass.async_block_till_done()
    assert _issue(monitor) is None
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(seconds=181))
    await hass.async_block_till_done()
    assert not _warnings(caplog)
    assert "available again" not in caplog.text


async def test_logs_once_after_three_minutes_and_logs_recovery(hass, monitor, caplog):
    caplog.set_level(logging.INFO, logger="custom_components.thermosmart.availability")
    start = dt_util.utcnow()
    monitor.start()
    async_fire_time_changed_exact(hass, start + timedelta(seconds=179))
    await hass.async_block_till_done()
    assert not _warnings(caplog)
    async_fire_time_changed_exact(hass, start + timedelta(seconds=181))
    await hass.async_block_till_done()
    assert len(_warnings(caplog)) == 1
    assert "at least 3 minutes" in _warnings(caplog)[0].message
    hass.states.async_set("sensor.room", "unknown")
    await hass.async_block_till_done()
    async_fire_time_changed_exact(hass, start + timedelta(minutes=10))
    await hass.async_block_till_done()
    assert len(_warnings(caplog)) == 1
    hass.states.async_set("sensor.room", "20")
    await hass.async_block_till_done()
    assert _issue(monitor) is None
    assert "available again" in caplog.text


async def test_removal_and_repeated_outages_are_detected(hass, monitor, caplog):
    hass.states.async_set("sensor.room", "20")
    monitor.start()
    assert _issue(monitor) is None
    for _ in range(2):
        hass.states.async_remove("sensor.room")
        await hass.async_block_till_done()
        assert _issue(monitor) is not None
        hass.states.async_set("sensor.room", "20")
        await hass.async_block_till_done()
        assert _issue(monitor) is None
    assert not _warnings(caplog)


async def test_recovery_resets_warning_timer_for_a_new_outage(hass, monitor, caplog):
    monitor.start()
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(seconds=181))
    await hass.async_block_till_done()
    assert len(_warnings(caplog)) == 1
    hass.states.async_set("sensor.room", "20")
    await hass.async_block_till_done()
    hass.states.async_set("sensor.room", "unavailable")
    await hass.async_block_till_done()
    assert _issue(monitor) is not None
    assert len(_warnings(caplog)) == 1
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(seconds=179))
    await hass.async_block_till_done()
    assert len(_warnings(caplog)) == 1
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(seconds=181))
    await hass.async_block_till_done()
    assert len(_warnings(caplog)) == 2


async def test_rapid_recovery_starts_new_timer_but_unknown_updates_do_not(hass, monitor):
    with patch(
        "custom_components.thermosmart.availability.async_call_later",
        wraps=async_call_later,
    ) as timer:
        monitor.start()
        assert timer.call_count == 1
        assert timer.call_args.args[1] == 180
        hass.states.async_set("sensor.room", "unknown")
        hass.states.async_set("sensor.room", "unavailable")
        await hass.async_block_till_done()
        assert timer.call_count == 1
        # State events must be processed in order, even when already superseded.
        hass.states.async_set("sensor.room", "20")
        hass.states.async_set("sensor.room", "unavailable")
        await hass.async_block_till_done()
        assert timer.call_count == 2
        assert timer.call_args.args[1] == 180
        assert _issue(monitor) is not None


async def test_stop_clears_repairs_and_cancels_pending_warnings(hass, monitor, caplog):
    monitor.start()
    assert _issue(monitor) is not None
    monitor.stop()
    monitor.stop()
    assert _issue(monitor) is None
    hass.states.async_set("sensor.room", "unknown")
    await hass.async_block_till_done()
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(minutes=4))
    await hass.async_block_till_done()
    assert _issue(monitor) is None
    assert not _warnings(caplog)


async def test_stopping_before_start_cancels_start_listener(hass, monitor):
    hass.state = CoreState.starting
    monitor.start()
    monitor.stop()
    hass.state = CoreState.running
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert _issue(monitor) is None


async def test_shutdown_stops_reporting(hass, monitor, caplog):
    monitor.start()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert _issue(monitor) is None
    async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(minutes=4))
    await hass.async_block_till_done()
    assert not _warnings(caplog)


async def test_same_entity_in_two_entries_has_independent_repairs(hass, monitor):
    other = EntityAvailabilityMonitor(hass, MockConfigEntry(
        domain=DOMAIN, title="Other", data={"temp_sensors": ["sensor.room"]},
    ))
    try:
        monitor.start()
        other.start()
        assert _issue(monitor) is not None
        assert _issue(other) is not None
        monitor.stop()
        assert _issue(other) is not None
        hass.states.async_set("sensor.room", "20")
        await hass.async_block_till_done()
        assert _issue(other) is None
    finally:
        other.stop()


@pytest.mark.parametrize("entry_type", ["system", "zone"])
async def test_entry_setup_reload_and_unload_manage_repairs(
    hass, enable_custom_integrations, entry_type, caplog
):
    from tests.test_zone_setup_entities import _setup_zone

    with patch("custom_components.thermosmart._async_register_card"):
        if entry_type == "zone":
            entry = await _setup_zone(hass, {"outdoor_temp_sensor": "sensor.missing"})
        else:
            entry = MockConfigEntry(
                domain=DOMAIN, title="ThermoSmart System",
                data={"entry_type": "system", "outdoor_temp_sensor": "sensor.missing"},
            )
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        registry = ir.async_get(hass)
        old_id = f"entity_unavailable_{entry.entry_id}_sensor.missing"
        new_id = f"entity_unavailable_{entry.entry_id}_sensor.new"
        assert registry.async_get_issue(DOMAIN, old_id) is not None
        assert not _warnings(caplog)
        assert "nicht gefunden" not in caplog.text
        assert "no outdoor temperature available" not in caplog.text

        hass.config_entries.async_update_entry(
            entry, options={"outdoor_temp_sensor": "sensor.new"}
        )
        await hass.async_block_till_done()
        assert registry.async_get_issue(DOMAIN, old_id) is None
        assert registry.async_get_issue(DOMAIN, new_id) is not None

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get_issue(DOMAIN, new_id) is None
        async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(minutes=4))
        await hass.async_block_till_done()
        assert not _warnings(caplog)
