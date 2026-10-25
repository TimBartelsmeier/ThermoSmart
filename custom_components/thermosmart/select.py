"""Select platform für ThermoSmart."""
from __future__ import annotations
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, VERSION, PRESETS
from .coordinator import ThermoSmartCoordinator
from .triggers import TRIGGER_TARGET_CHANGE

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ThermoSmartCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([ThermoSmartModeSelect(coordinator, entry)])


class ThermoSmartModeSelect(SelectEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "preset"
    _attr_icon = "mdi:home-thermometer-outline"
    _attr_options = list(PRESETS)

    def __init__(self, coordinator: ThermoSmartCoordinator, entry: ConfigEntry):
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"ThermoSmart – {entry.data.get('name', 'Zone')}",
            manufacturer="ThermoSmart",
            model="Self-learning Heating Controller",
            sw_version=VERSION,
            entry_type="service",  # type: ignore[arg-type]
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The climate entity restores the desired target and preset; this select
        # only mirrors the coordinator and updates whenever the target changes.
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Coordinator hat Daten aktualisiert → State neu schreiben."""
        self.async_write_ha_state()

    @property
    def current_option(self) -> str | None:
        """The active preset, or None after a manual temperature change."""
        return self._coordinator.target.preset

    async def async_select_option(self, option: str) -> None:
        if option not in PRESETS:
            return
        self._coordinator.set_preset(option)
        self.async_write_ha_state()
        self._coordinator.request_pass(TRIGGER_TARGET_CHANGE)
