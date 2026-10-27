"""Config flow für ThermoSmart."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, selector

from .device_profiles import get_preset
from .schedule import ScheduleConflictError, ScheduleError, parse_events
from .const import (
    DOMAIN,
    CONF_ZONE_CONTROL_MODE,
    CONF_CONTROL_INTERVAL,
    CONF_TEMP_TOLERANCE,
    CONF_EXT_TEMP_THRESHOLD,
    CONF_TRV_SETPOINT_DEBOUNCE,
    CONF_TRV_SETPOINT_SETTLING,
    CONF_DEVICE_SETTINGS,
    CONF_DEV_CONTROL_MODE,
    CONF_DEV_VALVE_ENTITY,
    CONF_DEV_FEED_EXT_TEMP,
    CONF_DEV_EXT_TEMP_ENTITY,
    CONF_DEV_CALIBRATION_ENTITY,
    CONF_DEV_CALIBRATION_SYNC,
    CONF_DEV_CALIBRATION_INVERT,
    CONF_DEV_VALVE_MAINTENANCE,
    CONF_DEV_WINDOW_OFF,
    CONF_DEV_ACCEPT_SETPOINT_CHANGES,
    CONF_DEV_STATE_SETTINGS,
    CONF_SCHEDULE_EVENTS,
    CONTROL_MODE_INHERIT,
    CONTROL_MODE_SETPOINT,
    CONTROL_MODE_TPI_BOOST,
    CONTROL_MODE_DIRECT_VALVE,
    AUTO_VALVE_PATTERNS,
    AUTO_EXT_TEMP_PATTERNS,
    AUTO_CALIBRATION_PATTERNS,
    CONF_WEATHER_ENTITY,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_OUTDOOR_HUMIDITY_SENSOR,
    CONF_OUTDOOR_WIND_SENSOR,
    CONF_OUTDOOR_SOLAR_SENSOR,
    CONF_OUTDOOR_RAIN_SENSOR,
    CONF_VACATION_TEMP,
    CONF_WINDOW_OPEN_TEMP,
    CONF_ECO_TEMP,
    CONF_SUMMER_ON_THRESHOLD,
    CONF_SUMMER_OFF_THRESHOLD,
    SUMMER_THRESHOLD,
    WINTER_THRESHOLD,
    TEMP_ECO,
    WINDOW_OPEN_SETPOINT,
)


# ── Hilfklasse: EntitySelector der None und "" als "nicht gesetzt" akzeptiert ─

class _NullableEntitySelector(selector.EntitySelector):
    """EntitySelector that treats None and '' as unset (returns None, no error).

    HA 2026.x sends null for empty optional entity pickers and auto-validates
    data_schema before calling the step handler. The standard EntitySelector
    rejects None via cv.entity_id_or_uuid(None). This subclass short-circuits
    that path while remaining a proper EntitySelector for frontend serialization.
    """
    def __call__(self, data):
        if data is None or data == "":
            return None
        return super().__call__(data)


# ── Validierung ─────────────────────────────────────────────────────────────

def _validate_temps(data: dict) -> dict[str, str]:
    """Cross-field temperature sanity check.

    Rules:
      - night_temp  must be strictly less than comfort_temp
        (night >= comfort would invert the day/night schedule)
      - away_temp   must not exceed night_temp
        (away > night makes no operational sense)

    Error keys map to existing translations in strings.json / de.json:
      night_temp_too_high  →  shown on the night_temp field
      away_temp_too_high   →  shown on the away_temp field
    """
    errors: dict[str, str] = {}
    try:
        comfort = float(data.get("comfort_temp", 21.0))
        night   = float(data.get("night_temp",   18.0))
        away    = float(data.get("away_temp",    17.0))
    except (TypeError, ValueError):
        return errors   # Schema-Validierung ist bereits vorab passiert

    if night > comfort:
        errors["night_temp"] = "night_temp_too_high"
    if away > night:
        errors["away_temp"] = "away_temp_too_high"
    return errors


# ── Schema-Hilfsfunktionen (eine pro Schritt) ────────────────────────────────

def _schema_devices(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required("name", default=d.get("name", "")):
            selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)),

        vol.Required("climate_entities", default=d.get("climate_entities", [])):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="climate", multiple=True)),

        vol.Optional("temp_sensors", default=d.get("temp_sensors", [])):
            selector.EntitySelector(selector.EntitySelectorConfig(
                domain="sensor", device_class="temperature", multiple=True
            )),
        vol.Optional("humidity_sensors", default=d.get("humidity_sensors", [])):
            selector.EntitySelector(selector.EntitySelectorConfig(
                domain="sensor", device_class="humidity", multiple=True
            )),
        vol.Optional("window_sensors", default=d.get("window_sensors", [])):
            selector.EntitySelector(selector.EntitySelectorConfig(
                domain="binary_sensor", device_class=["window", "opening", "door"], multiple=True
            )),

        vol.Required("window_open_delay", default=d.get("window_open_delay", 20)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=0, max=3600, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX,
            )),
        vol.Required("window_close_delay", default=d.get("window_close_delay", 20)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=0, max=1800, step=1, unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX,
            )),
    })


def _schema_schedule(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required("comfort_temp", default=d.get("comfort_temp", 21.0)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=5, max=30, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX,
            )),
        vol.Required("night_temp", default=d.get("night_temp", 18.0)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=5, max=30, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX,
            )),
        vol.Required("away_temp", default=d.get("away_temp", 17.0)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=5, max=30, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX,
            )),
        vol.Required(CONF_VACATION_TEMP, default=d.get(CONF_VACATION_TEMP, 12.0)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=5, max=20, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX,
            )),
        vol.Required(CONF_ECO_TEMP, default=d.get(CONF_ECO_TEMP, TEMP_ECO)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=15, max=22, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX,
            )),
        vol.Required(CONF_WINDOW_OPEN_TEMP, default=d.get(CONF_WINDOW_OPEN_TEMP, WINDOW_OPEN_SETPOINT)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=5, max=20, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX,
            )),
    })


_SCHEDULE_DAYS = [
    {"value": value, "label": name}
    for value, name in zip(
        ("mon", "tue", "wed", "thu", "fri", "sat", "sun"),
        ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    )
]
_SCHEDULE_DAY_VALUES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_SCHEDULE_PRESETS = [
    {"value": preset, "label": preset.replace("_", " ").title()}
    for preset in ("comfort", "eco", "night", "away", "vacation")
]


def _schema_schedule_event(d: dict) -> vol.Schema:
    days = d.get("days", [0, 1, 2, 3, 4])
    selector_days = [
        _SCHEDULE_DAY_VALUES[day] if isinstance(day, int) and 0 <= day < 7 else day
        for day in days
    ]
    event_time = str(d.get("time", "06:00:00"))
    if len(event_time.split(":")) == 2:
        event_time = f"{event_time}:00"
    return vol.Schema({
        vol.Required("days", default=selector_days):
            selector.SelectSelector(selector.SelectSelectorConfig(
                options=_SCHEDULE_DAYS, multiple=True, mode=selector.SelectSelectorMode.LIST,
            )),
        vol.Required("time", default=event_time):
            selector.TimeSelector(),
        vol.Required("preheat", default=d.get("preheat", False)):
            selector.BooleanSelector(),
    })


def _schema_schedule_target_temperature(d: dict) -> vol.Schema:
    target = d.get("target")
    default = target if isinstance(target, (int, float)) else 21.0
    return vol.Schema({
        vol.Required("target", default=default):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=5, max=30, step=0.5, unit_of_measurement="°C",
                mode=selector.NumberSelectorMode.BOX,
            )),
    })


def _schema_schedule_target_preset(d: dict) -> vol.Schema:
    target = d.get("target")
    default = target if isinstance(target, str) else "comfort"
    return vol.Schema({
        vol.Required("target", default=default):
            selector.SelectSelector(selector.SelectSelectorConfig(
                options=_SCHEDULE_PRESETS, mode=selector.SelectSelectorMode.DROPDOWN,
            )),
    })


def _schema_system(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Optional(CONF_WEATHER_ENTITY, default=d.get(CONF_WEATHER_ENTITY) or None):
            _NullableEntitySelector(selector.EntitySelectorConfig(domain="weather")),
        vol.Optional(CONF_OUTDOOR_TEMP_SENSOR, default=d.get(CONF_OUTDOOR_TEMP_SENSOR) or None):
            _NullableEntitySelector(selector.EntitySelectorConfig(
                domain="sensor", device_class="temperature"
            )),
        vol.Required(CONF_SUMMER_ON_THRESHOLD, default=d.get(CONF_SUMMER_ON_THRESHOLD, SUMMER_THRESHOLD)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=0, max=40, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX,
            )),
        vol.Required(CONF_SUMMER_OFF_THRESHOLD, default=d.get(CONF_SUMMER_OFF_THRESHOLD, WINTER_THRESHOLD)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=0, max=40, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX,
            )),
    })


def _validate_system(hass, data: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    weather = data.get(CONF_WEATHER_ENTITY)
    if weather and hass.states.get(weather) is None:
        errors[CONF_WEATHER_ENTITY] = "entity_not_found"
    try:
        if float(data[CONF_SUMMER_OFF_THRESHOLD]) >= float(data[CONF_SUMMER_ON_THRESHOLD]):
            errors[CONF_SUMMER_OFF_THRESHOLD] = "summer_thresholds_inverted"
    except (KeyError, TypeError, ValueError):
        pass
    return errors


def _entity_options(values: list[tuple[str, str]]) -> selector.SelectSelector:
    return selector.SelectSelector(selector.SelectSelectorConfig(
        options=[{"value": value, "label": label} for value, label in values],
        mode=selector.SelectSelectorMode.DROPDOWN,
    ))


def _translated_options(values: list[str], translation_key: str) -> selector.SelectSelector:
    """Dropdown whose labels come from the integration's `selector` translations."""
    return selector.SelectSelector(selector.SelectSelectorConfig(
        options=values,
        mode=selector.SelectSelectorMode.DROPDOWN,
        translation_key=translation_key,
    ))


def _schema_control_options(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_ZONE_CONTROL_MODE, default=d.get(
            CONF_ZONE_CONTROL_MODE, CONTROL_MODE_TPI_BOOST
        )): _translated_options(
            [CONTROL_MODE_SETPOINT, CONTROL_MODE_TPI_BOOST, CONTROL_MODE_DIRECT_VALVE],
            "zone_control_mode",
        ),
        vol.Required(CONF_CONTROL_INTERVAL, default=d.get(CONF_CONTROL_INTERVAL, 5)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=2, max=15, step=1, unit_of_measurement="min",
                mode=selector.NumberSelectorMode.BOX,
            )),
        vol.Required(CONF_TEMP_TOLERANCE, default=d.get(CONF_TEMP_TOLERANCE, 0.5)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=0.1, max=2.0, step=0.1, unit_of_measurement="°C",
                mode=selector.NumberSelectorMode.BOX,
            )),
        vol.Required(CONF_EXT_TEMP_THRESHOLD, default=d.get(CONF_EXT_TEMP_THRESHOLD, 0.5)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=0.1, max=1.5, step=0.1, unit_of_measurement="°C",
                mode=selector.NumberSelectorMode.BOX,
            )),
        vol.Required(CONF_TRV_SETPOINT_DEBOUNCE, default=d.get(
            CONF_TRV_SETPOINT_DEBOUNCE, 5
        )): selector.NumberSelector(selector.NumberSelectorConfig(
            min=0, max=60, step=1, unit_of_measurement="s",
            mode=selector.NumberSelectorMode.BOX,
        )),
        vol.Required(CONF_TRV_SETPOINT_SETTLING, default=d.get(
            CONF_TRV_SETPOINT_SETTLING, 5
        )): selector.NumberSelector(selector.NumberSelectorConfig(
            min=0, max=60, step=1, unit_of_measurement="s",
            mode=selector.NumberSelectorMode.BOX,
        )),
    })


_STATE_DOMAINS = ["select", "number", "switch"]


def _schema_device_settings(detected: dict, zone_mode: str) -> vol.Schema:
    settings = detected["settings"]
    if zone_mode not in (CONTROL_MODE_SETPOINT, CONTROL_MODE_TPI_BOOST, CONTROL_MODE_DIRECT_VALVE):
        zone_mode = CONTROL_MODE_TPI_BOOST
    return vol.Schema({
        vol.Required(CONF_DEV_CONTROL_MODE, default=settings.get(
            CONF_DEV_CONTROL_MODE, CONTROL_MODE_INHERIT
        )): _translated_options(
            [CONTROL_MODE_INHERIT, CONTROL_MODE_SETPOINT, CONTROL_MODE_TPI_BOOST,
             CONTROL_MODE_DIRECT_VALVE],
            f"device_control_mode_{zone_mode}",
        ),
        vol.Optional(CONF_DEV_VALVE_ENTITY, default=settings.get(CONF_DEV_VALVE_ENTITY) or None):
            _NullableEntitySelector(selector.EntitySelectorConfig(domain="number")),
        vol.Required(CONF_DEV_FEED_EXT_TEMP, default=settings.get(
            CONF_DEV_FEED_EXT_TEMP, False
        )): selector.BooleanSelector(),
        vol.Optional(CONF_DEV_EXT_TEMP_ENTITY, default=settings.get(
            CONF_DEV_EXT_TEMP_ENTITY
        ) or None): _NullableEntitySelector(selector.EntitySelectorConfig(domain="number")),
        vol.Required(CONF_DEV_CALIBRATION_SYNC, default=settings.get(
            CONF_DEV_CALIBRATION_SYNC, False
        )): selector.BooleanSelector(),
        vol.Optional(CONF_DEV_CALIBRATION_ENTITY, default=settings.get(
            CONF_DEV_CALIBRATION_ENTITY
        ) or None): _NullableEntitySelector(selector.EntitySelectorConfig(domain="number")),
        vol.Required(CONF_DEV_CALIBRATION_INVERT, default=settings.get(
            CONF_DEV_CALIBRATION_INVERT, detected["preset"].calibration_inverted
        )): selector.BooleanSelector(),
        vol.Required(CONF_DEV_VALVE_MAINTENANCE, default=settings.get(
            CONF_DEV_VALVE_MAINTENANCE, True
        )): selector.BooleanSelector(),
        vol.Required(CONF_DEV_WINDOW_OFF, default=settings.get(
            CONF_DEV_WINDOW_OFF, False
        )): selector.BooleanSelector(),
        vol.Required(CONF_DEV_ACCEPT_SETPOINT_CHANGES, default=settings.get(
            CONF_DEV_ACCEPT_SETPOINT_CHANGES, True
        )): selector.BooleanSelector(),
    })


def _friendly_name(hass, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    return state.attributes.get("friendly_name", entity_id) if state else entity_id


def _describe_state_rules(hass, rules: list[dict]) -> str:
    if not rules:
        return "_No state settings configured._"
    lines = []
    for rule in rules:
        entity_id = rule.get("entity_id", "")
        value = rule.get("value", "")
        lines.append(f"- {_friendly_name(hass, entity_id)} (`{entity_id}`) = **{value}**")
    return "\n".join(lines)


def _state_value_selector(domain: str, state) -> tuple[object, object]:
    """Build a value selector mirroring HA's own control for the entity."""
    attrs = state.attributes if state else {}
    current = state.state if state and state.state not in ("unknown", "unavailable") else None
    if domain == "switch":
        return selector.BooleanSelector(), current == "on" if current else None
    if domain == "number":
        config: dict = {"mode": selector.NumberSelectorMode.BOX,
                        "step": attrs.get("step") or "any"}
        if attrs.get("min") is not None:
            config["min"] = float(attrs["min"])
        if attrs.get("max") is not None:
            config["max"] = float(attrs["max"])
        if attrs.get("unit_of_measurement"):
            config["unit_of_measurement"] = attrs["unit_of_measurement"]
        try:
            default = float(current) if current is not None else None
        except ValueError:
            default = None
        return selector.NumberSelector(selector.NumberSelectorConfig(**config)), default
    options = list(attrs.get("options") or [])
    if options:
        return _entity_options([(o, o) for o in options]), (
            current if current in options else None
        )
    return selector.TextSelector(), current


def _detect_device_settings(hass, climate_id: str) -> dict:
    entity_reg = er.async_get(hass)
    climate_entry = entity_reg.async_get(climate_id)
    device_entry = (
        dr.async_get(hass).async_get(climate_entry.device_id)
        if climate_entry and climate_entry.device_id else None
    )
    preset = get_preset(
        device_entry.model if device_entry else None,
        device_entry.manufacturer if device_entry else None,
    )
    detected_entities: list = []
    if climate_entry and climate_entry.device_id:
        detected_entities = er.async_entries_for_device(
            entity_reg, climate_entry.device_id
        )

    def find_all(domain: str, patterns) -> list[str]:
        found = []
        for entry in detected_entities:
            if entry.entity_id.split(".", 1)[0] != domain:
                continue
            fingerprint = " ".join((
                entry.entity_id,
                getattr(entry, "translation_key", None) or "",
                getattr(entry, "original_name", None) or "",
            )).lower()
            if any(pattern.lower() in fingerprint for pattern in patterns):
                found.append(entry.entity_id)
        return found

    def find(domain: str, patterns) -> str | None:
        return next(iter(find_all(domain, patterns)), None)

    valve_entity = find("number", (
        preset.valve_entity_pattern,
    ) if preset.valve_entity_pattern else AUTO_VALVE_PATTERNS)
    ext_entity = find("number", (
        preset.ext_temp_entity_pattern,
    ) if preset.ext_temp_entity_pattern else AUTO_EXT_TEMP_PATTERNS)
    calibration_entity = find("number", AUTO_CALIBRATION_PATTERNS)

    # Preset state rules: explicit defaults first, then TRV-internal logic
    # switches (window detection, child lock, ...) that must stay off.
    state_rules: list[dict] = []
    for rule in preset.default_state_settings:
        entity_id = next(
            (e for d in _STATE_DOMAINS
             if (e := find(d, (rule["entity_pattern"],)))),
            None,
        )
        if entity_id:
            state_rules.append({"entity_id": entity_id, "value": rule["value"]})
    taken = {r["entity_id"] for r in state_rules}
    for entity_id in find_all("switch", preset.quirk_entity_patterns):
        if entity_id not in taken:
            state_rules.append({"entity_id": entity_id, "value": "off"})
    recommended_mode = preset.recommended_mode
    if valve_entity:
        recommended_mode = CONTROL_MODE_DIRECT_VALVE
    if recommended_mode == CONTROL_MODE_DIRECT_VALVE and not valve_entity:
        recommended_mode = CONTROL_MODE_TPI_BOOST
    settings = {
        CONF_DEV_CONTROL_MODE: (
            CONTROL_MODE_DIRECT_VALVE
            if recommended_mode == CONTROL_MODE_DIRECT_VALVE
            else CONTROL_MODE_INHERIT
        ),
        CONF_DEV_VALVE_ENTITY: valve_entity,
        CONF_DEV_FEED_EXT_TEMP: bool(ext_entity and preset.feed_external_temp),
        CONF_DEV_EXT_TEMP_ENTITY: ext_entity,
        CONF_DEV_CALIBRATION_ENTITY: calibration_entity,
        CONF_DEV_CALIBRATION_SYNC: bool(calibration_entity and preset.calibration_sync),
        CONF_DEV_CALIBRATION_INVERT: preset.calibration_inverted,
        CONF_DEV_VALVE_MAINTENANCE: preset.valve_maintenance,
        CONF_DEV_WINDOW_OFF: preset.window_off,
        CONF_DEV_STATE_SETTINGS: state_rules,
    }
    return {
        "preset": preset,
        "settings": settings,
        "recommended_mode": recommended_mode,
        "device": device_entry,
        "valve_entity": valve_entity,
    }


def _schema_weather(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Optional(CONF_WEATHER_ENTITY, default=d.get(CONF_WEATHER_ENTITY) or None):
            _NullableEntitySelector(selector.EntitySelectorConfig(domain="weather")),

        vol.Optional(CONF_OUTDOOR_TEMP_SENSOR, default=d.get(CONF_OUTDOOR_TEMP_SENSOR) or None):
            _NullableEntitySelector(selector.EntitySelectorConfig(
                domain="sensor", device_class="temperature"
            )),
        vol.Optional(CONF_OUTDOOR_HUMIDITY_SENSOR, default=d.get(CONF_OUTDOOR_HUMIDITY_SENSOR) or None):
            _NullableEntitySelector(selector.EntitySelectorConfig(
                domain="sensor", device_class="humidity"
            )),
        vol.Optional(CONF_OUTDOOR_WIND_SENSOR, default=d.get(CONF_OUTDOOR_WIND_SENSOR) or None):
            _NullableEntitySelector(selector.EntitySelectorConfig(
                domain="sensor", device_class="wind_speed"
            )),
        vol.Optional(CONF_OUTDOOR_SOLAR_SENSOR, default=d.get(CONF_OUTDOOR_SOLAR_SENSOR) or None):
            _NullableEntitySelector(selector.EntitySelectorConfig(
                domain="sensor", device_class="irradiance"
            )),
        vol.Optional(CONF_OUTDOOR_RAIN_SENSOR, default=d.get(CONF_OUTDOOR_RAIN_SENSOR) or None):
            _NullableEntitySelector(selector.EntitySelectorConfig(
                domain="sensor", device_class=["precipitation", "precipitation_intensity"]
            )),
    })


class _DeviceSettingsMixin:
    """Sequential per-TRV settings plus the state-settings sub-flow.

    Host flows provide the zone context via the ``_ds_*`` hooks.
    """

    _device_index: int = 0
    _pending: dict | None = None
    _rules: list | None = None
    _rule_entity: str | None = None

    def _ds_entities(self) -> list[str]:
        raise NotImplementedError

    def _ds_all_settings(self) -> dict:
        raise NotImplementedError

    def _ds_zone_mode(self) -> str:
        raise NotImplementedError

    async def _ds_finished(self):
        raise NotImplementedError

    def _ds_detected(self, eid: str) -> dict:
        detected = _detect_device_settings(self.hass, eid)
        detected["settings"] = {
            **detected["settings"], **self._ds_all_settings().get(eid, {}),
        }
        return detected

    def _ds_device_entity_ids(self, climate_id: str) -> list[str]:
        entity_reg = er.async_get(self.hass)
        entry = entity_reg.async_get(climate_id)
        if not entry or not entry.device_id:
            return []
        return [
            e.entity_id for e in er.async_entries_for_device(entity_reg, entry.device_id)
            if e.entity_id.split(".", 1)[0] in _STATE_DOMAINS
        ]

    def _ds_current_eid(self) -> str:
        return self._ds_entities()[self._device_index]

    def _ds_zone_name(self) -> str:
        raise NotImplementedError

    def _ds_placeholders(self, eid: str) -> dict:
        return {
            "zone": self._ds_zone_name(),
            "device_name": f"{_friendly_name(self.hass, eid)} ({eid})",
            "index": str(self._device_index + 1),
            "total": str(len(self._ds_entities())),
        }

    async def async_step_device_settings(self, user_input: dict | None = None):
        entities = self._ds_entities()
        if not entities:
            return await self._ds_finished()
        eid = entities[self._device_index]
        detected = self._ds_detected(eid)
        if user_input is not None:
            self._pending = dict(user_input)
            self._rules = [
                dict(rule) for rule in detected["settings"].get(CONF_DEV_STATE_SETTINGS, [])
            ]
            return await self.async_step_state_menu()

        mode = self._ds_zone_mode()
        warning = (
            "\n\n⚠️ **Warning:** Zone control mode is set to direct valve control, "
            "but no valve position entity was found for this device. Direct valve "
            "control may fail or fall back."
            if mode == CONTROL_MODE_DIRECT_VALVE
            and not detected["settings"].get(CONF_DEV_VALVE_ENTITY)
            else ""
        )
        device = detected["device"]
        header = (
            f"Device identified as {device.manufacturer or ''} {device.model or ''}. "
            f"A device preset was loaded.\n\n{detected['preset'].info_summary}"
            if device and detected["preset"].preset_id != "generic"
            else "Generic device. Available inputs are auto-detected."
        )
        return self.async_show_form(
            step_id="device_settings",
            data_schema=_schema_device_settings(detected, mode),
            description_placeholders={
                **self._ds_placeholders(eid), "preset_info": header + warning,
            },
        )

    # ── State settings sub-flow ──────────────────────────────────────

    def _set_rule(self, entity_id: str, value) -> None:
        self._rules = [r for r in (self._rules or []) if r.get("entity_id") != entity_id]
        self._rules.append({"entity_id": entity_id, "value": value})

    async def async_step_state_menu(self, user_input: dict | None = None):
        eid = self._ds_current_eid()
        options = ["state_add_device", "state_add_any"]
        if self._rules:
            options.append("state_remove")
        options.append("state_done")
        return self.async_show_menu(
            step_id="state_menu",
            menu_options=options,
            description_placeholders={
                **self._ds_placeholders(eid),
                "state_rules": _describe_state_rules(self.hass, self._rules or []),
            },
        )

    async def _state_pick(self, step_id: str, user_input: dict | None, restrict: bool):
        eid = self._ds_current_eid()
        if user_input is not None:
            if not user_input.get("entity_id"):
                return await self.async_step_state_menu()
            self._rule_entity = user_input["entity_id"]
            return await self.async_step_state_value()
        config: dict = {"domain": _STATE_DOMAINS}
        candidates = [
            c for c in self._ds_device_entity_ids(eid)
        ] if restrict else []
        if candidates:
            config["include_entities"] = candidates
        scope = (
            "Showing entities of this TRV's device."
            if candidates else
            "Showing all select, number and switch entities."
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({
                vol.Optional("entity_id"): selector.EntitySelector(
                    selector.EntitySelectorConfig(**config)
                ),
            }),
            description_placeholders={**self._ds_placeholders(eid), "scope": scope},
        )

    async def async_step_state_add_device(self, user_input: dict | None = None):
        return await self._state_pick("state_add_device", user_input, True)

    async def async_step_state_add_any(self, user_input: dict | None = None):
        return await self._state_pick("state_add_any", user_input, False)

    async def async_step_state_value(self, user_input: dict | None = None):
        entity_id = self._rule_entity
        domain = entity_id.split(".", 1)[0]
        if user_input is not None:
            if user_input.get("cancel"):
                return await self.async_step_state_menu()
            value = user_input["value"]
            if domain == "switch":
                value = "on" if value else "off"
            elif domain == "number":
                value = float(value)
            else:
                value = str(value)
            self._set_rule(entity_id, value)
            return await self.async_step_state_menu()
        state = self.hass.states.get(entity_id)
        value_selector, default = _state_value_selector(domain, state)
        existing = next(
            (r["value"] for r in self._rules or [] if r.get("entity_id") == entity_id), None
        )
        if existing is not None:
            default = existing == "on" if domain == "switch" else existing
        key = vol.Required("value") if default is None else vol.Required("value", default=default)
        return self.async_show_form(
            step_id="state_value",
            data_schema=vol.Schema({
                key: value_selector,
                vol.Optional("cancel", default=False): selector.BooleanSelector(),
            }),
            description_placeholders={
                **self._ds_placeholders(self._ds_current_eid()),
                "entity": f"{_friendly_name(self.hass, entity_id)} ({entity_id})",
                "current": state.state if state else "unavailable",
            },
        )

    async def async_step_state_remove(self, user_input: dict | None = None):
        eid = self._ds_current_eid()
        if user_input is not None:
            gone = set(user_input.get("remove", []))
            self._rules = [r for r in (self._rules or []) if r.get("entity_id") not in gone]
            return await self.async_step_state_menu()
        labels = []
        for rule in self._rules or []:
            entity_id = rule["entity_id"]
            labels.append((
                entity_id, f"{_friendly_name(self.hass, entity_id)} = {rule['value']}"
            ))
        return self.async_show_form(
            step_id="state_remove",
            data_schema=vol.Schema({
                vol.Optional("remove", default=[]): selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[{"value": v, "label": l} for v, l in labels],
                    multiple=True, mode=selector.SelectSelectorMode.LIST,
                )),
            }),
            description_placeholders=self._ds_placeholders(eid),
        )

    async def async_step_state_done(self, user_input: dict | None = None):
        eid = self._ds_current_eid()
        settings = dict(self._pending or {})
        settings[CONF_DEV_STATE_SETTINGS] = list(self._rules or [])
        self._ds_all_settings()[eid] = settings
        self._device_index += 1
        self._pending = None
        self._rules = None
        if self._device_index >= len(self._ds_entities()):
            self._device_index = 0
            return await self._ds_finished()
        return await self.async_step_device_settings()


# ── Shared zone steps (setup and options) ────────────────────────────────────

ZONE_STEPS = (
    "devices", "control_options", "device_settings", "temperatures", "schedule", "outdoor"
)


def _validate_outdoor(hass, data: dict) -> dict[str, str]:
    weather = data.get(CONF_WEATHER_ENTITY, "")
    if weather and hass.states.get(weather) is None:
        return {CONF_WEATHER_ENTITY: "entity_not_found"}
    return {}


class _ScheduleEditorMixin:
    """In-flow CRUD editor for a zone's recurring schedule events."""

    _schedule_events_cache: list[dict] | None = None
    _schedule_pending: dict | None = None
    _schedule_edit_index: int | None = None

    def _schedule_events(self) -> list[dict]:
        if self._schedule_events_cache is None:
            cfg = self._zone_data()
            parsed = parse_events(cfg.get(CONF_SCHEDULE_EVENTS, []))
            self._schedule_events_cache = [
                {
                    "days": list(event.days),
                    "time": f"{event.minute // 60:02d}:{event.minute % 60:02d}",
                    "target": event.target,
                    "preheat": event.preheat,
                }
                for event in parsed
            ]
        return self._schedule_events_cache

    @staticmethod
    def _schedule_event_label(event: dict) -> str:
        names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        days = ", ".join(names[day] for day in event["days"])
        target = event["target"]
        if isinstance(target, str):
            target = target.title()
        else:
            target = f"{target:g} °C"
        preheat = ", pre-heat" if event["preheat"] else ""
        return f"{days} {event['time']} → {target}{preheat}"

    def _schedule_description(self) -> str:
        events = self._schedule_events()
        return "\n".join(
            f"{index + 1}. {self._schedule_event_label(event)}"
            for index, event in enumerate(events)
        ) or "_No schedule events configured._"

    async def async_step_schedule(self, user_input: dict | None = None):
        self._schedule_events_cache = None
        self._schedule_pending = None
        self._schedule_edit_index = None
        self._schedule_events()
        return await self.async_step_schedule_menu()

    async def async_step_schedule_menu(self, user_input: dict | None = None):
        options = ["schedule_add"]
        if self._schedule_events():
            options.extend(("schedule_edit", "schedule_remove"))
        options.append("schedule_done")
        return self.async_show_menu(
            step_id="schedule",
            menu_options=options,
            description_placeholders={"events": self._schedule_description()},
        )

    async def async_step_schedule_add(self, user_input: dict | None = None):
        self._schedule_edit_index = None
        self._schedule_pending = None
        return await self.async_step_schedule_event(user_input)

    async def async_step_schedule_edit(self, user_input: dict | None = None):
        events = self._schedule_events()
        if user_input is not None:
            try:
                index = int(user_input["event"])
            except (KeyError, TypeError, ValueError):
                return await self.async_step_schedule_menu()
            if not 0 <= index < len(events):
                return await self.async_step_schedule_menu()
            self._schedule_edit_index = index
            self._schedule_pending = dict(events[index])
            return await self.async_step_schedule_event()
        return self.async_show_form(
            step_id="schedule_edit",
            data_schema=vol.Schema({
                vol.Required("event"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": str(i), "label": self._schedule_event_label(event)}
                            for i, event in enumerate(events)
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
        )

    async def async_step_schedule_event(self, user_input: dict | None = None):
        current = self._schedule_pending or {}
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                draft = {
                    "days": user_input["days"],
                    "time": user_input["time"],
                    "target": current.get("target", 21.0),
                    "preheat": user_input["preheat"],
                }
                remaining = [
                    event for i, event in enumerate(self._schedule_events())
                    if i != self._schedule_edit_index
                ]
                parse_events([*remaining, draft])
                # Store canonical weekday/time values before moving to the target step.
                normalized = parse_events([draft])[0]
                draft["days"] = list(normalized.days)
                draft["time"] = (
                    f"{normalized.minute // 60:02d}:{normalized.minute % 60:02d}"
                )
                self._schedule_pending = draft
                return await self.async_step_schedule_target_type()
            except ScheduleConflictError:
                errors["base"] = "schedule_conflict"
            except ScheduleError:
                errors["base"] = "schedule_invalid"
        return self.async_show_form(
            step_id="schedule_event",
            data_schema=_schema_schedule_event({**current, **(user_input or {})}),
            errors=errors,
        )

    async def async_step_schedule_target_type(self, user_input: dict | None = None):
        target = (self._schedule_pending or {}).get("target")
        return self.async_show_menu(
            step_id="schedule_target_type",
            menu_options=["schedule_target_temperature", "schedule_target_preset"],
            description_placeholders={"current_target": str(target) if target is not None else ""},
        )

    async def _schedule_save_target(
        self, user_input: dict, target_step: str
    ):
        pending = dict(self._schedule_pending or {})
        pending["target"] = user_input["target"]
        candidate = list(self._schedule_events())
        if self._schedule_edit_index is None:
            candidate.append(pending)
        else:
            candidate[self._schedule_edit_index] = pending
        try:
            parsed = parse_events(candidate)
        except ScheduleConflictError:
            schema = (
                _schema_schedule_target_preset(pending)
                if target_step == "schedule_target_preset"
                else _schema_schedule_target_temperature(pending)
            )
            return self.async_show_form(
                step_id=target_step,
                data_schema=schema,
                errors={"base": "schedule_conflict"},
            )
        except ScheduleError:
            schema = (
                _schema_schedule_target_preset(pending)
                if target_step == "schedule_target_preset"
                else _schema_schedule_target_temperature(pending)
            )
            return self.async_show_form(
                step_id=target_step,
                data_schema=schema,
                errors={"base": "schedule_invalid"},
            )
        self._schedule_events_cache = [
            {
                "days": list(event.days),
                "time": f"{event.minute // 60:02d}:{event.minute % 60:02d}",
                "target": event.target,
                "preheat": event.preheat,
            }
            for event in parsed
        ]
        self._schedule_pending = None
        self._schedule_edit_index = None
        return await self.async_step_schedule_menu()

    async def async_step_schedule_target_temperature(self, user_input: dict | None = None):
        if user_input is not None:
            return await self._schedule_save_target(user_input, "schedule_target_temperature")
        return self.async_show_form(
            step_id="schedule_target_temperature",
            data_schema=_schema_schedule_target_temperature(self._schedule_pending or {}),
        )

    async def async_step_schedule_target_preset(self, user_input: dict | None = None):
        if user_input is not None:
            return await self._schedule_save_target(user_input, "schedule_target_preset")
        return self.async_show_form(
            step_id="schedule_target_preset",
            data_schema=_schema_schedule_target_preset(self._schedule_pending or {}),
        )

    async def async_step_schedule_remove(self, user_input: dict | None = None):
        events = self._schedule_events()
        if user_input is not None:
            remove = {int(index) for index in user_input.get("remove", [])}
            self._schedule_events_cache = [
                event for index, event in enumerate(events) if index not in remove
            ]
            return await self.async_step_schedule_menu()
        return self.async_show_form(
            step_id="schedule_remove",
            data_schema=vol.Schema({
                vol.Optional("remove", default=[]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": str(i), "label": self._schedule_event_label(event)}
                            for i, event in enumerate(events)
                        ],
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    async def async_step_schedule_done(self, user_input: dict | None = None):
        return await self._zone_saved(
            "schedule", {CONF_SCHEDULE_EVENTS: list(self._schedule_events())}
        )


class _ZoneStepsMixin(_DeviceSettingsMixin, _ScheduleEditorMixin):
    """The zone configuration steps, identical in the setup and the options flow.

    Hosts provide the zone data and decide what happens after a step was saved
    (setup: continue with the next step; options: store the section and finish).
    """

    def _zone_data(self) -> dict:
        raise NotImplementedError

    async def _zone_saved(self, step: str, update: dict):
        raise NotImplementedError

    def _outdoor_defaults(self) -> dict:
        return {}

    # ── Devices & sensors ────────────────────────────────────────────

    def _show_devices(self, data: dict, errors: dict):
        return self.async_show_form(
            step_id="devices",
            data_schema=_schema_devices(data),
            errors=errors,
        )

    async def async_step_devices(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        current = self._zone_data()
        if user_input is None:
            return self._show_devices(current, errors)
        name = user_input.get("name", "").strip()
        if not name:
            errors["name"] = "name_required"
        elif not user_input.get("climate_entities"):
            errors["climate_entities"] = "required"
        else:
            settings = dict(current.get(CONF_DEVICE_SETTINGS, {}))
            for climate_id in user_input["climate_entities"]:
                if climate_id not in settings:
                    settings[climate_id] = _detect_device_settings(self.hass, climate_id)["settings"]
            return await self._zone_saved(
                "devices", {**user_input, "name": name, CONF_DEVICE_SETTINGS: settings},
            )
        return self._show_devices({**current, **user_input}, errors)

    # ── Control strategy ─────────────────────────────────────────────

    async def async_step_control_options(self, user_input: dict | None = None):
        if user_input is not None:
            return await self._zone_saved("control_options", user_input)
        current = self._zone_data()
        climate_entities = current.get("climate_entities", [])
        detections = [_detect_device_settings(self.hass, eid) for eid in climate_entities]
        recommended = [d["recommended_mode"] for d in detections]
        default_mode = (
            CONTROL_MODE_DIRECT_VALVE
            if recommended and all(m == CONTROL_MODE_DIRECT_VALVE for m in recommended)
            else CONTROL_MODE_TPI_BOOST
        )
        lines = []
        for eid, detected in zip(climate_entities, detections):
            lines.append(
                f"- **{_friendly_name(self.hass, eid)}**: {detected['preset'].display_name} — "
                f"recommended: {detected['recommended_mode'].replace('_', ' ')}"
            )
        defaults = {CONF_ZONE_CONTROL_MODE: default_mode, **current}
        return self.async_show_form(
            step_id="control_options",
            data_schema=_schema_control_options(defaults),
            description_placeholders={
                "zone": str(current.get("name", "")),
                "detected_devices": "\n".join(lines) or "- No devices selected.",
            },
        )

    # ── Per-TRV settings ─────────────────────────────────────────────

    def _ds_zone_name(self) -> str:
        return str(self._zone_data().get("name", ""))

    def _ds_entities(self) -> list[str]:
        return self._zone_data().get("climate_entities", [])

    def _ds_zone_mode(self) -> str:
        return self._zone_data().get(CONF_ZONE_CONTROL_MODE, CONTROL_MODE_TPI_BOOST)

    async def _ds_finished(self):
        return await self._zone_saved(
            "device_settings", {CONF_DEVICE_SETTINGS: self._ds_all_settings()},
        )

    # ── Temperatures ─────────────────────────────────────────────────

    async def async_step_temperatures(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        current = self._zone_data()
        if user_input is not None:
            errors = _validate_temps(user_input)
            if not errors:
                return await self._zone_saved("temperatures", user_input)
        return self.async_show_form(
            step_id="temperatures",
            data_schema=_schema_schedule({**current, **(user_input or {})}),
            errors=errors,
            description_placeholders={"zone": str(current.get("name", ""))},
        )

    # ── Outdoor data ─────────────────────────────────────────────────

    async def async_step_outdoor(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        current = self._zone_data()
        if user_input is not None:
            errors = _validate_outdoor(self.hass, user_input)
            if not errors:
                return await self._zone_saved("outdoor", user_input)
        defaults = {**self._outdoor_defaults(), **current, **(user_input or {})}
        return self.async_show_form(
            step_id="outdoor",
            data_schema=_schema_weather(defaults),
            errors=errors,
            description_placeholders={"zone": str(current.get("name", ""))},
        )


# ── Config Flow (Ersteinrichtung) ────────────────────────────────────────────

class ThermoSmartConfigFlow(_ZoneStepsMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Einrichtung: Entweder ThermoSmart System (globale Schalter) oder eine Heizzone."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict = {}

    # Startmenü: System oder Zone – die globale Einrichtung wird nur angeboten,
    # solange noch kein ThermoSmart-Entry existiert (weder System noch Zone).
    # Danach führt der Flow direkt zum Heizzone-hinzufügen-Schritt, statt eine
    # Option anzuzeigen die ohnehin nur mit einem Abort enden würde.
    async def async_step_user(self, user_input: dict | None = None):
        if self.hass.config_entries.async_entries(DOMAIN):
            return await self.async_step_add_zone()
        return self.async_show_menu(
            step_id="user",
            menu_options=["add_system", "add_zone"],
        )

    # Option A: Globale Steuerung einrichten (kein TRV nötig)
    async def async_step_add_system(self, user_input: dict | None = None):
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get("entry_type") == "system":
                return self.async_abort(reason="system_already_configured")
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_system(self.hass, user_input)
            if not errors:
                return self.async_create_entry(
                    title="ThermoSmart System",
                    data={"entry_type": "system", **user_input},
                )
        return self.async_show_form(
            step_id="add_system",
            data_schema=_schema_system(user_input or {}),
            errors=errors,
        )

    # Option B: Heizzone hinzufügen.
    async def async_step_add_zone(self, user_input: dict | None = None):
        return await self.async_step_devices()

    def _zone_data(self) -> dict:
        return self._data

    async def _zone_saved(self, step: str, update: dict):
        self._data.update(update)
        if step == "outdoor":
            return self.async_create_entry(title=self._data["name"], data=self._data)
        following = ZONE_STEPS[ZONE_STEPS.index(step) + 1]
        return await getattr(self, f"async_step_{following}")()

    def _ds_all_settings(self) -> dict:
        return self._data.setdefault(CONF_DEVICE_SETTINGS, {})

    def _outdoor_defaults(self) -> dict:
        """Pre-fill from an existing zone; options flows show only the zone's own values."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            data = {**entry.data, **entry.options}
            if data.get(CONF_WEATHER_ENTITY):
                return {k: data[k] for k in (
                    CONF_WEATHER_ENTITY,
                    CONF_OUTDOOR_TEMP_SENSOR,
                    CONF_OUTDOOR_HUMIDITY_SENSOR,
                    CONF_OUTDOOR_WIND_SENSOR,
                    CONF_OUTDOOR_SOLAR_SENSOR,
                    CONF_OUTDOOR_RAIN_SENSOR,
                ) if data.get(k)}
        return {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        if config_entry.data.get("entry_type") == "system":
            return ThermoSmartSystemOptionsFlow()
        return ThermoSmartOptionsFlow()


# ── Options Flow (System-Entry: gleiche Felder wie bei der Einrichtung) ──────

class ThermoSmartSystemOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        return await self.async_step_system(user_input)

    async def async_step_system(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_system(self.hass, user_input)
            if not errors:
                return self.async_create_entry(title="", data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="system",
            data_schema=_schema_system(user_input or current),
            errors=errors,
        )


# ── Options Flow (Zone bearbeiten) ───────────────────────────────────────────

class ThermoSmartOptionsFlow(_ZoneStepsMixin, config_entries.OptionsFlow):
    """Edit an existing zone: the same steps as the setup, one section at a time.

    The hub menu lets the user pick a section; each section is saved on its own
    and merged with all current fields, so unrelated options are preserved.
    Per-zone setup steps are not repeated in sequence (the only flow difference).
    """

    def _current(self) -> dict:
        return {**self.config_entry.data, **self.config_entry.options}

    async def async_step_init(self, user_input: dict | None = None):
        return self.async_show_menu(
            step_id="init",
            description_placeholders={"zone": self._ds_zone_name()},
            menu_options=list(ZONE_STEPS),
        )

    def _zone_data(self) -> dict:
        return self._current()

    async def _zone_saved(self, step: str, update: dict):
        return self.async_create_entry(title="", data={**self._current(), **update})

    def _ds_all_settings(self) -> dict:
        if getattr(self, "_settings_cache", None) is None:
            self._settings_cache = dict(self._current().get(CONF_DEVICE_SETTINGS, {}))
        return self._settings_cache
