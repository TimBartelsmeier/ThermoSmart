"""WeatherEngine – Außenbedingungen aus Sensoren und/oder Wetter-Entity."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.const import UnitOfSpeed
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import SpeedConverter

from .temperature_units import is_plausible_temperature_c, to_internal_temperature_c
from .const import (
    WEATHER_COLD_THRESHOLD,
    WEATHER_MILD_THRESHOLD,
    WEATHER_WARM_THRESHOLD,
)

_LOGGER = logging.getLogger(__name__)

_VALID_SPEED_UNITS = {
    UnitOfSpeed.METERS_PER_SECOND,
    UnitOfSpeed.KILOMETERS_PER_HOUR,
    UnitOfSpeed.MILES_PER_HOUR,
    UnitOfSpeed.KNOTS,
    UnitOfSpeed.FEET_PER_SECOND,
}


def _to_wind_speed_ms(value, unit: str | None) -> float | None:
    """Normalize a wind-speed reading to m/s.

    Mirrors to_internal_temperature_c()'s unit-aware normalization. Wind
    sources report their own unit (a sensor's unit_of_measurement, or a
    weather entity's dedicated wind_speed_unit attribute) — km/h is a common
    convention for European weather stations, and the config UI explicitly
    advertises "m/s or km/h" as accepted, so WIND_THRESHOLD_MS must compare
    against an actually-normalized value, not a raw passthrough.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if unit not in _VALID_SPEED_UNITS:
        return numeric  # unrecognized/missing unit → assume already m/s
    try:
        return SpeedConverter.convert(numeric, unit, UnitOfSpeed.METERS_PER_SECOND)
    except HomeAssistantError:
        return None


class WeatherEngine:
    """Liest Außenbedingungen aus konfigurierten Sensoren (bevorzugt)
    oder aus einem HA-Wetter-Entity (Fallback).

    Priorität pro Wert:
      1. Dedizierter Sensor (eigene Wetterstation)
      2. Wetter-Entity Attribut
      3. None (unbekannt)
    """

    def __init__(
        self,
        hass: HomeAssistant,
        weather_entity: str,
        outdoor_temp_sensor: str | None = None,
        outdoor_humidity_sensor: str | None = None,
        outdoor_wind_sensor: str | None = None,
        outdoor_solar_sensor: str | None = None,
        outdoor_rain_sensor: str | None = None,
    ) -> None:
        self._hass = hass
        self._weather_entity = weather_entity
        self._temp_sensor = outdoor_temp_sensor
        self._humidity_sensor = outdoor_humidity_sensor
        self._wind_sensor = outdoor_wind_sensor
        self._solar_sensor = outdoor_solar_sensor
        self._rain_sensor = outdoor_rain_sensor

    def _read_sensor(
        self, entity_id: str | None, *, is_temperature: bool = False, is_wind: bool = False,
    ) -> float | None:
        """Einzelnen Sensor lesen. Gibt None zurück wenn unavailable.

        ``is_temperature=True`` normalizes the sensor's own unit_of_measurement
        to °C. ``is_wind=True`` normalizes it to m/s — humidity/solar/rain
        sensors are never unit-converted.
        """
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state and state.state not in ("unknown", "unavailable", "None"):
            if is_temperature:
                celsius = to_internal_temperature_c(
                    self._hass, state.state, unit=state.attributes.get("unit_of_measurement"))
                # Reject garbage/sentinel values that survive unit conversion
                # (e.g. a Zigbee error sentinel, or a misconfigured/broken
                # outdoor sensor) before they ever reach the TPI/weather-offset
                # calculation — same shared band already used for room/TRV
                # readings (temperature_units.py).
                return celsius if is_plausible_temperature_c(celsius) else None
            if is_wind:
                return _to_wind_speed_ms(state.state, state.attributes.get("unit_of_measurement"))
            try:
                return float(state.state)
            except (ValueError, TypeError):
                pass
        return None

    async def async_get_data(self) -> dict:
        """Alle Außenbedingungen als normalisiertes Dict zurückgeben."""
        data: dict = {
            "temperature": None,
            "humidity": None,
            "wind_speed": None,
            "solar_radiation": None,
            "rain": None,
            "condition": None,
            "forecast_high": None,
            "forecast_low": None,
        }

        # ── Wetter-Entity (Basis) ─────────────────────────────────────
        weather_state = self._hass.states.get(self._weather_entity)
        weather_temp_unit: str | None = None
        if weather_state and weather_state.state not in ("unknown", "unavailable"):
            attrs = weather_state.attributes
            data["condition"] = weather_state.state
            # weather.* entities expose their own display unit as a dedicated
            # "temperature_unit" attribute (distinct from a sensor's
            # unit_of_measurement) — the entity's temperature/forecast
            # values are already expressed in this unit.
            weather_temp_unit = attrs.get("temperature_unit")

            raw_humidity = attrs.get("humidity")
            if raw_humidity is not None:
                try:
                    data["humidity"] = float(raw_humidity)
                except (TypeError, ValueError):
                    pass
            data["wind_speed"] = _to_wind_speed_ms(
                attrs.get("wind_speed"), attrs.get("wind_speed_unit"))
            _weather_temp_c = to_internal_temperature_c(
                self._hass, attrs.get("temperature"), unit=weather_temp_unit)
            # Same garbage/sentinel guard as the dedicated-sensor path above —
            # a misbehaving weather integration must not feed an implausible
            # value into TPI/weather-offset just because it parsed and
            # unit-converted "successfully".
            data["temperature"] = (
                _weather_temp_c if is_plausible_temperature_c(_weather_temp_c) else None)

            # Letzter Fallback: Legacy-Forecast aus Entity-Attributen
            # (für Integrationen die den neuen Service nicht unterstützen)
            legacy_forecast = attrs.get("forecast")
            if isinstance(legacy_forecast, list) and legacy_forecast:
                try:
                    _high = to_internal_temperature_c(
                        self._hass, legacy_forecast[0].get("temperature"), unit=weather_temp_unit)
                    # Same garbage/sentinel guard as the current-temperature
                    # paths above — an implausible forecast must never reach
                    # compute_forecast_suppression() (it directly gates
                    # heating: forecast_high >= target_temp fully suppresses).
                    data["forecast_high"] = _high if is_plausible_temperature_c(_high) else None
                    _low_raw = legacy_forecast[0].get("templow")
                    if _low_raw is not None:
                        _low = to_internal_temperature_c(self._hass, _low_raw, unit=weather_temp_unit)
                        data["forecast_low"] = _low if is_plausible_temperature_c(_low) else None
                    else:
                        data["forecast_low"] = data["forecast_high"]
                except (TypeError, ValueError, AttributeError):
                    pass
        elif self._weather_entity:
            _LOGGER.debug(
                "WeatherEngine: Wetter-Entity '%s' nicht verfügbar – nur Sensordaten",
                self._weather_entity,
            )

        # ── Forecast über neue HA-API (ab 2024.3) ────────────────────
        # Neue Methode hat Vorrang vor Legacy-Attributen.
        # Timeout 10s verhindert, dass ein hängender Service den Update-Loop blockiert.
        try:
            async with asyncio.timeout(10):
                result = await self._hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"entity_id": self._weather_entity, "type": "daily"},
                    blocking=True,
                    return_response=True,
                )
            if result:
                forecast_list = result.get(self._weather_entity, {}).get("forecast", [])
                if forecast_list:
                    first = forecast_list[0]
                    # get_forecasts() returns values already converted to the
                    # entity's own display unit (same weather_temp_unit as above).
                    _high = to_internal_temperature_c(
                        self._hass, first.get("temperature"), unit=weather_temp_unit)
                    # Same garbage/sentinel guard as the legacy-forecast path
                    # above — an implausible value here must never overwrite
                    # an already-plausible legacy fallback, so it's treated
                    # exactly like "no value returned" (skip the overwrite).
                    if is_plausible_temperature_c(_high):
                        data["forecast_high"] = _high
                    _low_raw = first.get("templow")
                    if _low_raw is not None:
                        _low = to_internal_temperature_c(
                            self._hass, _low_raw, unit=weather_temp_unit)
                        if is_plausible_temperature_c(_low):
                            data["forecast_low"] = _low
                    elif data["forecast_high"] is not None:
                        data["forecast_low"] = data["forecast_high"]
        except TimeoutError:
            _LOGGER.warning(
                "WeatherEngine: Forecast-Abruf Timeout (>10s) – nutze Legacy-Daten falls verfügbar"
            )
        except Exception:
            # Service nicht verfügbar oder Entity unterstützt keinen Forecast → kein Problem
            pass

        # ── Dedizierte Sensoren überschreiben Wetter-Entity-Werte ─────
        sensor_map = {
            "temperature":    self._temp_sensor,
            "humidity":       self._humidity_sensor,
            "wind_speed":     self._wind_sensor,
            "solar_radiation": self._solar_sensor,
            "rain":           self._rain_sensor,
        }
        for key, sensor_id in sensor_map.items():
            value = self._read_sensor(
                sensor_id, is_temperature=(key == "temperature"), is_wind=(key == "wind_speed"))
            if value is not None:
                data[key] = value
                if key == "temperature":
                    _LOGGER.debug(
                        "WeatherEngine: Außentemp %.1f°C von %s", value, sensor_id
                    )

        return data

    def is_heating_season(self, weather_data: dict) -> bool:
        """True wenn Heizung wahrscheinlich benötigt wird."""
        outdoor = weather_data.get("temperature")
        if outdoor is None:
            return True
        return outdoor < WEATHER_WARM_THRESHOLD

    def get_condition_category(self, weather_data: dict) -> str:
        """Wetterkategorie: cold / mild / warm / hot."""
        outdoor = weather_data.get("temperature")
        if outdoor is None:
            return "unknown"
        if outdoor < WEATHER_COLD_THRESHOLD:
            return "cold"
        if outdoor < WEATHER_MILD_THRESHOLD:
            return "mild"
        if outdoor < WEATHER_WARM_THRESHOLD:
            return "warm"
        return "hot"
