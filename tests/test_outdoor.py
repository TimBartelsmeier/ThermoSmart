"""Outdoor conditions: sensor first, weather entity per-value fallback, no forecast."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.thermosmart.outdoor import OUTDOOR_KEYS, OutdoorConditions


def _state(value, **attrs) -> MagicMock:
    st = MagicMock()
    st.state = str(value)
    st.attributes = attrs
    return st


def _weather(temperature=None, **attrs) -> MagicMock:
    if temperature is not None:
        attrs["temperature"] = temperature
    return _state("sunny", **attrs)


def _reader(states: dict, weather_entity="weather.home", **sensors) -> OutdoorConditions:
    hass = MagicMock()
    hass.config.units.temperature_unit = "°C"
    hass.states.get = MagicMock(side_effect=lambda eid: states.get(eid))
    return OutdoorConditions(hass, weather_entity, **sensors)


class TestTemperaturePlausibility:
    @pytest.mark.parametrize("value, unit", [(200.0, "°C"), (-100.0, "°C"), (451.0, "°F")])
    def test_implausible_sensor_value_is_ignored(self, value, unit):
        data = _reader(
            {"sensor.t": _state(value, unit_of_measurement=unit)}, outdoor_temp_sensor="sensor.t",
        ).read()
        assert data["temperature"] is None

    @pytest.mark.parametrize("value, unit, expected", [
        (-10.0, "°C", -10.0), (35.0, "°C", 35.0), (68.0, "°F", 20.0),
    ])
    def test_plausible_sensor_value_is_kept(self, value, unit, expected):
        data = _reader(
            {"sensor.t": _state(value, unit_of_measurement=unit)}, outdoor_temp_sensor="sensor.t",
        ).read()
        assert data["temperature"] == pytest.approx(expected, abs=0.1)

    def test_implausible_weather_entity_temperature_is_ignored(self):
        data = _reader({"weather.home": _weather(200.0, temperature_unit="°C")}).read()
        assert data["temperature"] is None

    def test_weather_entity_temperature_unit_attribute_is_used(self):
        data = _reader({"weather.home": _weather(
            68.0, temperature_unit="°F", humidity=40, wind_speed=5)}).read()
        assert data["temperature"] == pytest.approx(20.0, abs=0.1)
        assert data["humidity"] == pytest.approx(40)  # never unit-converted
        assert data["wind_speed"] == pytest.approx(5)


class TestWindUnits:
    @pytest.mark.parametrize("value, unit, expected", [
        (36.0, "km/h", 10.0), (7.5, "m/s", 7.5), (6.0, None, 6.0),
    ])
    def test_sensor(self, value, unit, expected):
        attrs = {"unit_of_measurement": unit} if unit else {}
        data = _reader({"sensor.w": _state(value, **attrs)}, outdoor_wind_sensor="sensor.w").read()
        assert data["wind_speed"] == pytest.approx(expected)

    def test_weather_entity(self):
        data = _reader({"weather.home": _weather(wind_speed=36.0, wind_speed_unit="km/h")}).read()
        assert data["wind_speed"] == pytest.approx(10.0)


SENSORS = {
    "temperature": ("outdoor_temp_sensor", "sensor.temp", "5.0", 5.0),
    "humidity": ("outdoor_humidity_sensor", "sensor.hum", "70", 70.0),
    "wind_speed": ("outdoor_wind_sensor", "sensor.wind", "3", 3.0),
    "solar_radiation": ("outdoor_solar_sensor", "sensor.sun", "400", 400.0),
    "rain": ("outdoor_rain_sensor", "sensor.rain", "1.5", 1.5),
}
WEATHER_VALUES = {"temperature": 11.0, "humidity": 55.0, "wind_speed": 8.0}


class TestPerValueFallback:
    @pytest.mark.parametrize("key", list(SENSORS))
    @pytest.mark.parametrize("sensor_state", ["ok", "unavailable", "unknown", "garbage", "absent"])
    @pytest.mark.parametrize("weather", ["ok", "absent"])
    def test_matrix(self, key, sensor_state, weather):
        kwarg, entity, raw, parsed = SENSORS[key]
        states = {}
        if sensor_state == "ok":
            states[entity] = _state(raw)
        elif sensor_state != "absent":
            states[entity] = _state(sensor_state)
        if weather == "ok":
            states["weather.home"] = _weather(
                WEATHER_VALUES["temperature"], humidity=55, wind_speed=8)
        reader = _reader(states, weather_entity="weather.home" if weather == "ok" else None,
                         **{kwarg: entity})
        data = reader.read()

        if sensor_state == "ok":
            assert data[key] == pytest.approx(parsed)
            assert data["sources"][key] == "sensor"
        elif weather == "ok" and key in WEATHER_VALUES:
            assert data[key] == pytest.approx(WEATHER_VALUES[key])
            assert data["sources"][key] == "weather"
        else:
            assert data[key] is None
            assert data["sources"][key] is None

    def test_values_are_resolved_independently(self):
        states = {
            "sensor.temp": _state("2.0"),
            "sensor.hum": _state("unavailable"),
            "weather.home": _weather(11.0, humidity=55, wind_speed=8),
        }
        data = _reader(
            states, outdoor_temp_sensor="sensor.temp", outdoor_humidity_sensor="sensor.hum",
        ).read()
        assert (data["temperature"], data["humidity"], data["wind_speed"]) == (2.0, 55.0, 8.0)
        assert data["sources"]["temperature"] == "sensor"
        assert data["sources"]["humidity"] == "weather"

    def test_sensor_recovering_is_used_again(self):
        states = {"sensor.temp": _state("unavailable"), "weather.home": _weather(11.0)}
        reader = _reader(states, outdoor_temp_sensor="sensor.temp")
        assert reader.read()["temperature"] == 11.0
        states["sensor.temp"] = _state("2.0")
        assert reader.read()["temperature"] == 2.0

    def test_unavailable_weather_entity_is_not_an_error(self):
        data = _reader({"weather.home": _state("unavailable")}).read()
        assert data["condition"] is None and data["temperature"] is None


class TestNoOutdoorData:
    def test_nothing_configured_gives_none_everywhere(self):
        reader = _reader({}, weather_entity=None)
        data = reader.read()
        assert all(data[key] is None for key in OUTDOOR_KEYS)
        assert data["condition"] is None
        assert not reader.has_sensor and not reader.has_weather_entity
        assert not reader.has_temperature_source

    def test_source_flags(self):
        assert _reader({}, outdoor_temp_sensor="sensor.t").has_temperature_source
        assert _reader({}, weather_entity="weather.home").has_temperature_source
        assert not _reader({}, weather_entity=None, outdoor_rain_sensor="sensor.r"
                           ).has_temperature_source


class TestNoForecast:
    def test_no_service_call_and_no_forecast_keys(self):
        states = {"weather.home": _weather(10.0, forecast=[{"temperature": 25.0}])}
        reader = _reader(states)
        data = reader.read()
        reader._hass.services.async_call.assert_not_called()
        assert not any("forecast" in key for key in data)
        assert data["temperature"] == 10.0
