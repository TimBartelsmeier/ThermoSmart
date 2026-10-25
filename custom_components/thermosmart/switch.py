"""Switch platform für ThermoSmart."""
from __future__ import annotations
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, VERSION, DOMAIN_GLOBAL_VACATION
from .coordinator import ThermoSmartCoordinator
from .triggers import TRIGGER_SCHEDULING, TRIGGER_SWITCH
from .system import SYSTEM_KEY, SystemController, iter_zone_coordinators

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    cfg = {**entry.data, **entry.options}

    # System-Entry: globale Schalter
    # (Sommer-Override: select.ThermoSmartGlobalSummerSelect)
    if cfg.get("entry_type") == "system":
        controller: SystemController = hass.data[DOMAIN][SYSTEM_KEY]
        async_add_entities([
            ThermoSmartGlobalVacationSwitch(controller),
            ThermoSmartForceSummerSwitch(controller),
            ThermoSmartAutoSummerSwitch(controller),
        ])
        return

    # Zone-Entry: zone-spezifische Schalter
    coordinator: ThermoSmartCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([
        ThermoSmartActiveSwitch(coordinator, entry),
        ThermoSmartLearningSwitch(coordinator, entry),
        ThermoSmartSchedulingSwitch(coordinator, entry),
    ])


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"ThermoSmart – {entry.data.get('name', 'Zone')}",
        manufacturer="ThermoSmart",
        model="Self-learning Heating Controller",
        sw_version=VERSION,
        entry_type="service",  # type: ignore[arg-type]
    )


class ThermoSmartActiveSwitch(SwitchEntity, RestoreEntity):
    """Active control switch – default OFF (observation mode)."""
    _attr_has_entity_name = True
    _attr_translation_key = "active_control"
    _attr_icon = "mdi:thermostat-auto"

    def __init__(self, coordinator: ThermoSmartCoordinator, entry: ConfigEntry):
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_active_control"
        self._attr_device_info = _device_info(entry)
        self._is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        self._is_on = last is not None and last.state == "on"
        self._coordinator.set_active_control(self._is_on)

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "mode": "active_control" if self._is_on else "observation",
        }

    async def async_turn_on(self, **kwargs) -> None:
        self._is_on = True
        self.async_write_ha_state()
        self._coordinator.set_active_control(True)
        self._coordinator.request_pass(TRIGGER_SWITCH)

    async def async_turn_off(self, **kwargs) -> None:
        self._is_on = False
        self.async_write_ha_state()
        self._coordinator.set_active_control(False)
        self._coordinator.request_pass(TRIGGER_SWITCH)


class ThermoSmartLearningSwitch(SwitchEntity, RestoreEntity):
    """Learning mode switch – default ON."""
    _attr_has_entity_name = True
    _attr_translation_key = "learning"
    _attr_icon = "mdi:brain"

    def __init__(self, coordinator: ThermoSmartCoordinator, entry: ConfigEntry):
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_learning"
        self._attr_device_info = _device_info(entry)
        self._is_on = True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._is_on = last.state == "on"
        engine = self._get_engine()
        if engine is not None:
            engine.set_zone_enabled(self._entry.entry_id, self._is_on)

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_turn_on(self, **kwargs) -> None:
        self._is_on = True
        self.async_write_ha_state()
        engine = self._get_engine()
        if engine:
            engine.set_zone_enabled(self._entry.entry_id, True)
        self._coordinator.request_pass(TRIGGER_SWITCH)

    async def async_turn_off(self, **kwargs) -> None:
        self._is_on = False
        self.async_write_ha_state()
        engine = self._get_engine()
        if engine:
            engine.set_zone_enabled(self._entry.entry_id, False)
        self._coordinator.request_pass(TRIGGER_SWITCH)

    def _get_engine(self):
        return self.hass.data.get(DOMAIN, {}).get("learning_engine")


class ThermoSmartSchedulingSwitch(SwitchEntity, RestoreEntity):
    """Enable or disable schedule events for one zone (default ON)."""

    _attr_has_entity_name = True
    _attr_translation_key = "scheduling_enabled"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: ThermoSmartCoordinator, entry: ConfigEntry):
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_scheduling_enabled"
        self._attr_device_info = _device_info(entry)
        self._is_on = True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        self._is_on = last is None or last.state != "off"
        self._coordinator.set_scheduling_enabled(self._is_on)

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        self._is_on = value
        self._coordinator.set_scheduling_enabled(value)
        self.async_write_ha_state()
        self._coordinator.request_pass(TRIGGER_SCHEDULING)


# ── Globale Schalter (domain-weit, steuern alle Zonen) ───────────────────────

def _global_device_info() -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, "global")},
        name="ThermoSmart System",
        manufacturer="ThermoSmart",
        model="Global Control",
        sw_version=VERSION,
    )


class ThermoSmartGlobalVacationSwitch(SwitchEntity, RestoreEntity):
    """Vacation mode – all zones are held at their vacation temperature."""
    _attr_has_entity_name = True
    _attr_translation_key = "vacation_mode"
    _attr_icon = "mdi:airplane"
    _attr_unique_id = DOMAIN_GLOBAL_VACATION

    def __init__(self, controller: SystemController):
        self._controller = controller
        self._attr_device_info = _global_device_info()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        await self._controller.async_set_vacation(last is not None and last.state == "on")

    @property
    def is_on(self) -> bool:
        return self._controller.vacation

    @property
    def extra_state_attributes(self) -> dict:
        return {"active_zones": sum(1 for _ in iter_zone_coordinators(self.hass))}

    async def async_turn_on(self, **kwargs) -> None:
        await self._controller.async_set_vacation(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._controller.async_set_vacation(False)
        self.async_write_ha_state()


class ThermoSmartForceSummerSwitch(SwitchEntity, RestoreEntity):
    """Force summer mode: heating stays off (frost protection) regardless of detection."""
    _attr_has_entity_name = True
    _attr_translation_key = "force_summer_mode"
    _attr_icon = "mdi:weather-sunny"
    _attr_unique_id = f"{DOMAIN}_force_summer"

    def __init__(self, controller: SystemController):
        self._controller = controller
        self._attr_device_info = _global_device_info()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        await self._controller.async_set_force_summer(last is not None and last.state == "on")

    @property
    def is_on(self) -> bool:
        return self._controller.force_summer

    async def async_turn_on(self, **kwargs) -> None:
        await self._controller.async_set_force_summer(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._controller.async_set_force_summer(False)
        self.async_write_ha_state()


class ThermoSmartAutoSummerSwitch(SwitchEntity, RestoreEntity):
    """Enable summer mode automatically from the 72 h outdoor temperature average."""
    _attr_has_entity_name = True
    _attr_translation_key = "auto_summer_mode"
    _attr_icon = "mdi:weather-partly-snowy-rainy"
    _attr_unique_id = f"{DOMAIN}_auto_summer"

    def __init__(self, controller: SystemController):
        self._controller = controller
        self._attr_device_info = _global_device_info()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._controller.restore_detected_summer(
                bool(last.attributes.get("summer_detected", False))
            )
        await self._controller.async_set_auto_summer(last is None or last.state != "off")

    @property
    def is_on(self) -> bool:
        return self._controller.auto_summer

    @property
    def extra_state_attributes(self) -> dict:
        detector = self._controller.detector
        average = detector.average
        return {
            "summer_detected": self._controller.detected_summer,
            "summer_active": self._controller.summer_active,
            "summer_source": self._controller.summer_source,
            "outdoor_temperature": self._controller.outdoor_temperature,
            "average_outdoor_temperature": round(average, 1) if average is not None else None,
            "sample_hours": round(detector.span.total_seconds() / 3600, 1) if detector.span else 0,
            "outdoor_source_configured": self._controller.has_outdoor_source,
            "summer_on_threshold": detector.on_threshold,
            "summer_off_threshold": detector.off_threshold,
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self._controller.async_set_auto_summer(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._controller.async_set_auto_summer(False)
        self.async_write_ha_state()
