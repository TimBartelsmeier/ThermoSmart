"""Climate platform für ThermoSmart – virtuelle Thermostat-Entität pro Zone."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    PRESET_AWAY as HA_PRESET_AWAY,
    PRESET_COMFORT as HA_PRESET_COMFORT,
    PRESET_ECO as HA_PRESET_ECO,
    PRESET_SLEEP as HA_PRESET_SLEEP,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er_helper
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, VERSION,
    PRESET_COMFORT, PRESET_ECO, PRESET_NIGHT, PRESET_AWAY, PRESET_VACATION,
)
from .coordinator import ThermoSmartCoordinator
from .triggers import TRIGGER_SWITCH, TRIGGER_TARGET_CHANGE
from .target import SOURCE_USER, TARGET_MAX_C, TARGET_MIN_C

_LOGGER = logging.getLogger(__name__)

# ThermoSmart preset → HA standard preset name. The HA constants make cards, voice
# assistants and automations work without extra configuration; "vacation" has no
# HA equivalent and stays a custom string.
PRESET_TO_HA = {
    PRESET_COMFORT:  HA_PRESET_COMFORT,
    PRESET_ECO:      HA_PRESET_ECO,
    PRESET_NIGHT:    HA_PRESET_SLEEP,
    PRESET_AWAY:     HA_PRESET_AWAY,
    PRESET_VACATION: "vacation",
}
HA_TO_PRESET = {v: k for k, v in PRESET_TO_HA.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ThermoSmartCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([ThermoSmartClimate(coordinator, entry)])


class ThermoSmartClimate(CoordinatorEntity, ClimateEntity, RestoreEntity):
    """Virtuelle Climate-Entität für eine ThermoSmart-Zone.

    Zeigt: Ist-Temperatur, Zieltemperatur, Modus.
    Steuert: Zieltemperatur (Override), HVAC-Modus, Preset.
    """

    _attr_has_entity_name = True
    _attr_name = None  # Primärentität des Geräts → name = Gerätename
    _attr_translation_key = "zone"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_preset_modes = list(PRESET_TO_HA.values())
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_target_temperature_step = 0.5
    _attr_min_temp = TARGET_MIN_C
    _attr_max_temp = TARGET_MAX_C

    def __init__(self, coordinator: ThermoSmartCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        zone_name = entry.data.get("name", "Zone")
        self._attr_unique_id = f"{entry.entry_id}_climate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"ThermoSmart – {zone_name}",
            manufacturer="ThermoSmart",
            model="Self-learning Heating Controller",
            sw_version=VERSION,
            entry_type="service",  # type: ignore[arg-type]
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last:
            # Restore the desired target; the preset is only kept if the value still matches.
            try:
                value = float(last.attributes.get(ATTR_TEMPERATURE))
                preset = HA_TO_PRESET.get(last.attributes.get("preset_mode"))
                self.coordinator.target.restore(value, preset)
            except (TypeError, ValueError):
                pass

    # ── Eigenschaften ────────────────────────────────────────────────

    @property
    def _zone(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get("zone", {})

    @property
    def current_temperature(self) -> float | None:
        live = self.coordinator._live_temp
        return live if live is not None else self._zone.get("current_temp")

    @property
    def current_humidity(self) -> float | None:
        live = self.coordinator._live_humidity
        return live if live is not None else self._zone.get("indoor_humidity")

    @property
    def target_temperature(self) -> float | None:
        """The desired target – also shown while a window or suspension holds the TRVs."""
        return self.coordinator.target.value

    @property
    def hvac_mode(self) -> HVACMode:
        if not self.coordinator._active_control:
            return HVACMode.OFF
        return HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction:
        if not self.coordinator._active_control:
            return HVACAction.OFF
        curr = self._zone.get("current_temp")
        target = self._zone.get("effective_target")
        if curr is None or target is None:
            return HVACAction.IDLE
        tolerance = self.coordinator.zone_cfg.get("temp_tolerance", 0.5)
        if curr < target - tolerance:
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def preset_mode(self) -> str | None:
        return PRESET_TO_HA.get(self.coordinator.target.preset)

    def _get_device_batteries(self) -> dict[str, int]:
        """Liest Batteriestände aller Geräte in der Zone (TRVs + Temp-/Feuchtigkeitssensoren).

        Zwei Methoden werden nacheinander versucht:
        1. Batterie als Attribut direkt auf der Entity (z. B. manche Aqara-Sensoren via ZHA).
        2. Separate battery-Sensor-Entity am selben Gerät (Standard bei Zigbee-TRVs via Z2M/ZHA).
        """
        cfg = self.coordinator.zone_cfg
        devices: list[str] = []
        devices.extend(cfg.get("climate_entities", []))
        devices.extend(s for s in cfg.get("temp_sensors", []) if s)
        devices.extend(s for s in cfg.get("humidity_sensors", []) if s)

        er = er_helper.async_get(self.coordinator.hass)
        batteries: dict[str, int] = {}

        for entity_id in devices:
            # Methode 1: Batterie-Attribut direkt auf der Entity
            state = self.coordinator.hass.states.get(entity_id)
            if state is not None and state.state not in ("unavailable", "unknown"):
                for attr_name in ("battery_level", "battery", "bat_percent", "battery_percent"):
                    val = state.attributes.get(attr_name)
                    if val is not None:
                        try:
                            batteries[entity_id] = int(float(val))
                            break
                        except (TypeError, ValueError):
                            pass

            if entity_id in batteries:
                continue

            # Methode 2: Separate battery-Sensor-Entity am selben Gerät (Zigbee-TRVs u. a.)
            entry = er.async_get(entity_id)
            if entry is None or entry.device_id is None:
                continue
            for sibling in er.entities.get_entries_for_device_id(entry.device_id):
                if sibling.domain != "sensor":
                    continue
                dc = sibling.device_class or sibling.original_device_class
                if dc != "battery":
                    continue
                bat_state = self.coordinator.hass.states.get(sibling.entity_id)
                if bat_state is None or bat_state.state in ("unavailable", "unknown"):
                    continue
                try:
                    batteries[entity_id] = int(float(bat_state.state))
                    break
                except (TypeError, ValueError):
                    pass

        return batteries

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        z = self._zone
        trv = z.get("trv_setpoint")
        target = self.coordinator.target.value
        boost_delta = round(trv - target, 1) if trv is not None and trv > target else 0.0
        return {
            "desired_target_source": self.coordinator.target.current.source,
            "preheat_time":         f"{z.get('preheat_minutes', 0)} min",
            "learning_confidence":  f"{round((z.get('learning_confidence', 0) * 100), 1)}%",
            "outdoor_temperature":  z.get("outdoor_temp"),
            "trv_setpoint":         trv,
            "boost_delta":          f"+{boost_delta}°C" if boost_delta > 0 else "0°C",
            "boost_factor":            z.get("boost_factor", 1.0),            # compat: 1.0=neutral
            "boost_offset_c":          z.get("boost_offset_c", 0.0),          # Learning raw prediction: 0.0=neutral
            "applied_boost_offset_c":  z.get("applied_boost_offset_c", 0.0),  # confirmed dispatch truth: 0.0=not applied
            "observation_mode":     not self.coordinator._active_control,
            "window_open":          bool(z.get("window_open", False)),
            "summer_mode":          self.coordinator._is_summer,
            "heating_failure":      self.coordinator._heating_failure_notified,
            "preheat_active":       bool(z.get("preheat_active", False)),
            "device_batteries":     self._get_device_batteries(),
        }

    # ── Steuerung ────────────────────────────────────────────────────

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the desired target (a manual change; last writer wins)."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        try:
            self.coordinator.set_target(float(temp), SOURCE_USER)
        except ValueError as err:
            raise ServiceValidationError(
                f"Target must be between {TARGET_MIN_C:g} and {TARGET_MAX_C:g} °C"
            ) from err
        self.async_write_ha_state()
        self.coordinator.request_pass(TRIGGER_TARGET_CHANGE)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Aktive Steuerung ein- oder ausschalten."""
        active = hvac_mode == HVACMode.HEAT
        self.coordinator.set_active_control(active)
        self.async_write_ha_state()
        self.coordinator.request_pass(TRIGGER_SWITCH)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Apply a preset: the desired target becomes that preset's temperature."""
        preset = HA_TO_PRESET.get(preset_mode)
        if preset is None:
            raise ServiceValidationError(f"Unknown preset '{preset_mode}'")
        self.coordinator.set_preset(preset)
        self.async_write_ha_state()
        self.coordinator.request_pass(TRIGGER_TARGET_CHANGE)

