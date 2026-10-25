"""System-wide state shared by all zones: vacation, summer (forced or detected)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_SUMMER_OFF_THRESHOLD,
    CONF_SUMMER_ON_THRESHOLD,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    SEASON_HOURS,
    SEASON_SAMPLE_MINUTES,
    SUMMER_THRESHOLD,
    WINTER_THRESHOLD,
)
from .suspension import SummerDetector
from .triggers import TRIGGER_SYSTEM_STATE
from .outdoor import OutdoorConditions

_LOGGER = logging.getLogger(__name__)

SYSTEM_KEY = "system"
_NON_ZONE_KEYS = {"learning_engine", SYSTEM_KEY, "schedule_write_lock"}


def iter_zone_coordinators(hass: HomeAssistant) -> Iterator:
    for key, data in hass.data.get(DOMAIN, {}).items():
        if key in _NON_ZONE_KEYS or not isinstance(data, dict):
            continue
        if coordinator := data.get("coordinator"):
            yield coordinator


def thresholds_from_cfg(cfg: dict) -> tuple[float, float]:
    on = float(cfg.get(CONF_SUMMER_ON_THRESHOLD, SUMMER_THRESHOLD))
    off = float(cfg.get(CONF_SUMMER_OFF_THRESHOLD, WINTER_THRESHOLD))
    return on, off


class SystemController:
    """Holds the three global switches' state and pushes the result to every zone.

    Summer is active when forced, or when automatic detection is enabled and the
    detector reports summer. Detection needs an outdoor temperature (the System entry's
    own sensor, or its weather entity as fallback); without one it never enables summer.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        cfg = {**entry.data, **entry.options}
        on, off = thresholds_from_cfg(cfg)
        self.detector = SummerDetector(
            on, off,
            window=timedelta(hours=SEASON_HOURS),
        )
        self.outdoor = OutdoorConditions(
            hass,
            cfg.get(CONF_WEATHER_ENTITY) or None,
            outdoor_temp_sensor=cfg.get(CONF_OUTDOOR_TEMP_SENSOR) or None,
        )
        self.has_outdoor_source = bool(
            cfg.get(CONF_OUTDOOR_TEMP_SENSOR) or cfg.get(CONF_WEATHER_ENTITY)
        )
        self.vacation = False
        self.force_summer = False
        self.auto_summer = True
        self.outdoor_temperature: float | None = None
        self._unsub = None
        self._no_data_since: datetime | None = None

    # ── derived state ────────────────────────────────────────────────

    @property
    def detected_summer(self) -> bool:
        return self.detector.summer

    @property
    def summer_active(self) -> bool:
        return self.force_summer or (self.auto_summer and self.detector.summer)

    @property
    def summer_source(self) -> str | None:
        if self.force_summer:
            return "forced"
        if self.auto_summer and self.detector.summer:
            return "detected"
        return None

    # ── lifecycle ────────────────────────────────────────────────────

    async def async_start(self) -> None:
        if self.has_outdoor_source:
            await self.async_sample()
            self._unsub = async_track_time_interval(
                self.hass, self._async_sample_cb, timedelta(minutes=SEASON_SAMPLE_MINUTES)
            )

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _async_sample_cb(self, _now) -> None:
        await self.async_sample()

    async def async_sample(self, now: datetime | None = None) -> None:
        """Read the outdoor temperature once and feed the detector."""
        now = now or datetime.now(timezone.utc)
        before = self.summer_active
        data = self.outdoor.read()
        temperature = data.get("temperature")
        self.outdoor_temperature = temperature
        if temperature is None:
            self._handle_no_data(now)
        else:
            self._no_data_since = None
            self.detector.add_sample(now, temperature)
        if self.summer_active != before:
            _LOGGER.warning(
                "ThermoSmart: summer mode %s (detected from %.1f °C 72 h average)",
                "ON" if self.summer_active else "OFF", self.detector.average or float("nan"),
            )
            await self.async_push()

    def _handle_no_data(self, now: datetime) -> None:
        """Without any reading for the whole window, detected summer must not stick."""
        if self._no_data_since is None:
            self._no_data_since = now
            _LOGGER.warning(
                "ThermoSmart: no outdoor temperature available for automatic summer detection"
            )
        elif self.detector.summer and now - self._no_data_since >= self.detector.window:
            _LOGGER.warning(
                "ThermoSmart: no outdoor temperature for %d h – detected summer mode reset",
                SEASON_HOURS,
            )
            self.detector.summer = False

    # ── setters (called by the switch entities) ──────────────────────

    async def async_set_vacation(self, value: bool) -> None:
        self.vacation = bool(value)
        await self.async_push()

    async def async_set_force_summer(self, value: bool) -> None:
        self.force_summer = bool(value)
        await self.async_push()

    async def async_set_auto_summer(self, value: bool) -> None:
        self.auto_summer = bool(value)
        await self.async_push()

    def restore_detected_summer(self, value: bool) -> None:
        self.detector.summer = bool(value)

    # ── distribution ─────────────────────────────────────────────────

    def apply_to(self, coordinator) -> bool:
        return coordinator.set_system_state(vacation=self.vacation, summer=self.summer_active)

    async def async_push(self) -> None:
        """Hand the current state to every zone; zones refresh when it changed."""
        for coordinator in iter_zone_coordinators(self.hass):
            changed = coordinator.set_system_state(
                vacation=self.vacation, summer=self.summer_active
            )
            if changed:
                coordinator.request_pass(TRIGGER_SYSTEM_STATE)
