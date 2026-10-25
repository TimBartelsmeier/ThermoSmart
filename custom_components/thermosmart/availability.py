"""Recoverable entity availability issues, independent of control passes."""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.start import async_at_started

from .const import (
    CONF_CLIMATE_ENTITIES,
    CONF_DEVICE_SETTINGS,
    CONF_DEV_CALIBRATION_ENTITY,
    CONF_DEV_CALIBRATION_SYNC,
    CONF_DEV_CONTROL_MODE,
    CONF_DEV_EXT_TEMP_ENTITY,
    CONF_DEV_FEED_EXT_TEMP,
    CONF_DEV_STATE_SETTINGS,
    CONF_DEV_VALVE_ENTITY,
    CONF_OUTDOOR_HUMIDITY_SENSOR,
    CONF_OUTDOOR_RAIN_SENSOR,
    CONF_OUTDOOR_SOLAR_SENSOR,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_OUTDOOR_WIND_SENSOR,
    CONF_WEATHER_ENTITY,
    CONF_ZONE_CONTROL_MODE,
    CONTROL_MODE_DIRECT_VALVE,
    CONTROL_MODE_INHERIT,
    CONTROL_MODE_TPI_BOOST,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
UNAVAILABLE_LOG_DELAY_SECONDS = 180


def configured_entities(cfg: Mapping) -> dict[str, str]:
    """Map used entity IDs to their roles, merging repeated references."""
    roles: dict[str, list[str]] = {}

    def add(entity_id: str | None, role: str) -> None:
        if entity_id:
            assigned = roles.setdefault(entity_id, [])
            if role not in assigned:
                assigned.append(role)

    add(cfg.get(CONF_WEATHER_ENTITY), "weather source")
    add(cfg.get(CONF_OUTDOOR_TEMP_SENSOR), "outdoor temperature sensor")
    if cfg.get("entry_type") != "system":
        for key, role in (
            (CONF_OUTDOOR_HUMIDITY_SENSOR, "outdoor humidity sensor"),
            (CONF_OUTDOOR_WIND_SENSOR, "outdoor wind sensor"),
            (CONF_OUTDOOR_SOLAR_SENSOR, "outdoor solar sensor"),
            (CONF_OUTDOOR_RAIN_SENSOR, "outdoor rain sensor"),
        ):
            add(cfg.get(key), role)
        for key, role in (
            ("temp_sensors", "room temperature sensor"),
            ("humidity_sensors", "room humidity sensor"),
            ("window_sensors", "window sensor"),
            (CONF_CLIMATE_ENTITIES, "TRV"),
        ):
            for entity_id in cfg.get(key, []):
                add(entity_id, role)
        for climate_id in cfg.get(CONF_CLIMATE_ENTITIES, []):
            settings = cfg.get(CONF_DEVICE_SETTINGS, {}).get(climate_id, {})
            mode = settings.get(CONF_DEV_CONTROL_MODE, CONTROL_MODE_INHERIT)
            if mode == CONTROL_MODE_INHERIT:
                mode = cfg.get(CONF_ZONE_CONTROL_MODE, CONTROL_MODE_TPI_BOOST)
            if mode == CONTROL_MODE_DIRECT_VALVE:
                add(settings.get(CONF_DEV_VALVE_ENTITY), f"valve control for {climate_id}")
            if settings.get(CONF_DEV_FEED_EXT_TEMP, False):
                add(settings.get(CONF_DEV_EXT_TEMP_ENTITY), f"temperature input for {climate_id}")
            if settings.get(CONF_DEV_CALIBRATION_SYNC, False):
                add(settings.get(CONF_DEV_CALIBRATION_ENTITY), f"calibration for {climate_id}")
            for rule in settings.get(CONF_DEV_STATE_SETTINGS, []):
                add(rule.get("entity_id"), f"state setting for {climate_id}")
    return {entity_id: ", ".join(assigned) for entity_id, assigned in roles.items()}


@dataclass
class _Outage:
    cancel_warning: Callable[[], None] | None = None
    logged: bool = False


class EntityAvailabilityMonitor:
    """Show Repairs immediately after startup; log only sustained outages."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        cfg = {**entry.data, **entry.options}
        self.name = str(cfg.get("name", entry.title))
        self.entities = configured_entities(cfg)
        self._outages: dict[str, _Outage] = {}
        self._unsubscribers: list[Callable[[], None]] = []

    @callback
    def start(self) -> None:
        self._unsubscribers.append(
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop)
        )
        self._unsubscribers.append(async_at_started(self.hass, self._on_started))

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        if not self.entities:
            return
        self._unsubscribers.append(async_track_state_change_event(
            self.hass, list(self.entities), self._on_state_change
        ))
        for entity_id in self.entities:
            self._check(entity_id, self.hass.states.get(entity_id))

    @callback
    def _on_state_change(self, event: Event) -> None:
        self._check(event.data["entity_id"], event.data["new_state"])

    def _issue_id(self, entity_id: str) -> str:
        return f"entity_unavailable_{self.entry.entry_id}_{entity_id}"

    @staticmethod
    def _is_unavailable(state: State | None) -> bool:
        return state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE)

    @callback
    def _check(self, entity_id: str, state: State | None) -> None:
        if not self._is_unavailable(state):
            if outage := self._outages.pop(entity_id, None):
                if outage.cancel_warning is not None:
                    outage.cancel_warning()
                ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(entity_id))
                if outage.logged:
                    _LOGGER.info(
                        "ThermoSmart '%s': %s (%s) is available again",
                        self.name, entity_id, self.entities[entity_id],
                    )
            return
        if entity_id in self._outages:
            return
        outage = self._outages[entity_id] = _Outage()
        ir.async_create_issue(
            self.hass, DOMAIN, self._issue_id(entity_id),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="entity_unavailable",
            translation_placeholders={
                "name": self.name, "entity_id": entity_id, "roles": self.entities[entity_id],
            },
        )

        @callback
        def warn_if_unavailable(_now) -> None:
            outage.cancel_warning = None
            state = self.hass.states.get(entity_id)
            if not self._is_unavailable(state):
                self._check(entity_id, state)
                return
            outage.logged = True
            _LOGGER.warning(
                "ThermoSmart '%s': %s (%s) has been unavailable for at least 3 minutes; "
                "see Settings > System > Repairs",
                self.name, entity_id, self.entities[entity_id],
            )

        outage.cancel_warning = async_call_later(
            self.hass, UNAVAILABLE_LOG_DELAY_SECONDS, warn_if_unavailable
        )

    @callback
    def _on_stop(self, _event: Event) -> None:
        self.stop()

    @callback
    def stop(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        for entity_id, outage in self._outages.items():
            if outage.cancel_warning is not None:
                outage.cancel_warning()
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(entity_id))
        self._outages.clear()
