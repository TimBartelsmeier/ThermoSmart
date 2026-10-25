"""TRV control and calibration – ThermoSmart TRVControlMixin."""
from __future__ import annotations

import asyncio
import logging
import math

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util.unit_conversion import TemperatureConverter

from .device_profiles import DeviceProfile, get_profile, SETPOINT_HVAC_FIRST
from .temperature_units import from_internal_temperature_c, to_internal_temperature_c
from .const import (
    CONF_WINDOW_OPEN_TEMP,
    CONF_DEVICE_SETTINGS,
    CONF_EXT_TEMP_THRESHOLD,
    CONF_TEMP_TOLERANCE,
    CONF_ZONE_CONTROL_MODE,
    CONF_DEV_CONTROL_MODE,
    CONF_DEV_VALVE_ENTITY,
    CONF_DEV_FEED_EXT_TEMP,
    CONF_DEV_EXT_TEMP_ENTITY,
    CONF_DEV_CALIBRATION_ENTITY,
    CONF_DEV_CALIBRATION_SYNC,
    CONF_DEV_CALIBRATION_INVERT,
    CONF_DEV_WINDOW_OFF,
    CONF_DEV_STATE_SETTINGS,
    CONTROL_MODE_INHERIT,
    CONTROL_MODE_SETPOINT,
    CONTROL_MODE_TPI_BOOST,
    CONTROL_MODE_DIRECT_VALVE,
    WINDOW_OPEN_SETPOINT,
    TPI_VALVE_BUMP_PCT,
    TPI_VALVE_BUMP_DELAY,
)

_LOGGER = logging.getLogger(__name__)


# ── Dispatch statistics (privacy-safe, no entity IDs) ────────────────────────

class _DispatchStats:
    """Accumulates per-entity dispatch results without exposing entity IDs."""
    __slots__ = ("targets_total", "targets_succeeded", "targets_failed",
                 "failure_reasons", "effective_setpoints",
                 "effective_setpoints_by_entity", "failed_entity_ids")

    def __init__(self) -> None:
        self.targets_total: int = 0
        self.targets_succeeded: int = 0
        self.targets_failed: int = 0
        self.failure_reasons: list = []
        self.effective_setpoints: list = []  # actual per-device °C values written (successes only)
        # entity_id → effective °C written (successes only; keyed for counterfactual baseline).
        self.effective_setpoints_by_entity: dict = {}
        # entity_id per failed dispatch attempt (may repeat within one cycle across
        # dispatch paths) — feeds the coordinator's per-entity consecutive-failure
        # counter (Device Compatibility P1). Not privacy-sensitive: consumed only
        # in-process by the coordinator, never itself exported or stored.
        self.failed_entity_ids: list = []

    def record(self, exc=None, *, effective_c=None, entity_id: str | None = None) -> None:
        """Record one dispatch attempt; pass exc to mark it as failed.

        effective_c: actual setpoint °C that was written (for success, ignored on failure).
        entity_id: entity whose setpoint was written; populates
        effective_setpoints_by_entity on success, failed_entity_ids on failure.
        """
        self.targets_total += 1
        if exc is not None:
            self.targets_failed += 1
            self.failure_reasons.append(_normalize_dispatch_error(exc))
            if entity_id is not None:
                self.failed_entity_ids.append(entity_id)
        else:
            self.targets_succeeded += 1
            if effective_c is not None:
                self.effective_setpoints.append(float(effective_c))
                if entity_id is not None:
                    self.effective_setpoints_by_entity[entity_id] = float(effective_c)

    def record_gather(self, results) -> None:
        """Process the list of results from asyncio.gather(return_exceptions=True).

        For per-entity effective setpoint tracking, use record() directly with effective_c.
        """
        for r in results:
            if isinstance(r, BaseException):
                self.targets_failed += 1
                self.failure_reasons.append(_normalize_dispatch_error(r))
            else:
                self.targets_succeeded += 1
        self.targets_total += len(results)

    def merge(self, other: "_DispatchStats") -> "_DispatchStats":
        merged = _DispatchStats()
        merged.targets_total = self.targets_total + other.targets_total
        merged.targets_succeeded = self.targets_succeeded + other.targets_succeeded
        merged.targets_failed = self.targets_failed + other.targets_failed
        merged.failure_reasons = self.failure_reasons + other.failure_reasons
        merged.effective_setpoints = self.effective_setpoints + other.effective_setpoints
        merged.effective_setpoints_by_entity = {
            **self.effective_setpoints_by_entity, **other.effective_setpoints_by_entity}
        merged.failed_entity_ids = self.failed_entity_ids + other.failed_entity_ids
        return merged

    @property
    def status(self) -> str:
        if self.targets_total == 0:
            return "not_attempted"
        if self.targets_failed == 0:
            return "fully_succeeded"
        if self.targets_succeeded == 0:
            return "failed"
        return "partially_succeeded"


def _normalize_dispatch_error(exc: BaseException) -> str:
    """Return a privacy-safe dispatch error label (no entity IDs, no service payloads)."""
    try:
        from homeassistant.exceptions import (
            ServiceNotFound, ServiceValidationError, HomeAssistantError)
        if isinstance(exc, ServiceNotFound):
            return "service_not_found"
        if isinstance(exc, ServiceValidationError):
            return "service_validation_error"
        if isinstance(exc, HomeAssistantError):
            return "ha_service_error"
    except ImportError:
        pass
    return type(exc).__name__


class TRVControlMixin:
    """TRV-Steuerung, automatische Kalibrierung, Quirk-Management und Geräteerkennung."""

    # ── Geräteerkennung ──────────────────────────────────────────────

    async def async_detect_device_entities(self) -> None:
        """Resolve the device profile of every configured TRV via the device registry."""
        cfg = self.zone_cfg
        climate_entities = cfg.get("climate_entities", [])
        if not climate_entities:
            return

        ent_reg = er.async_get(self.hass)

        device_to_climate: dict[str, str] = {}
        for entity_id in climate_entities:
            entry = ent_reg.async_get(entity_id)
            if entry and entry.device_id:
                device_to_climate[entry.device_id] = entity_id

        if not device_to_climate:
            return

        # ── Device profile detection ──────────────────────────────────────────
        dev_reg = dr.async_get(self.hass)
        profile_map: dict[str, DeviceProfile] = {}
        for device_id, climate_id in device_to_climate.items():
            dev_entry = dev_reg.async_get(device_id)
            manufacturer = dev_entry.manufacturer if dev_entry else None
            model = dev_entry.model if dev_entry else None
            profile = get_profile(model, manufacturer)
            profile_map[climate_id] = profile
            _LOGGER.debug(
                "ThermoSmart '%s': device profile → %s (entity=%s, manufacturer=%r, model=%r)",
                self.zone_name, profile.identifier, climate_id, manufacturer, model,
            )

        self._device_profiles = profile_map

    # ── TRV-Offline-Tracking ─────────────────────────────────────────

    def _get_trv_state(self, entity_id: str):
        """Return a usable TRV state; availability reporting is handled separately."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            self._trv_offline.add(entity_id)
            return None
        self._trv_offline.discard(entity_id)
        return state

    def _supports_single_setpoint(self, entity_id: str, state) -> bool:
        """Return False only when a climate entity EXPLICITLY declares
        TARGET_TEMPERATURE_RANGE support and NOT single TARGET_TEMPERATURE —
        a genuine dual-setpoint / HEAT_COOL-only device.

        ThermoSmart only ever calls ``climate.set_temperature`` with a single
        ``temperature`` value; such an entity would reject that on every
        single cycle. Warns ONCE per entity (never a silent, repeating
        failure loop) and the caller skips dispatch for it entirely — the
        entity is never even attempted, so it cannot pollute the consecutive-
        dispatch-failure counter either. No new UI, no crash.

        A missing/zero ``supported_features`` (no positive evidence either
        way — e.g. an integration that has not populated it) is treated as
        single-setpoint-capable, matching prior behavior: this guard only
        acts on a real, positive dual-setpoint-only signal, never on absence
        of information.
        """
        supported = state.attributes.get("supported_features", 0) or 0
        if supported & ClimateEntityFeature.TARGET_TEMPERATURE:
            return True
        if not (supported & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE):
            return True
        if entity_id not in self._dual_setpoint_warned:
            self._dual_setpoint_warned.add(entity_id)
            _LOGGER.warning(
                "ThermoSmart '%s': %s unterstützt kein einzelnes Sollwert-Attribut "
                "(nur TARGET_TEMPERATURE_RANGE / HEAT_COOL) – ThermoSmart kann "
                "diesen TRV nicht direkt ansteuern und überspringt ihn.",
                self.zone_name, entity_id,
            )
        return False

    # ── Watchdog ─────────────────────────────────────────────────────

    # HVAC-Modi die ThermoSmart niemals beim TRV haben möchte:
    # - "auto"      → TRV nutzt eigenen internen Zeitplan, ignoriert externe Setpoints
    # - "heat_cool" → Automatisches Heizen/Kühlen ohne externe Kontrolle
    # - "cool"      → Kühlmodus, nicht gewünscht für Heizzonen
    # - "off"       → Watchdog setzt zurück auf heat
    _UNWANTED_MODES = {"auto", "heat_cool", "cool", "dry", "fan_only", "off"}

    async def _watchdog_hvac(self, cfg: dict, recommendation: dict) -> None:
        """TRVs in unerwünschten Modi auf 'heat' zurücksetzen.

        Fängt alle Modi die ThermoSmart's Steuerung untergraben würden:
        - 'off':       TRV heizt gar nicht mehr
        - 'auto':      TRV nutzt eigenen internen Zeitplan (TRVZB-Hauptproblem!)
                       Im Auto-Mode ignoriert der TRVZB externe set_temperature-Befehle
                       vollständig und folgt nur noch seinem eigenen Wochenprogramm.
        - 'heat_cool': Automatischer Modus ohne externe Kontrolle
        - andere:      Kühlen, Fan etc.

        Sonderfall no-off-Mode: TRVs ohne 'heat'-Unterstützung werden übersprungen.
        """
        if recommendation.get("window_open"):
            return

        for entity_id in cfg.get("climate_entities", []):
            state = self._get_trv_state(entity_id)
            if state is None:
                continue

            _profile = self._device_profiles.get(entity_id)
            if _profile is not None and not _profile.hvac_watchdog:
                _LOGGER.debug(
                    "ThermoSmart '%s': Watchdog skipped for %s – profile '%s' has hvac_watchdog=False",
                    self.zone_name, entity_id, _profile.identifier,
                )
                continue

            if state.state not in self._UNWANTED_MODES:
                continue  # TRV ist in 'heat' – alles gut

            hvac_modes = state.attributes.get("hvac_modes", [])
            if "heat" not in hvac_modes:
                _LOGGER.debug(
                    "ThermoSmart '%s': %s hat keinen 'heat'-Mode – Watchdog übersprungen",
                    self.zone_name, entity_id,
                )
                continue

            _LOGGER.info(
                "ThermoSmart Watchdog '%s': %s ist im Modus '%s' → erzwinge 'heat'",
                self.zone_name, entity_id, state.state,
            )
            self._mark_trv_write(entity_id, cfg)
            await self.hass.services.async_call(
                "climate", "set_hvac_mode",
                {"entity_id": entity_id, "hvac_mode": "heat"},
                blocking=False,
            )

    # ── Kalibrierung ─────────────────────────────────────────────────

    async def _async_calibrate_trvs(self, cfg: dict, recommendation: dict) -> None:
        """Lokale TRV-Kalibrierung: Offset zwischen Raumsensor und TRV-Sensor.

        EMA-geglättet (α=0.25), Schreiben nur bei Änderung > 0.5°C.
        Überspringt wenn Heizkörper gerade heizt (Sensor verfälscht durch Radiatorkontakt).
        """
        room_temp = recommendation.get("current_temp")
        if room_temp is None:
            return

        for climate_id in cfg.get("climate_entities", []):
            settings = cfg.get(CONF_DEVICE_SETTINGS, {}).get(climate_id, {})
            cal_entity = settings.get(CONF_DEV_CALIBRATION_ENTITY)
            if not cal_entity or not settings.get(CONF_DEV_CALIBRATION_SYNC, False):
                continue
            _cal_profile = self._device_profiles.get(climate_id)

            trv_state = self._get_trv_state(climate_id)
            trv_temp: float | None = None
            if trv_state:
                # climate.current_temperature carries no unit attribute of its
                # own — HA already normalizes it to the system unit.
                trv_temp = to_internal_temperature_c(
                    self.hass, trv_state.attributes.get("current_temperature"))

            if trv_temp is None:
                continue

            if trv_temp - room_temp > 3.0:
                _LOGGER.debug(
                    "ThermoSmart '%s': Kalibrierung %s übersprungen – TRV %.1f°C > Raum %.1f°C",
                    self.zone_name, cal_entity, trv_temp, room_temp,
                )
                continue

            raw_offset = room_temp - trv_temp

            _invert = settings.get(CONF_DEV_CALIBRATION_INVERT, False)
            if _invert:
                raw_offset = -raw_offset

            _plaus_limit = (
                _cal_profile.calibration_plausibility_limit
                if _cal_profile is not None else 7.0
            )
            if abs(raw_offset) > _plaus_limit:
                _LOGGER.warning(
                    "ThermoSmart '%s': Kalibrierungs-Offset %.1f°C für %s unplausibel – übersprungen",
                    self.zone_name, raw_offset, cal_entity,
                )
                continue

            prev = self._calibration_offsets.get(cal_entity, raw_offset)
            smoothed = round(0.25 * raw_offset + 0.75 * prev, 1)
            self._calibration_offsets[cal_entity] = smoothed

            cal_state = self.hass.states.get(cal_entity)
            if cal_state is None:
                continue
            try:
                current_cal = float(cal_state.state)
            except (TypeError, ValueError):
                current_cal = 0.0

            if abs(smoothed - current_cal) < 0.5:
                continue

            clamped = max(-5.0, min(5.0, smoothed))
            _LOGGER.info(
                "ThermoSmart '%s': TRV-Kalibrierung %s → %.1f°C (Raum=%.1f°C, TRV=%.1f°C)",
                self.zone_name, cal_entity, clamped, room_temp, trv_temp,
            )
            self.hass.async_create_task(self.hass.services.async_call(
                "number", "set_value",
                {"entity_id": cal_entity, "value": clamped},
                blocking=False,
            ))

    # ── Hilfsroutinen ────────────────────────────────────────────────

    @staticmethod
    def _round_to_step(value: float, step: float) -> float:
        """Round value to the nearest multiple of step (round half up)."""
        return round(math.floor(value / step + 0.5) * step, 2)

    def _snap_to_device_step(self, entity_id: str, state, value: float) -> float:
        """Snap value to the TRV's target_temp_step if the attribute is present.

        Known documented limitation (Temperature-Unit audit): unlike
        min_temp/max_temp/current_temperature/temperature, HA's climate
        platform does NOT normalize target_temp_step to the system unit —
        it is exposed exactly as the entity itself reports it. A step is an
        INTERVAL, not an absolute reading, so it cannot be corrected with
        to_internal_temperature_c() (a point conversion; Fahrenheit-to-Celsius
        includes a +32 offset that must never apply to a delta). Converting
        it correctly would need HA's separate interval-conversion API. Left
        unconverted here: the practical impact is at most a slightly coarser
        or finer snap granularity on a non-Celsius-system installation with a
        device reporting a foreign-unit step — never a safety issue, since
        min/max clamping (already unit-safe above) always runs before and
        after this snap.
        """
        try:
            step = float(state.attributes.get("target_temp_step", 0))
            if step > 0:
                snapped = self._round_to_step(value, step)
                if snapped != value:
                    _LOGGER.debug(
                        "ThermoSmart '%s': %s step-snap %.4f°C → %.2f°C (step=%.2f)",
                        self.zone_name, entity_id, value, snapped, step,
                    )
                return snapped
        except (TypeError, ValueError):
            pass
        return value

    def resolve_device_effective_setpoint(
        self, entity_id: str, state, setpoint: float, profile=None
    ) -> float:
        """Compute the effective setpoint after all per-device transforms.

        Single authority for both the dispatch path and the counterfactual baseline:

            device_applied_boost = resolve_device_effective_setpoint(eid, state, baseline + boost)
                                 - resolve_device_effective_setpoint(eid, state, baseline)

        Transform order (matches _apply_temperature exactly — no drift possible):
          1. HA min_temp clamp
          2. Profile minimum_setpoint clamp
          3. Step-snap (target_temp_step attribute)
          4. Re-clamp min after snap (prevents snap from going below device/profile floor)
          5. HA max_temp clamp (device ceiling)
          6. Profile maximum_setpoint clamp (may be stricter than HA max)

        Args:
            entity_id: for step-snap debug logging.
            state: HA state object with .attributes dict.
            setpoint: input °C (e.g. tpi_baseline_setpoint or baseline + approved_boost).
            profile: DeviceProfile or None.
        Returns:
            Effective °C after all per-device clamps, snap, and ceiling.
        """
        eff = setpoint
        # min_temp/max_temp are HA climate capability_attributes — already
        # normalized to the HA system unit (never the device's native unit)
        # by HA's own climate platform, so a plain unit-less conversion here
        # (falls back to hass.config.units.temperature_unit) is correct and
        # prevents a Fahrenheit-system installation from silently disabling
        # this plausibility band (e.g. 41°F would fail a raw "<= 30" check).
        dev_min = to_internal_temperature_c(self.hass, state.attributes.get("min_temp"))
        dev_max = to_internal_temperature_c(self.hass, state.attributes.get("max_temp"))
        # 1. HA min_temp clamp
        if dev_min is not None and 0 < dev_min <= 30 and dev_min > eff:
            _LOGGER.debug(
                "ThermoSmart '%s': clamped setpoint for %s from %.1f°C to %.1f°C"
                " due to device min_temp",
                self.zone_name, entity_id, eff, dev_min,
            )
            eff = dev_min
        # 2. Profile minimum_setpoint clamp
        if profile is not None and getattr(profile, "minimum_setpoint", None) is not None:
            if profile.minimum_setpoint > eff:
                _LOGGER.debug(
                    "ThermoSmart '%s': clamped setpoint for %s from %.1f°C to %.1f°C"
                    " due to profile minimum_setpoint",
                    self.zone_name, entity_id, eff, profile.minimum_setpoint,
                )
                eff = profile.minimum_setpoint
        # 3. Step-snap (target_temp_step attribute)
        eff = self._snap_to_device_step(entity_id, state, eff)
        # 4. Re-clamp min after snap (snap may have rounded down below floor)
        if dev_min is not None and 0 < dev_min <= 30 and dev_min > eff:
            eff = dev_min
        if profile is not None and getattr(profile, "minimum_setpoint", None) is not None:
            if profile.minimum_setpoint > eff:
                eff = profile.minimum_setpoint
        # 5. HA max_temp clamp (device ceiling, applied after step-snap)
        if dev_max is not None and dev_max > 0 and eff > dev_max:
            _LOGGER.debug(
                "ThermoSmart '%s': clamped setpoint for %s from %.1f°C to %.1f°C"
                " due to device max_temp",
                self.zone_name, entity_id, eff, dev_max,
            )
            eff = dev_max
        # 6. Profile maximum_setpoint clamp (may be stricter than HA max)
        if profile is not None and getattr(profile, "maximum_setpoint", None) is not None:
            if eff > profile.maximum_setpoint:
                _LOGGER.debug(
                    "ThermoSmart '%s': clamped setpoint for %s from %.1f°C to %.1f°C"
                    " due to profile maximum_setpoint",
                    self.zone_name, entity_id, eff, profile.maximum_setpoint,
                )
                eff = profile.maximum_setpoint
        return eff

    # ── Temperatur schreiben ──────────────────────────────────────────

    async def _async_enforce_device_state_settings(self, climate_id: str, cfg: dict) -> None:
        """Apply configured state rules only when the current value differs."""
        settings = cfg.get(CONF_DEVICE_SETTINGS, {}).get(climate_id, {})
        rules = settings.get(CONF_DEV_STATE_SETTINGS, [])
        enforced = getattr(self, "_state_settings_enforced", set())
        for rule in rules:
            entity_id = rule.get("entity_id")
            target = rule.get("value")
            if not entity_id or target in (None, ""):
                continue
            rule_key = (climate_id, entity_id, str(target))
            if rule_key in enforced:
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                continue
            domain = entity_id.split(".", 1)[0]
            if domain == "switch":
                target_on = str(target).lower() in ("on", "true", "1")
                if (state.state == "on") == target_on:
                    enforced.add((climate_id, entity_id, str(target)))
                    self._state_settings_enforced = enforced
                    continue
            elif domain == "number":
                try:
                    target_num = float(target)
                    if abs(float(state.state) - target_num) < 1e-6:
                        continue
                except ValueError:
                    _LOGGER.warning(
                        "ThermoSmart '%s': Invalid numeric state setting %s=%s",
                        self.zone_name, entity_id, target,
                    )
                    continue
            elif state.state == str(target):
                continue

            if domain == "select":
                service, payload = "select_option", {"option": str(target)}
            elif domain == "number":
                service, payload = "set_value", {"value": target_num}
            elif domain == "switch":
                service = "turn_on" if target_on else "turn_off"
                payload = {}
            else:
                _LOGGER.warning(
                    "ThermoSmart '%s': Unsupported state setting entity domain for %s",
                    self.zone_name, entity_id,
                )
                continue
            try:
                await self.hass.services.async_call(
                    domain, service, {"entity_id": entity_id, **payload}, blocking=True,
                )
                enforced.add(rule_key)
                self._state_settings_enforced = enforced
            except Exception as err:
                _LOGGER.warning(
                    "ThermoSmart '%s': Could not apply configured state %s=%s: %s",
                    self.zone_name, entity_id, target, err,
                )

    def _window_off_wanted(self, entity_id: str, state, cfg: dict) -> bool:
        """True if this TRV should be turned off (not setpoint-lowered) on an open window."""
        settings = cfg.get(CONF_DEVICE_SETTINGS, {}).get(entity_id, {})
        if not settings.get(CONF_DEV_WINDOW_OFF, False):
            return False
        if "off" not in (state.attributes.get("hvac_modes") or []):
            _LOGGER.debug(
                "ThermoSmart '%s': %s has no 'off' mode – using window-open setpoint instead",
                self.zone_name, entity_id,
            )
            return False
        return True

    async def _async_restore_after_window_off(self, cfg: dict) -> None:
        """Switch TRVs that were turned off for an open window back to heat."""
        for entity_id in list(self._window_off_entities):
            self._window_off_entities.discard(entity_id)
            # The window-open phase wrote no setpoint; a stale value would make the
            # manual-change listener misread the TRV's first report after re-heating.
            self._last_written_setpoints.pop(entity_id, None)
            state = self._get_trv_state(entity_id)
            if state is None or state.state != "off":
                continue
            if "heat" not in (state.attributes.get("hvac_modes") or []):
                continue
            try:
                self._mark_trv_write(entity_id, cfg)
                await self.hass.services.async_call(
                    "climate", "set_hvac_mode",
                    {"entity_id": entity_id, "hvac_mode": "heat"},
                    blocking=True,
                )
            except Exception as err:
                _LOGGER.warning(
                    "ThermoSmart '%s': could not re-enable heat mode on %s: %s",
                    self.zone_name, entity_id, err,
                )

    async def _apply_temperature(self, cfg: dict, recommendation: dict) -> "_DispatchStats":
        """TRV-Setpoints schreiben; bei geöffnetem Fenster Fenster-Setpoint setzen oder TRV ausschalten.

        _last_written_setpoints is updated only for entities where the service call
        actually succeeded.  This prevents false manual-override detection after a
        failed dispatch, since the manual-override listener compares the TRV's
        echoed state against _last_written_setpoints.
        """
        stats = _DispatchStats()
        target = recommendation.get("effective_target")

        if target is None:
            if recommendation.get("window_open"):
                effective_frost = cfg.get(CONF_WINDOW_OPEN_TEMP, WINDOW_OPEN_SETPOINT)
                # Collect (entity_id, frost_temp, coro) so results can be paired with entities.
                _frost_pending: list = []
                _off_pending: list = []
                for entity_id in cfg.get("climate_entities", []):
                    await self._async_enforce_device_state_settings(entity_id, cfg)
                    state = self._get_trv_state(entity_id)
                    if state is None:
                        continue
                    if self._window_off_wanted(entity_id, state, cfg):
                        self._window_off_entities.add(entity_id)
                        if state.state != "off":
                            self._mark_trv_write(entity_id, cfg)
                            _off_pending.append((entity_id, self.hass.services.async_call(
                                "climate", "set_hvac_mode",
                                {"entity_id": entity_id, "hvac_mode": "off"},
                                blocking=True,
                            )))
                        continue
                    self._window_off_entities.discard(entity_id)
                    if not self._supports_single_setpoint(entity_id, state):
                        continue
                    frost_temp = cfg.get(CONF_WINDOW_OPEN_TEMP, WINDOW_OPEN_SETPOINT)
                    # min_temp is already normalized to the HA system unit by
                    # HA's climate platform — see resolve_device_effective_setpoint().
                    device_min = to_internal_temperature_c(self.hass, state.attributes.get("min_temp"))
                    if device_min is not None and 0 < device_min <= 30 and device_min > frost_temp:
                        _LOGGER.debug(
                            "ThermoSmart '%s': clamped window-open setpoint for %s"
                            " from %.1f°C to %.1f°C due to device min_temp",
                            self.zone_name, entity_id, frost_temp, device_min,
                        )
                        frost_temp = device_min
                    _wp = self._device_profiles.get(entity_id)
                    if _wp is not None and not _wp.is_active:
                        _LOGGER.debug(
                            "ThermoSmart '%s': skipping setpoint write for %s (profile is_active=False)",
                            self.zone_name, entity_id,
                        )
                        continue
                    if _wp is not None and _wp.minimum_setpoint is not None and _wp.minimum_setpoint > frost_temp:
                        _LOGGER.debug(
                            "ThermoSmart '%s': clamped window-open setpoint for %s"
                            " from %.1f°C to %.1f°C due to profile minimum_setpoint",
                            self.zone_name, entity_id, frost_temp, _wp.minimum_setpoint,
                        )
                        frost_temp = _wp.minimum_setpoint
                    frost_temp = self._snap_to_device_step(entity_id, state, frost_temp)
                    # Final clamp: snap must not fall below device or profile minimum
                    if device_min is not None and 0 < device_min <= 30 and device_min > frost_temp:
                        frost_temp = device_min
                    if _wp is not None and _wp.minimum_setpoint is not None and _wp.minimum_setpoint > frost_temp:
                        frost_temp = _wp.minimum_setpoint
                    effective_frost = frost_temp  # track clamped value for sensor display
                    if self._manual_setpoint_is_pending(entity_id):
                        continue
                    current_setpoint_c = to_internal_temperature_c(
                        self.hass, state.attributes.get("temperature"))
                    if current_setpoint_c is not None and abs(current_setpoint_c - frost_temp) < 0.3:
                        continue
                    self._mark_trv_write(entity_id, cfg)
                    _frost_pending.append((entity_id, frost_temp, self.hass.services.async_call(
                        "climate", "set_temperature",
                        {"entity_id": entity_id,
                         "temperature": from_internal_temperature_c(self.hass, frost_temp)},
                        blocking=True,
                    )))
                if _off_pending:
                    _off_results = await asyncio.gather(
                        *[coro for _, coro in _off_pending], return_exceptions=True)
                    for (eid, _), result in zip(_off_pending, _off_results):
                        if isinstance(result, BaseException):
                            _LOGGER.debug(
                                "ThermoSmart '%s': window-open turn-off failed for %s: %s",
                                self.zone_name, eid, result,
                            )
                            stats.record(result, entity_id=eid)
                        else:
                            stats.record(None)
                if _frost_pending:
                    _results = await asyncio.gather(
                        *[coro for _, _, coro in _frost_pending], return_exceptions=True)
                    for (eid, sp, _), result in zip(_frost_pending, _results):
                        if isinstance(result, BaseException):
                            _LOGGER.debug(
                                "ThermoSmart '%s': window-open setpoint failed for %s: %s",
                                self.zone_name, eid, result,
                            )
                            stats.record(result, entity_id=eid)
                        else:
                            self._last_written_setpoints[eid] = sp  # success only
                            stats.record(None)
                # Immediately reflect the written frost temp in the sensor display.
                # Without this, the display fallback (which runs before _apply_temperature)
                # would show the stale pre-window-open setpoint for one full cycle.
                recommendation["trv_setpoint"] = effective_frost
            return stats

        await self._async_restore_after_window_off(cfg)

        if not (5.0 <= target <= 30.0):
            _LOGGER.warning(
                "ThermoSmart '%s': Ziel %.1f°C außerhalb Sicherheitsbereich",
                self.zone_name, target,
            )
            return stats

        trv_setpoint = recommendation.get("trv_setpoint", target)
        unboosted_target = target
        zone_mode = cfg.get(CONF_ZONE_CONTROL_MODE, CONTROL_MODE_TPI_BOOST)
        tolerance = cfg.get(CONF_TEMP_TOLERANCE, 0.5)

        # Collect gather-path entities so results can be paired with entity IDs and setpoints.
        _sp_pending: list = []  # (entity_id, effective_setpoint, coro)
        for entity_id in cfg.get("climate_entities", []):
            await self._async_enforce_device_state_settings(entity_id, cfg)
            state = self._get_trv_state(entity_id)
            if state is None:
                continue
            _profile = self._device_profiles.get(entity_id)
            if _profile is not None and not _profile.is_active:
                _LOGGER.debug(
                    "ThermoSmart '%s': skipping setpoint write for %s (profile is_active=False)",
                    self.zone_name, entity_id,
                )
                continue
            if not self._supports_single_setpoint(entity_id, state):
                continue
            device_settings = cfg.get(CONF_DEVICE_SETTINGS, {}).get(entity_id, {})
            device_mode = device_settings.get(CONF_DEV_CONTROL_MODE, CONTROL_MODE_INHERIT)
            effective_mode = (
                device_mode if device_mode != CONTROL_MODE_INHERIT else zone_mode
            )
            if effective_mode in (CONTROL_MODE_SETPOINT, CONTROL_MODE_DIRECT_VALVE):
                requested_setpoint = unboosted_target
            elif zone_mode == CONTROL_MODE_SETPOINT:
                requested_setpoint = recommendation.get("tpi_boosted_setpoint", target)
            else:
                requested_setpoint = trv_setpoint
            effective_setpoint = self.resolve_device_effective_setpoint(
                entity_id, state, requested_setpoint, _profile)
            if self._manual_setpoint_is_pending(entity_id):
                continue
            current_setpoint = to_internal_temperature_c(self.hass, state.attributes.get("temperature"))
            if current_setpoint is not None:
                if abs(current_setpoint - effective_setpoint) < tolerance:
                    continue
            _LOGGER.debug(
                "ThermoSmart '%s' → %s: %.1f°C (Ziel=%.1f°C, Boost+%.1f°C)",
                self.zone_name, entity_id, effective_setpoint, target, effective_setpoint - target,
            )
            # Note: _last_written_setpoints is updated below only after confirmed success.

            if _profile is not None and _profile.setpoint_method == SETPOINT_HVAC_FIRST:
                # Sequential write: set HVAC mode first, wait, then write setpoint.
                # Required for TRVs that silently ignore climate.set_temperature unless
                # they are already in the correct HVAC mode (e.g. Eurotronic Spirit).
                mode = _profile.hvac_mode_before_write or "heat"
                if state.state != mode:
                    try:
                        await self.hass.services.async_call(
                            "climate", "set_hvac_mode",
                            {"entity_id": entity_id, "hvac_mode": mode},
                            blocking=True,
                        )
                    except Exception as err:
                        _LOGGER.warning(
                            "ThermoSmart '%s': HVAC mode pre-write failed for %s: %s",
                            self.zone_name, entity_id, err,
                        )
                    if _profile.setpoint_delay_seconds > 0.0:
                        await asyncio.sleep(_profile.setpoint_delay_seconds)
                _hvac_first_exc = None
                try:
                    self._mark_trv_write(entity_id, cfg)
                    await self.hass.services.async_call(
                        "climate", "set_temperature",
                        {"entity_id": entity_id,
                         "temperature": from_internal_temperature_c(self.hass, effective_setpoint)},
                        blocking=True,
                    )
                    self._last_written_setpoints[entity_id] = effective_setpoint  # success only
                except Exception as err:
                    _LOGGER.debug(
                        "ThermoSmart '%s': HVAC_FIRST setpoint failed for %s: %s",
                        self.zone_name, entity_id, err,
                    )
                    _hvac_first_exc = err
                stats.record(_hvac_first_exc,
                             effective_c=effective_setpoint if _hvac_first_exc is None else None,
                             entity_id=entity_id)
            else:
                self._mark_trv_write(entity_id, cfg)
                _sp_pending.append((entity_id, effective_setpoint, self.hass.services.async_call(
                    "climate", "set_temperature",
                    {"entity_id": entity_id,
                     "temperature": from_internal_temperature_c(self.hass, effective_setpoint)},
                    blocking=True,
                )))

        if _sp_pending:
            _gather_results = await asyncio.gather(
                *[coro for _, _, coro in _sp_pending], return_exceptions=True)
            for (eid, sp, _), result in zip(_sp_pending, _gather_results):
                if isinstance(result, BaseException):
                    _LOGGER.debug(
                        "ThermoSmart '%s': Temperatur-Setpoint fehlgeschlagen: %s",
                        self.zone_name, result,
                    )
                    stats.record(result, entity_id=eid)
                else:
                    self._last_written_setpoints[eid] = sp  # success only
                    stats.record(None, effective_c=sp, entity_id=eid)

        if trv_setpoint > target:
            if self.zone_id not in self._boost_active:
                self._boost_active[self.zone_id] = {
                    "target": target,
                    "setpoint": trv_setpoint,
                    "started": self._now_local(),
                }
            else:
                self._boost_active[self.zone_id]["target"] = target
                self._boost_active[self.zone_id]["setpoint"] = trv_setpoint

        return stats

    # ── Direkte Ventilsteuerung (TPI) ────────────────────────────────

    async def _async_set_valve_percent(self, cfg: dict, duty_cycle: float) -> "_DispatchStats":
        """Schreibt TPI Duty-Cycle direkt als Ventilprozent (0–100%) auf unterstützte TRVs.

        Unterstützte Entities: valve_position, pi_heating_demand, heating_demand, level.
        valve_opening_degree is intentionally excluded (max-opening limit, not a live
        position — see AUTO_VALVE_PATTERNS in const.py).

        Valve-Bump-Workaround (TRVZB-Motor):
        Beim Schließen (neuer Wert < letzter Wert) kurz weiter öffnen und dann
        auf Zielwert setzen – verhindert steckende Motoren bei kleinen Schließbewegungen.

        Returns _DispatchStats with per-valve write counts.
        Individual valve writes use blocking=False (fire-and-forget at device level)
        but the service call itself is awaited so exceptions are captured in stats.
        """
        stats = _DispatchStats()
        target_pct = max(0, min(100, round(duty_cycle)))
        device_settings = cfg.get(CONF_DEVICE_SETTINGS, {})
        zone_mode = cfg.get(CONF_ZONE_CONTROL_MODE, CONTROL_MODE_TPI_BOOST)

        for climate_id in cfg.get("climate_entities", []):
            settings = device_settings.get(climate_id, {})
            device_mode = settings.get(CONF_DEV_CONTROL_MODE, CONTROL_MODE_INHERIT)
            effective_mode = device_mode if device_mode != CONTROL_MODE_INHERIT else zone_mode
            if effective_mode != CONTROL_MODE_DIRECT_VALVE:
                continue
            valve_entity = settings.get(CONF_DEV_VALVE_ENTITY)
            if not valve_entity:
                continue

            await self._async_enforce_device_state_settings(climate_id, cfg)
            state = self.hass.states.get(valve_entity)
            if state is None or state.state in ("unavailable", "unknown"):
                continue

            # Homematic IP (hahomematic) uses 0.0–1.0 scale instead of 0–100.
            # Detect by checking if current state value is <= 1.0 and entity contains "level".
            is_fractional = "level" in valve_entity.lower()

            try:
                raw = float(state.state)
                current_pct = round(raw * 100 if is_fractional else raw)
            except (TypeError, ValueError):
                current_pct = None

            _profile = self._device_profiles.get(climate_id)
            # Valve-Bump: beim Schließen kurz öffnen dann schließen
            # Verhindert TRVZB-Motor-Sticking bei kleinen Schließbewegungen
            if (
                (_profile is None or _profile.valve_bump_on_close)
                and current_pct is not None
                and target_pct < current_pct
                and (current_pct - target_pct) >= 5
            ):
                bump_pct = min(100, current_pct + TPI_VALVE_BUMP_PCT)
                bump_val = round(bump_pct / 100, 2) if is_fractional else bump_pct
                _LOGGER.debug(
                    "ThermoSmart '%s': Valve-Bump %s → %d%% → %d%%",
                    self.zone_name, valve_entity, bump_pct, target_pct,
                )
                try:
                    await self.hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": valve_entity, "value": bump_val},
                        blocking=False,
                    )
                except Exception:
                    pass  # bump is a transient motor-unstick; not tracked in stats
                await asyncio.sleep(TPI_VALVE_BUMP_DELAY)

            if current_pct == target_pct:
                # Already at target — no write needed; counts as a "no-op success".
                # Not added to stats because no command was issued.
                continue

            write_val = round(target_pct / 100, 2) if is_fractional else target_pct
            _LOGGER.debug(
                "ThermoSmart '%s': Ventil %s → %d%%",
                self.zone_name, valve_entity, target_pct,
            )
            # Await the service call so exceptions are captured in stats.
            # blocking=False means HA does not wait for device acknowledgement (fire-and-forget
            # at device level), but the Python coroutine returns quickly after queuing the call.
            _valve_exc = None
            try:
                await self.hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": valve_entity, "value": write_val},
                    blocking=False,
                )
            except Exception as err:
                _LOGGER.debug(
                    "ThermoSmart '%s': valve write failed for %s: %s",
                    self.zone_name, valve_entity, err,
                )
                _valve_exc = err
            stats.record(_valve_exc)

        return stats

    # ── External Temperature Input (TRVZB) ───────────────────────────

    async def _async_write_external_temp(self, cfg: dict, recommendation: dict) -> None:
        """Schreibt die Raumtemperatur direkt in die external_temperature_input-Entity des TRV.

        Beim Sonoff TRVZB (und ähnlichen TRVs) gibt es eine number-Entity
        'external_temperature_input'. Wird dort die echte Raumtemperatur eingetragen,
        nutzt die TRV-Firmware diesen Wert statt des internen (oft ungenauen) Sensors.
        Das macht die TRV-eigene Regelung deutlich präziser und vermeidet
        Konflikte zwischen TRV-interner Logik und ThermoSmart-Kalibrierung.

        Schreibt nur wenn Wert sich um mehr als 0.5°C geändert hat.
        Klemmt auf 0–99.9°C (TRVZB-Wertebereich).

        Nur bei aktiver Steuerung (Active Control): Im Beobachtungsmodus schreibt
        ThermoSmart nichts auf die Geräte — der Wert würde sonst die interne
        Regelung eines TRV beeinflussen, der noch von seinem eigenen Controller
        gesteuert wird. Learning liest die konfigurierten HA-Temperatursensoren,
        nicht diese Entity, und ist davon nicht betroffen.
        """
        if not self._active_control:
            return

        room_temp = recommendation.get("current_temp")
        if room_temp is None:
            return
        # The TRV should see the real room reading, not the EMA-smoothed control value.
        raw_room_temp = self._read_raw_avg_sensor(cfg.get("temp_sensors", []))
        if raw_room_temp is not None:
            room_temp = raw_room_temp
        device_settings = cfg.get(CONF_DEVICE_SETTINGS, {})
        threshold = float(cfg.get(CONF_EXT_TEMP_THRESHOLD, 0.5))

        for climate_id in cfg.get("climate_entities", []):
            settings = device_settings.get(climate_id, {})
            ext_entity = settings.get(CONF_DEV_EXT_TEMP_ENTITY)
            if not settings.get(CONF_DEV_FEED_EXT_TEMP, False) or not ext_entity:
                continue

            state = self.hass.states.get(ext_entity)
            if state is None or state.state in ("unavailable", "unknown"):
                continue

            # This is a `number` entity — number.set_value performs no unit
            # conversion of its own (unlike climate.set_temperature), so the
            # value read/written here must use this entity's own
            # unit_of_measurement, not the HA system unit.
            ext_unit = state.attributes.get("unit_of_measurement")
            current_val = to_internal_temperature_c(self.hass, state.state, unit=ext_unit)

            if current_val is not None and abs(current_val - room_temp) < threshold:
                continue

            clamped = round(max(0.0, min(99.9, room_temp)), 1)
            write_value = clamped
            if ext_unit in (UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.KELVIN):
                write_value = round(
                    TemperatureConverter.convert(clamped, UnitOfTemperature.CELSIUS, ext_unit), 1)
            _LOGGER.debug(
                "ThermoSmart '%s': TRVZB external_temperature_input → %.1f°C (%s)",
                self.zone_name, clamped, ext_entity,
            )
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": ext_entity, "value": write_value},
                    blocking=False,
                )
            )
