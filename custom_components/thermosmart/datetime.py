"""Date/time entities for ThermoSmart zones."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, VERSION
from .coordinator import ThermoSmartCoordinator
from .triggers import TRIGGER_SCHEDULING


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.data.get("entry_type") == "system":
        return
    coordinator: ThermoSmartCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([ThermoSmartSchedulingDisabledUntil(coordinator, entry)])


class ThermoSmartSchedulingDisabledUntil(DateTimeEntity, RestoreEntity):
    """Pause schedule events until a local date and time (they are skipped, not caught up)."""

    _attr_has_entity_name = True
    _attr_translation_key = "scheduling_disabled_until"
    _attr_icon = "mdi:calendar-remove"

    def __init__(self, coordinator: ThermoSmartCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_scheduling_disabled_until"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"ThermoSmart – {entry.data.get('name', 'Zone')}",
            manufacturer="ThermoSmart",
            model="Self-learning Heating Controller",
            sw_version=VERSION,
            entry_type="service",  # type: ignore[arg-type]
        )
        self._value: datetime | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in ("unknown", "unavailable"):
            parsed = dt_util.parse_datetime(last.state)
            self._value = dt_util.as_local(parsed) if parsed else None
        self._coordinator.set_scheduling_disabled_until(self._value)

    @property
    def native_value(self) -> datetime | None:
        return self._value

    async def async_set_value(self, value: datetime) -> None:
        self._value = dt_util.as_local(value)
        self._coordinator.set_scheduling_disabled_until(self._value)
        self.async_write_ha_state()
        self._coordinator.request_pass(TRIGGER_SCHEDULING)
