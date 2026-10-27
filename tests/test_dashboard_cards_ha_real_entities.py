"""Card data and services against real HA entities, including renamed entities."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from aiohttp.resolver import ThreadedResolver

import pytest

from homeassistant.helpers import entity_registry as er
from homeassistant.exceptions import Unauthorized, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.thermosmart import dashboard
from custom_components.thermosmart.const import DOMAIN
from custom_components.thermosmart.learning.clock import FakeClock
from tests.test_zone_setup_entities import _setup_zone


async def test_data_discovers_renamed_companions_and_numeric_history_tracks_target(hass, enable_custom_integrations):
    entry = await _setup_zone(hass)
    registry = er.async_get(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = dashboard.companion_entities(hass, coordinator)
    assert all(entities.values())
    climate_id = registry.async_get_entity_id("climate", DOMAIN, f"{entry.entry_id}_climate")
    assert dashboard.zone_for_entity(hass, climate_id) is coordinator
    target_id = entities["desired_temperature"]
    actual_id = entities["actual_temperature"]
    registry.async_update_entity(target_id, new_entity_id="sensor.renamed_desired_target")
    await hass.async_block_till_done()
    assert dashboard.companion_entities(hass, coordinator)["desired_temperature"] == "sensor.renamed_desired_target"
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id, "temperature": 22.5}, blocking=True
    )
    assert float(hass.states.get("sensor.renamed_desired_target").state) == 22.5
    assert hass.states.get(actual_id).attributes["device_class"] == "temperature"
    assert float(hass.states.get(actual_id).state) == 19.0
    coordinator._live_temp = 20.25
    coordinator.async_update_listeners()
    assert float(hass.states.get(actual_id).state) == 20.25
    coordinator._vacation_mode = True
    coordinator.async_update_listeners()
    assert hass.states.get(climate_id).attributes["vacation_mode"] is True


async def test_shortcut_calls_real_datetime_entity_and_skips_event(hass, enable_custom_integrations):
    now = dt_util.now()
    at = (now + timedelta(minutes=5)).replace(second=0, microsecond=0)
    entry = await _setup_zone(hass, {
        "schedule_events": [{"days": [at.weekday()], "time": at.strftime("%H:%M"), "target": "comfort"}]
    })
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    coordinator._clock = FakeClock(now)
    registry = er.async_get(hass)
    climate_id = registry.async_get_entity_id("climate", DOMAIN, f"{entry.entry_id}_climate")
    connection = MagicMock()
    connection.user.permissions.check_entity.return_value = True
    from homeassistant.core import Context
    connection.context.return_value = Context()
    await dashboard.async_suspend_schedule.__wrapped__(
        hass, connection, {"id": 1, "entity_id": climate_id, "action": "skip_next"}
    )
    assert dt_util.as_utc(coordinator.scheduling_disabled_until) == dt_util.as_utc(at) + timedelta(seconds=1)
    data = dashboard.dashboard_data(hass, coordinator, max_events=1)
    assert dt_util.parse_datetime(data["events"][0]["at"]) > at
    assert data["events"][0]["preset"] == "comfort"
    assert data["events"][0]["temperature"] == 21.0
    with pytest.raises(ServiceValidationError, match="shorten"):
        # A week-long pause established by skip-next cannot be shortened.
        coordinator.set_scheduling_disabled_until(now + timedelta(days=8))
        await dashboard.async_suspend_schedule.__wrapped__(
            hass, connection, {"id": 2, "entity_id": climate_id, "action": "12h"}
        )
    connection.user.permissions.check_entity.return_value = False
    with pytest.raises(Unauthorized):
        await dashboard.async_suspend_schedule.__wrapped__(
            hass, connection, {"id": 3, "entity_id": climate_id, "action": "24h"}
        )


async def test_rejects_non_thermosmart_entity(hass):
    with pytest.raises(ServiceValidationError, match="ThermoSmart"):
        dashboard.zone_for_entity(hass, "climate.other")


async def test_websocket_output_shape_and_invalid_limits(
    hass, enable_custom_integrations, hass_ws_client
):
    with patch("aiohttp.connector.DefaultResolver", ThreadedResolver):
        entry = await _setup_zone(hass, {
            "schedule_events": [
                {"days": list(range(7)), "time": "08:00", "target": "comfort", "preheat": True}
            ]
        })
        registry = er.async_get(hass)
        climate_id = registry.async_get_entity_id("climate", DOMAIN, f"{entry.entry_id}_climate")
        client = await hass_ws_client(hass)
        dashboard.async_register_dashboard_commands(hass)
        await client.send_json({
            "id": 1, "type": "thermosmart/dashboard", "entity_id": climate_id, "max_events": 5,
        })
        response = await client.receive_json()
        assert response["success"]
        data = response["result"]
        assert len(data["events"]) == 5
        assert set(data["events"][0]) == {"at", "preset", "temperature"}
        assert data["events"][0]["preset"] == "comfort"
        assert set(data["preheat"]) == {"state", "reason", "start"}
        assert set(data["entities"]) == {
            "scheduling_enabled", "scheduling_disabled_until", "next_scheduled_event",
            "preheat_start", "actual_temperature", "desired_temperature",
        }
        await client.send_json({
            "id": 2, "type": "thermosmart/dashboard", "entity_id": climate_id, "max_events": 1.5,
        })
        response = await client.receive_json()
        assert not response["success"]
        assert response["error"]["code"] == "invalid_format"


async def test_preheat_is_not_invented_for_events_beyond_pause(hass, enable_custom_integrations):
    entry = await _setup_zone(hass, {
        "schedule_events": [
            {"days": list(range(7)), "time": "08:00", "target": "comfort", "preheat": True}
        ]
    })
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    coordinator.set_scheduling_disabled_until(dt_util.now() + timedelta(days=3))
    data = dashboard.dashboard_data(hass, coordinator, max_events=1)
    assert data["preheat"]["state"] == "not_calculated"
    assert data["preheat"]["start"] is None


async def test_matching_preheat_plan_exposes_cancellation_and_window_suspension(
    hass, enable_custom_integrations
):
    from custom_components.thermosmart.preheat import PreheatPlan

    entry = await _setup_zone(hass, {
        "schedule_events": [
            {"days": list(range(7)), "time": "08:00", "target": "comfort", "preheat": True}
        ]
    })
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    at = coordinator.scheduler.next_event.at
    coordinator._preheat_plan = PreheatPlan("none", "cancelled_by_manual_change", event_at=at)
    assert dashboard.dashboard_data(hass, coordinator, max_events=1)["preheat"]["reason"] == "cancelled_by_manual_change"
    coordinator._preheat_plan = PreheatPlan("active", start=at - timedelta(minutes=30), event_at=at)
    coordinator.data["zone"]["suspension"] = "window"
    assert dashboard.dashboard_data(hass, coordinator, max_events=1)["preheat"]["state"] == "suspended"
    coordinator._preheat_plan = PreheatPlan("none", "scheduling_inactive", event_at=at)
    assert dashboard.dashboard_data(hass, coordinator, max_events=1)["preheat"]["state"] == "not_calculated"
