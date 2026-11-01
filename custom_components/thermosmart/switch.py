"""Switch platform für ThermoSmart."""
from __future__ import annotations
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.restore_state import async_get as async_get_restore_data

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
        ThermoSmartAdaptSwitch(coordinator, entry),
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
    _attr_should_poll = False

    def __init__(self, coordinator: ThermoSmartCoordinator, entry: ConfigEntry):
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_active_control"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
        last = await self.async_get_last_state()
        self._coordinator.set_active_control(last is not None and last.state == "on")

    @property
    def is_on(self) -> bool:
        return self._coordinator._active_control

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "mode": "active_control" if self.is_on else "observation",
        }

    async def async_turn_on(self, **kwargs) -> None:
        self._coordinator.set_active_control(True)
        self._coordinator.request_pass(TRIGGER_SWITCH)

    async def async_turn_off(self, **kwargs) -> None:
        self._coordinator.set_active_control(False)
        self._coordinator.request_pass(TRIGGER_SWITCH)


class _LearningFlagSwitch(SwitchEntity, RestoreEntity):
    """Base for the per-zone learning switches (default ON, restored)."""
    _attr_has_entity_name = True
    _attr_should_poll = False
    _flag: str

    def __init__(self, coordinator: ThermoSmartCoordinator, entry: ConfigEntry, unique_suffix: str):
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
        last = await self.async_get_last_state()
        restored = (last.state == "on") if last is not None else self._initial_state()
        self._set(restored)

    def _initial_state(self) -> bool:
        return True

    def _set(self, value: bool) -> None:
        self._coordinator.set_learning_switches(**{self._flag: value})

    @property
    def is_on(self) -> bool:
        return getattr(self._coordinator, f"{self._flag}_enabled")

    async def async_turn_on(self, **kwargs) -> None:
        self._set(True)
        self._coordinator.request_pass(TRIGGER_SWITCH)

    async def async_turn_off(self, **kwargs) -> None:
        self._set(False)
        self._coordinator.request_pass(TRIGGER_SWITCH)


class ThermoSmartLearningSwitch(_LearningFlagSwitch):
    """"Learn room behaviour": collect observations and update the models.

    Keeps the unique id of the former single Learning switch.
    """
    _attr_translation_key = "learning"
    _attr_icon = "mdi:brain"
    _flag = "learn"

    def __init__(self, coordinator: ThermoSmartCoordinator, entry: ConfigEntry):
        super().__init__(coordinator, entry, "learning")


class ThermoSmartAdaptSwitch(_LearningFlagSwitch):
    """"Adapt control to learned behaviour": learned parameters drive control."""
    _attr_translation_key = "adapt_control"
    _attr_icon = "mdi:tune-variant"
    _flag = "adapt"

    def __init__(self, coordinator: ThermoSmartCoordinator, entry: ConfigEntry):
        super().__init__(coordinator, entry, "adapt_control")

    def _initial_state(self) -> bool:
        """First start after the switch split: inherit the former Learning switch."""
        legacy = _restored_switch_state(self.hass, f"{self._entry.entry_id}_learning")
        return True if legacy is None else legacy


def _restored_switch_state(hass: HomeAssistant, unique_id: str) -> bool | None:
    """Last stored on/off state of another ThermoSmart switch, if known."""
    entity_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, unique_id)
    if entity_id is None:
        return None
    stored = async_get_restore_data(hass).last_states.get(entity_id)
    if stored is None:
        return None
    return stored.state.state == "on"


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
