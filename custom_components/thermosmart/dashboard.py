"""Registry-backed dashboard data and timezone-safe schedule shortcuts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import DOMAIN
from .coordinator import ThermoSmartCoordinator
from .schedule import _local_instant, event_preset, resolve_target, upcoming_occurrences

_ENTITY_KEYS = {
    "scheduling_enabled": "switch",
    "scheduling_disabled_until": "datetime",
    "next_scheduled_event": "sensor",
    "preheat_start": "sensor",
    "actual_temperature": "sensor",
    "desired_temperature": "sensor",
}


def _limit(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise vol.Invalid("schedule limits must be positive integers or null")
    return value


def zone_for_entity(hass: HomeAssistant, entity_id: str) -> ThermoSmartCoordinator:
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None or entry.platform != DOMAIN or entry.domain != "climate":
        raise ServiceValidationError("Select a ThermoSmart zone climate entity")
    coordinator = hass.data.get(DOMAIN, {}).get(entry.config_entry_id, {}).get("coordinator")
    if coordinator is None:
        raise ServiceValidationError("The ThermoSmart zone is not loaded")
    return coordinator


def companion_entities(hass: HomeAssistant, coordinator: ThermoSmartCoordinator) -> dict[str, str | None]:
    registry = er.async_get(hass)
    return {
        key: registry.async_get_entity_id(domain, DOMAIN, f"{coordinator.zone_id}_{key}")
        for key, domain in _ENTITY_KEYS.items()
    }


def dashboard_data(
    hass: HomeAssistant,
    coordinator: ThermoSmartCoordinator,
    *,
    max_days: int | None = None,
    max_events: int | None = None,
) -> dict[str, Any]:
    now = coordinator._now_local()
    occurrences = upcoming_occurrences(
        coordinator.scheduler.events, now,
        enabled=coordinator.scheduling_enabled,
        disabled_until=coordinator.scheduling_disabled_until,
        max_days=max_days,
        max_events=max_events,
    )
    plan = coordinator.preheat_plan
    zone = (coordinator.data or {}).get("zone", {})
    next_at = occurrences[0].at if occurrences else None
    preheat: dict[str, Any] = {"state": "none", "reason": "event_without_preheat", "start": None}
    if occurrences and occurrences[0].event.preheat:
        preheat = {"state": "not_calculated", "reason": None, "start": None}
        if (
            plan is not None and plan.event_at is not None and next_at is not None
            and plan.event_at.astimezone(timezone.utc) == next_at.astimezone(timezone.utc)
            and plan.reason != "scheduling_inactive"
        ):
            state, reason = plan.state, plan.reason
            if state == "active" and zone.get("suspension"):
                state, reason = "suspended", zone["suspension"]
            preheat = {"state": state, "reason": reason, "start": plan.start.isoformat() if plan.start else None}
    until = coordinator.scheduling_disabled_until
    return {
        "entities": companion_entities(hass, coordinator),
        "time_zone": hass.config.time_zone,
        "scheduling_enabled": coordinator.scheduling_enabled,
        "disabled_until": until.isoformat() if until else None,
        "schedule_error": coordinator.schedule_error,
        "events": [
            {"at": item.at.isoformat(), "preset": event_preset(item.event.target),
             "temperature": resolve_target(item.event.target, coordinator.zone_cfg)}
            for item in occurrences
        ],
        "preheat": preheat,
    }


def suspension_deadline(coordinator: ThermoSmartCoordinator, action: str) -> datetime:
    now = coordinator._now_local()
    if not coordinator.scheduling_enabled:
        raise ServiceValidationError("Scheduling is disabled")
    if action == "skip_next":
        occurrences = upcoming_occurrences(
            coordinator.scheduler.events, now, enabled=True,
            disabled_until=coordinator.scheduling_disabled_until, max_events=1,
        )
        if not occurrences:
            raise ServiceValidationError("There is no next scheduled event to skip")
        deadline = occurrences[0].at.astimezone(timezone.utc) + timedelta(seconds=1)
    elif action == "midnight":
        deadline = _local_instant(now.date() + timedelta(days=1), 0, now.tzinfo)
    elif action in ("12h", "24h"):
        deadline = now.astimezone(timezone.utc) + timedelta(hours=int(action[:-1]))
    else:
        raise ServiceValidationError("Unknown schedule suspension shortcut")
    existing = coordinator.scheduling_disabled_until
    if existing is not None and existing.astimezone(timezone.utc) > deadline.astimezone(timezone.utc):
        raise ServiceValidationError("This shortcut would shorten the existing suspension; set the date explicitly instead")
    return deadline


@websocket_api.websocket_command({
    vol.Required("type"): "thermosmart/dashboard",
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("max_days", default=None): _limit,
    vol.Optional("max_events", default=None): _limit,
})
@websocket_api.async_response
async def async_dashboard(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if not connection.user.permissions.check_entity(msg["entity_id"], POLICY_READ):
        raise Unauthorized
    coordinator = zone_for_entity(hass, msg["entity_id"])
    connection.send_result(msg["id"], dashboard_data(
        hass, coordinator, max_days=msg["max_days"], max_events=msg["max_events"]
    ))


@websocket_api.websocket_command({
    vol.Required("type"): "thermosmart/suspend_schedule",
    vol.Required("entity_id"): cv.entity_id,
    vol.Required("action"): vol.In(["skip_next", "midnight", "12h", "24h"]),
})
@websocket_api.async_response
async def async_suspend_schedule(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if not connection.user.permissions.check_entity(msg["entity_id"], POLICY_CONTROL):
        raise Unauthorized
    coordinator = zone_for_entity(hass, msg["entity_id"])
    entity_id = companion_entities(hass, coordinator)["scheduling_disabled_until"]
    state = hass.states.get(entity_id) if entity_id else None
    if state is None or state.state == "unavailable":
        raise ServiceValidationError("The scheduling suspension date entity is unavailable or disabled")
    if not connection.user.permissions.check_entity(entity_id, POLICY_CONTROL):
        raise Unauthorized
    deadline = suspension_deadline(coordinator, msg["action"])
    await hass.services.async_call(
        "datetime", "set_value",
        {"entity_id": entity_id, "datetime": deadline.isoformat()},
        blocking=True, context=connection.context(msg),
    )
    applied = coordinator.scheduling_disabled_until
    if applied is None or applied.astimezone(timezone.utc) != deadline.astimezone(timezone.utc):
        raise ServiceValidationError("The scheduling suspension date was not applied")
    connection.send_result(msg["id"], {"disabled_until": deadline.isoformat()})


@callback
def async_register_dashboard_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, async_dashboard)
    websocket_api.async_register_command(hass, async_suspend_schedule)
