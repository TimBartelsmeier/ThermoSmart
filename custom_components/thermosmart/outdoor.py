"""Outdoor conditions: own sensors first, the weather entity as per-value fallback.

Only *current* conditions are read; forecasts are not used anywhere. Every value
is resolved on its own: a configured sensor wins while it delivers a usable
reading, otherwise the weather entity's current value is used, otherwise the
value is ``None``. Callers must cope with ``None`` (control and learning work
without any outdoor data).
"""
from __future__ import annotations

import logging

from homeassistant.const import UnitOfSpeed
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import SpeedConverter

from .temperature_units import is_plausible_temperature_c, to_internal_temperature_c

_LOGGER = logging.getLogger(__name__)

_VALID_SPEED_UNITS = {
    UnitOfSpeed.METERS_PER_SECOND,
    UnitOfSpeed.KILOMETERS_PER_HOUR,
    UnitOfSpeed.MILES_PER_HOUR,
    UnitOfSpeed.KNOTS,
    UnitOfSpeed.FEET_PER_SECOND,
}

_UNUSABLE = ("unknown", "unavailable", "None")

# Keys of the dict returned by OutdoorConditions.read(), in a stable order.
OUTDOOR_KEYS = ("temperature", "humidity", "wind_speed", "solar_radiation", "rain")


def _to_wind_speed_ms(value, unit: str | None) -> float | None:
    """Normalize a wind-speed reading to m/s (unknown unit: assumed m/s)."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if unit not in _VALID_SPEED_UNITS:
        return numeric
    try:
        return SpeedConverter.convert(numeric, unit, UnitOfSpeed.METERS_PER_SECOND)
    except HomeAssistantError:
        return None


class OutdoorConditions:
    """Reads the outdoor view of a zone (or of the System entry)."""

    def __init__(
        self,
        hass: HomeAssistant,
        weather_entity: str | None,
        outdoor_temp_sensor: str | None = None,
        outdoor_humidity_sensor: str | None = None,
        outdoor_wind_sensor: str | None = None,
        outdoor_solar_sensor: str | None = None,
        outdoor_rain_sensor: str | None = None,
    ) -> None:
        self._hass = hass
        self._weather_entity = weather_entity or None
        self._sensors = {
            "temperature": outdoor_temp_sensor,
            "humidity": outdoor_humidity_sensor,
            "wind_speed": outdoor_wind_sensor,
            "solar_radiation": outdoor_solar_sensor,
            "rain": outdoor_rain_sensor,
        }

    @property
    def has_sensor(self) -> bool:
        return any(self._sensors.values())

    @property
    def has_weather_entity(self) -> bool:
        return bool(self._weather_entity)

    @property
    def has_temperature_source(self) -> bool:
        return bool(self._sensors["temperature"] or self._weather_entity)

    def _read_sensor(
        self, entity_id: str | None, *, is_temperature: bool = False, is_wind: bool = False,
    ) -> float | None:
        """One sensor reading; None when absent, unavailable or implausible."""
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if not state or state.state in _UNUSABLE:
            return None
        if is_temperature:
            celsius = to_internal_temperature_c(
                self._hass, state.state, unit=state.attributes.get("unit_of_measurement"))
            return celsius if is_plausible_temperature_c(celsius) else None
        if is_wind:
            return _to_wind_speed_ms(state.state, state.attributes.get("unit_of_measurement"))
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _read_weather_entity(self) -> dict:
        """Current conditions of the weather entity (all None when unusable)."""
        values: dict = {key: None for key in OUTDOOR_KEYS}
        values["condition"] = None
        if not self._weather_entity:
            return values
        state = self._hass.states.get(self._weather_entity)
        if not state or state.state in ("unknown", "unavailable"):
            _LOGGER.debug(
                "Outdoor: weather entity '%s' unavailable – using sensors only",
                self._weather_entity,
            )
            return values
        attrs = state.attributes
        values["condition"] = state.state
        raw_humidity = attrs.get("humidity")
        if raw_humidity is not None:
            try:
                values["humidity"] = float(raw_humidity)
            except (TypeError, ValueError):
                pass
        values["wind_speed"] = _to_wind_speed_ms(
            attrs.get("wind_speed"), attrs.get("wind_speed_unit"))
        celsius = to_internal_temperature_c(
            self._hass, attrs.get("temperature"), unit=attrs.get("temperature_unit"))
        values["temperature"] = celsius if is_plausible_temperature_c(celsius) else None
        return values

    def read(self) -> dict:
        """Outdoor values (°C, %, m/s, W/m², rain) plus the weather ``condition``.

        Missing values are ``None``; ``sources`` names where each value came from
        (``sensor``, ``weather`` or ``None``) for transparency.
        """
        fallback = self._read_weather_entity()
        data: dict = {"condition": fallback["condition"]}
        sources: dict = {}
        for key in OUTDOOR_KEYS:
            value = self._read_sensor(
                self._sensors[key], is_temperature=key == "temperature", is_wind=key == "wind_speed",
            )
            if value is not None:
                data[key], sources[key] = value, "sensor"
            elif fallback[key] is not None:
                data[key], sources[key] = fallback[key], "weather"
            else:
                data[key], sources[key] = None, None
        data["sources"] = sources
        return data
