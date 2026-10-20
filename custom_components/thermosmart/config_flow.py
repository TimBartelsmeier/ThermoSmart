"""Config flow für ThermoSmart."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, selector

from .device_profiles import get_preset
from .const import (
    DOMAIN,
    CONF_ZONE_CONTROL_MODE,
    CONF_CONTROL_INTERVAL,
    CONF_TEMP_TOLERANCE,
    CONF_EXT_TEMP_THRESHOLD,
    CONF_DEVICE_SETTINGS,
    CONF_DEV_CONTROL_MODE,
    CONF_DEV_VALVE_ENTITY,
    CONF_DEV_FEED_EXT_TEMP,
    CONF_DEV_EXT_TEMP_ENTITY,
    CONF_DEV_CALIBRATION_INVERT,
    CONF_DEV_VALVE_MAINTENANCE,
    CONF_DEV_STATE_SETTINGS,
    CONTROL_MODE_INHERIT,
    CONTROL_MODE_SETPOINT,
    CONTROL_MODE_TPI_BOOST,
    CONTROL_MODE_DIRECT_VALVE,
    AUTO_VALVE_PATTERNS,
    AUTO_EXT_TEMP_PATTERNS,
    AUTO_TEMP_SOURCE_PATTERNS,
    CONF_WEATHER_ENTITY,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_OUTDOOR_HUMIDITY_SENSOR,
    CONF_OUTDOOR_WIND_SENSOR,
    CONF_OUTDOOR_SOLAR_SENSOR,
    CONF_OUTDOOR_RAIN_SENSOR,
    CONF_SCHEDULE_ENABLED,
    CONF_PRESENCE_PERSONS,
    CONF_HOME_ZONE,
    CONF_VACATION_TEMP,
    CONF_WINDOW_OPEN_TEMP,
    CONF_ECO_TEMP,
    CONF_SCHED_WD_MORNING, CONF_SCHED_WD_NIGHT,
    CONF_SCHED_WE_MORNING, CONF_SCHED_WE_NIGHT,
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

        vol.Required("window_open_delay", default=d.get("window_open_delay", 5)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=0, max=60, step=1, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX,
            )),
        vol.Required("window_close_delay", default=d.get("window_close_delay", 2)):
            selector.NumberSelector(selector.NumberSelectorConfig(
                min=0, max=30, step=1, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX,
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
        vol.Required(CONF_SCHEDULE_ENABLED, default=d.get(CONF_SCHEDULE_ENABLED, True)):
            selector.BooleanSelector(),
        vol.Optional(CONF_SCHED_WD_MORNING, default=d.get(CONF_SCHED_WD_MORNING, "06:00")):
            selector.TimeSelector(),
        vol.Optional(CONF_SCHED_WD_NIGHT, default=d.get(CONF_SCHED_WD_NIGHT, "22:00")):
            selector.TimeSelector(),
        vol.Optional(CONF_SCHED_WE_MORNING, default=d.get(CONF_SCHED_WE_MORNING, "08:00")):
            selector.TimeSelector(),
        vol.Optional(CONF_SCHED_WE_NIGHT, default=d.get(CONF_SCHED_WE_NIGHT, "23:00")):
            selector.TimeSelector(),
    })


def _schema_presence(d: dict) -> vol.Schema:
    """Presence-detection fields only.

    No global learning on/off question here — per-zone learning is controlled
    at runtime via the "Learning mode" switch entity (switch.py,
    ThermoSmartLearningSwitch), which already covers this. CONF_LEARNING_ENABLED
    stays readable in coordinator.py/__init__.py for backward compatibility with
    entries created before this field was removed from the UI.
    """
    return vol.Schema({
        vol.Optional(CONF_PRESENCE_PERSONS, default=d.get(CONF_PRESENCE_PERSONS, [])):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="person", multiple=True)),

        vol.Optional(CONF_HOME_ZONE, default=d.get(CONF_HOME_ZONE, "zone.home")):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="zone")),
    })


def _entity_options(values: list[tuple[str, str]]) -> selector.SelectSelector:
    return selector.SelectSelector(selector.SelectSelectorConfig(
        options=[{"value": value, "label": label} for value, label in values],
        mode=selector.SelectSelectorMode.DROPDOWN,
    ))


def _schema_control_options(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_ZONE_CONTROL_MODE, default=d.get(
            CONF_ZONE_CONTROL_MODE, CONTROL_MODE_TPI_BOOST
        )): _entity_options([
            (CONTROL_MODE_SETPOINT, "Setpoint only (TRV autonomous)"),
            (CONTROL_MODE_TPI_BOOST, "TPI setpoint modulation (calculated boost)"),
            (CONTROL_MODE_DIRECT_VALVE, "Direct valve control (0–100%)"),
        ]),
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
    })


def _schema_device_settings(detected: dict) -> vol.Schema:
    settings = detected["settings"]
    state_rules = settings.get(CONF_DEV_STATE_SETTINGS)
    default_state_entity = (
        state_rules[0].get("entity_id") if state_rules
        else None if CONF_DEV_STATE_SETTINGS in settings
        else detected["state_entity"]
    )
    default_state_value = (
        state_rules[0].get("value", state_rules[0].get("target_value", ""))
        if state_rules else
        "" if CONF_DEV_STATE_SETTINGS in settings
        else detected["state_value"] or ""
    )
    return vol.Schema({
        vol.Required(CONF_DEV_CONTROL_MODE, default=settings.get(
            CONF_DEV_CONTROL_MODE, CONTROL_MODE_INHERIT
        )): _entity_options([
            (CONTROL_MODE_INHERIT, "Inherit from zone"),
            (CONTROL_MODE_SETPOINT, "Setpoint only"),
            (CONTROL_MODE_TPI_BOOST, "TPI setpoint modulation"),
            (CONTROL_MODE_DIRECT_VALVE, "Direct valve control"),
        ]),
        vol.Optional(CONF_DEV_VALVE_ENTITY, default=settings.get(CONF_DEV_VALVE_ENTITY) or None):
            _NullableEntitySelector(selector.EntitySelectorConfig(domain="number")),
        vol.Required(CONF_DEV_FEED_EXT_TEMP, default=settings.get(
            CONF_DEV_FEED_EXT_TEMP, False
        )): selector.BooleanSelector(),
        vol.Optional(CONF_DEV_EXT_TEMP_ENTITY, default=settings.get(
            CONF_DEV_EXT_TEMP_ENTITY
        ) or None): _NullableEntitySelector(selector.EntitySelectorConfig(domain="number")),
        vol.Required(CONF_DEV_CALIBRATION_INVERT, default=settings.get(
            CONF_DEV_CALIBRATION_INVERT, detected["preset"].calibration_inverted
        )): selector.BooleanSelector(),
        vol.Required(CONF_DEV_VALVE_MAINTENANCE, default=settings.get(
            CONF_DEV_VALVE_MAINTENANCE, True
        )): selector.BooleanSelector(),
        vol.Optional("state_setting_entity", default=default_state_entity or None):
            _NullableEntitySelector(selector.EntitySelectorConfig(
                domain=["select", "switch", "climate"]
            )),
        vol.Optional("state_setting_value", default=default_state_value):
            selector.TextSelector(selector.TextSelectorConfig(
                type=selector.TextSelectorType.TEXT
            )),
    })


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

    def find(domain: str, patterns: tuple[str, ...] | list[str]) -> str | None:
        for entry in detected_entities:
            if entry.entity_id.split(".", 1)[0] != domain:
                continue
            fingerprint = " ".join((
                entry.entity_id,
                getattr(entry, "translation_key", None) or "",
                getattr(entry, "original_name", None) or "",
            )).lower()
            if any(pattern.lower() in fingerprint for pattern in patterns):
                return entry.entity_id
        return None

    valve_entity = find("number", (
        preset.valve_entity_pattern,
    ) if preset.valve_entity_pattern else AUTO_VALVE_PATTERNS)
    ext_entity = find("number", (
        preset.ext_temp_entity_pattern,
    ) if preset.ext_temp_entity_pattern else AUTO_EXT_TEMP_PATTERNS)
    state_entity = find("select", AUTO_TEMP_SOURCE_PATTERNS)
    state_value = None
    if state_entity:
        state_value = next(
            (rule["value"] for rule in preset.default_state_settings
             if rule.get("entity_pattern", "") in state_entity.lower()),
            None,
        )
    if not state_value and preset.default_state_settings:
        state_value = preset.default_state_settings[0]["value"]
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
        CONF_DEV_CALIBRATION_INVERT: preset.calibration_inverted,
        CONF_DEV_VALVE_MAINTENANCE: preset.valve_maintenance,
        CONF_DEV_STATE_SETTINGS: (
            [{"entity_id": state_entity, "value": state_value}]
            if state_entity and state_value else []
        ),
    }
    return {
        "preset": preset,
        "settings": settings,
        "state_entity": state_entity,
        "state_value": state_value,
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


# ── Config Flow (Ersteinrichtung) ────────────────────────────────────────────

class ThermoSmartConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
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
        return self.async_create_entry(
            title="ThermoSmart System",
            data={"entry_type": "system"},
        )

    # Option B: Heizzone hinzufügen.
    async def async_step_add_zone(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("name", "").strip():
                errors["name"] = "name_required"
            elif not user_input.get("climate_entities"):
                errors["climate_entities"] = "required"
            else:
                self._data.update(user_input)
                return await self.async_step_control_options()

        return self.async_show_form(
            step_id="add_zone",
            data_schema=_schema_devices(self._data),
            errors=errors,
        )

    async def async_step_control_options(self, user_input: dict | None = None):
        climate_entities = self._data.get("climate_entities", [])
        detections = [_detect_device_settings(self.hass, eid) for eid in climate_entities]
        if user_input is not None:
            self._data.update(user_input)
            existing = self._data.get(CONF_DEVICE_SETTINGS, {})
            for eid, detected in zip(climate_entities, detections):
                existing.setdefault(eid, detected["settings"])
            self._data[CONF_DEVICE_SETTINGS] = existing
            return await self.async_step_device_settings()

        recommended_modes = [d["recommended_mode"] for d in detections]
        mode = (
            CONTROL_MODE_DIRECT_VALVE
            if recommended_modes and all(m == CONTROL_MODE_DIRECT_VALVE for m in recommended_modes)
            else CONTROL_MODE_TPI_BOOST
        )
        summary_lines = []
        for eid, detected in zip(climate_entities, detections):
            state = self.hass.states.get(eid)
            name = state.attributes.get("friendly_name", eid) if state else eid
            summary_lines.append(
                f"- **{name}**: {detected['preset'].display_name} — recommended: "
                f"{detected['recommended_mode'].replace('_', ' ')}"
            )
        summary = "\n".join(summary_lines) or "- No devices selected."
        defaults = {**self._data, CONF_ZONE_CONTROL_MODE: mode}
        return self.async_show_form(
            step_id="control_options",
            data_schema=_schema_control_options(defaults),
            description_placeholders={"detected_devices": summary},
        )

    async def async_step_device_settings(self, user_input: dict | None = None):
        climate_entities = self._data.get("climate_entities", [])
        if not climate_entities:
            return await self.async_step_schedule()
        index = getattr(self, "_device_index", 0)
        if user_input is not None:
            eid = climate_entities[index]
            settings = dict(user_input)
            state_entity = settings.pop("state_setting_entity", None)
            state_value = settings.pop("state_setting_value", "")
            settings[CONF_DEV_STATE_SETTINGS] = (
                [{"entity_id": state_entity, "value": state_value}]
                if state_entity and state_value else []
            )
            all_settings = self._data.setdefault(CONF_DEVICE_SETTINGS, {})
            all_settings[eid] = settings
            index += 1
            self._device_index = index
            if index >= len(climate_entities):
                self._device_index = 0
                return await self.async_step_schedule()

        eid = climate_entities[index]
        detected = _detect_device_settings(self.hass, eid)
        existing = self._data.get(CONF_DEVICE_SETTINGS, {}).get(eid, {})
        detected["settings"] = {**detected["settings"], **existing}
        mode = self._data.get(CONF_ZONE_CONTROL_MODE, CONTROL_MODE_TPI_BOOST)
        warning = (
            "\n\n⚠️ **Warning:** Zone control mode is set to direct valve control, "
            "but no valve position entity was found for this device. Direct valve "
            "control may fail or fall back."
            if mode == CONTROL_MODE_DIRECT_VALVE and not detected["settings"].get(CONF_DEV_VALVE_ENTITY)
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
            data_schema=_schema_device_settings(detected),
            description_placeholders={
                "device_name": eid,
                "preset_info": header + warning,
            },
        )

    async def async_step_schedule(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_temps(user_input)
            if not errors:
                self._data.update(user_input)
                return await self.async_step_presence()

        return self.async_show_form(
            step_id="schedule",
            data_schema=_schema_schedule(self._data),
            errors=errors,
        )

    async def async_step_presence(self, user_input: dict | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_weather()

        return self.async_show_form(
            step_id="presence",
            data_schema=_schema_presence(self._data),
        )

    async def async_step_weather(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            weather = user_input.get(CONF_WEATHER_ENTITY, "")
            if weather and self.hass.states.get(weather) is None:
                errors[CONF_WEATHER_ENTITY] = "entity_not_found"
            else:
                self._data.update(user_input)
                return self.async_create_entry(
                    title=self._data["name"],
                    data=self._data,
                )

        defaults = self._defaults_from_existing()
        return self.async_show_form(
            step_id="weather",
            data_schema=_schema_weather({**defaults, **self._data}),
            errors=errors,
        )

    def _defaults_from_existing(self) -> dict:
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


# ── Options Flow (System-Entry: keine konfigurierbaren Optionen) ─────────────

class ThermoSmartSystemOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        return self.async_abort(reason="system_no_options")


# ── Options Flow (Zone bearbeiten) ───────────────────────────────────────────

class ThermoSmartOptionsFlow(config_entries.OptionsFlow):
    """Options Hub: users pick a section and edit only that section.

    Each section handler saves a partial update merged with all current fields,
    so unrelated options are always preserved. The hub entry point (async_step_init)
    shows a menu — users are never forced through all four steps for a single change.
    """

    def _current(self) -> dict:
        return {**self.config_entry.data, **self.config_entry.options}

    async def async_step_init(self, user_input: dict | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "devices", "control_options", "device_settings",
                "schedule", "presence", "weather",
            ],
        )

    async def async_step_devices(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        current = self._current()
        if user_input is not None:
            if not user_input.get("name", "").strip():
                errors["name"] = "name_required"
            elif not user_input.get("climate_entities"):
                errors["climate_entities"] = "required"
            else:
                updated = {**current, **user_input}
                device_settings = dict(current.get(CONF_DEVICE_SETTINGS, {}))
                for climate_id in user_input.get("climate_entities", []):
                    if climate_id not in device_settings:
                        device_settings[climate_id] = _detect_device_settings(
                            self.hass, climate_id
                        )["settings"]
                updated[CONF_DEVICE_SETTINGS] = device_settings
                return self.async_create_entry(title="", data=updated)
        return self.async_show_form(
            step_id="devices",
            data_schema=_schema_devices(current),
            errors=errors,
        )

    async def async_step_control_options(self, user_input: dict | None = None):
        current = self._current()
        if user_input is not None:
            return self.async_create_entry(title="", data={**current, **user_input})
        return self.async_show_form(
            step_id="control_options",
            data_schema=_schema_control_options(current),
        )

    async def async_step_device_settings(self, user_input: dict | None = None):
        current = self._current()
        climate_entities = current.get("climate_entities", [])
        if not climate_entities:
            return self.async_abort(reason="required")
        index = getattr(self, "_device_index", 0)
        settings_by_device = dict(current.get(CONF_DEVICE_SETTINGS, {}))
        if user_input is not None:
            eid = climate_entities[index]
            settings = dict(user_input)
            state_entity = settings.pop("state_setting_entity", None)
            state_value = settings.pop("state_setting_value", "")
            settings[CONF_DEV_STATE_SETTINGS] = (
                [{"entity_id": state_entity, "value": state_value}]
                if state_entity and state_value else []
            )
            settings_by_device[eid] = settings
            index += 1
            self._device_index = index
            if index >= len(climate_entities):
                self._device_index = 0
                return self.async_create_entry(
                    title="", data={**current, CONF_DEVICE_SETTINGS: settings_by_device}
                )

        eid = climate_entities[index]
        detected = _detect_device_settings(self.hass, eid)
        detected["settings"] = {
            **detected["settings"],
            **settings_by_device.get(eid, {}),
        }
        mode = current.get(CONF_ZONE_CONTROL_MODE, CONTROL_MODE_TPI_BOOST)
        warning = (
            "\n\n⚠️ **Warning:** Zone control mode is set to direct valve control, "
            "but no valve position entity was found for this device. Direct valve "
            "control may fail or fall back."
            if mode == CONTROL_MODE_DIRECT_VALVE and not detected["settings"].get(CONF_DEV_VALVE_ENTITY)
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
            data_schema=_schema_device_settings(detected),
            description_placeholders={"device_name": eid, "preset_info": header + warning},
        )

    async def async_step_schedule(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        current = self._current()
        if user_input is not None:
            errors = _validate_temps(user_input)
            if not errors:
                return self.async_create_entry(title="", data={**current, **user_input})
        return self.async_show_form(
            step_id="schedule",
            data_schema=_schema_schedule(current),
            errors=errors,
        )

    async def async_step_presence(self, user_input: dict | None = None):
        current = self._current()
        if user_input is not None:
            return self.async_create_entry(title="", data={**current, **user_input})
        return self.async_show_form(
            step_id="presence",
            data_schema=_schema_presence(current),
        )

    async def async_step_weather(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        current = self._current()
        if user_input is not None:
            weather = user_input.get(CONF_WEATHER_ENTITY, "")
            if weather and self.hass.states.get(weather) is None:
                errors[CONF_WEATHER_ENTITY] = "entity_not_found"
            else:
                return self.async_create_entry(title="", data={**current, **user_input})
        return self.async_show_form(
            step_id="weather",
            data_schema=_schema_weather(current),
            errors=errors,
        )
