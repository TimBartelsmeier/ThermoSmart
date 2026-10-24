"""ThermoSmart Coordinator – Hauptkoordinator für eine Heizzone."""
from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime, timedelta

from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .temperature_units import to_internal_temperature_c, is_plausible_temperature_c
from .tpi import compute_tpi, duty_to_setpoint
from .const import (
    DOMAIN,
    CONF_CONTROL_INTERVAL,
    CONF_ZONE_CONTROL_MODE,
    CONF_DEVICE_SETTINGS,
    CONF_TRV_SETPOINT_DEBOUNCE,
    CONF_TRV_SETPOINT_SETTLING,
    CONF_DEV_ACCEPT_SETPOINT_CHANGES,
    CONF_DEV_CONTROL_MODE,
    CONTROL_MODE_INHERIT,
    CONTROL_MODE_DIRECT_VALVE,
    CONTROL_MODE_SETPOINT,
    CONTROL_MODE_TPI_BOOST,
    CONF_VACATION_TEMP,
    NOISE_FILTER_SPIKE_THRESHOLD,
    NOISE_FILTER_EMA_ALPHA,
    CONF_EXT_TEMP_THRESHOLD,
    TEMP_FROST_PROTECTION,
    EMA_1H_ALPHA,
    HEATING_FAILURE_DELAY_MIN,
    HEATING_FAILURE_SLOPE_THRESH,
    HEATING_FAILURE_CMD_DELTA,
    DISPATCH_FAILURE_SUPPORT_EVENT_THRESHOLD,
    TPI_MAX_BOOST_CELSIUS,
    TPI_COEF_INT_DEFAULT,
    TPI_COEF_EXT_DEFAULT,
)
from .device_profiles import DeviceProfile
from .learning.clock import Clock, SystemClock
from .target import (
    MANUAL_SOURCES,
    SOURCE_DIAL,
    SOURCE_EVENT,
    SOURCE_PRESET,
    TargetState,
    preset_temperature,
)
from .schedule import (
    ScheduleError,
    ScheduleOccurrence,
    ScheduleRunner,
    async_load_schedule,
    event_preset,
    resolve_target,
)
from .weather_engine import WeatherEngine
from .learning_engine import LearningEngine
from .window import WindowMixin
from .suspension import held_at, resolve
from .season import SeasonMixin
from .trv_control import TRVControlMixin, _DispatchStats
from .maintenance import MaintenanceMixin

_LOGGER = logging.getLogger(__name__)

# TPI coef_int step-limiting (Phase 19D) — max change per coordinator cycle (~5 min).
# Derivation: at max 3 °C deficit, 0.10 step → 30 % duty change per cycle.  The
# full default→learned transition (|0.60 − 0.20| = 0.40) takes 4 cycles ≈ 20 min,
# which is well within a room's thermal time constant (30-120 min).
# Symmetric up/down: stepping up (more heating) is equally rate-limited to prevent
# abrupt duty increases on stale→valid recovery.
_TPI_COEF_INT_MAX_STEP_UP: float = 0.10
_TPI_COEF_INT_MAX_STEP_DOWN: float = 0.10
_TPI_COEF_INT_CLAMP_MIN: float = 0.15   # mirrors estimate_coefficients lower bound
_TPI_COEF_INT_CLAMP_MAX: float = 1.2    # mirrors estimate_coefficients upper bound


class ControlAdaptationMode:
    """Derived control mode based on the two user-visible switches.

    Invariants (computed each cycle, never stored as state):
      INACTIVE           — Learning OFF + Active Control OFF: no ThermoSmart influence
      SHADOW_ONLY        — Learning ON  + Active Control OFF: observe and learn, no service calls
      DETERMINISTIC      — Learning OFF + Active Control ON:  TPI control, static defaults only
      ADAPTIVE           — Learning ON  + Active Control ON:  full adaptive Learning control
    """
    INACTIVE      = "inactive"
    SHADOW_ONLY   = "shadow_only"
    DETERMINISTIC = "deterministic"
    ADAPTIVE      = "adaptive"

    @staticmethod
    def derive(learning_mode_on: bool, active_control_on: bool) -> str:
        if learning_mode_on and active_control_on:
            return ControlAdaptationMode.ADAPTIVE
        if learning_mode_on:
            return ControlAdaptationMode.SHADOW_ONLY
        if active_control_on:
            return ControlAdaptationMode.DETERMINISTIC
        return ControlAdaptationMode.INACTIVE


class ThermoSmartCoordinator(
    DataUpdateCoordinator,
    TRVControlMixin,
    MaintenanceMixin,
    SeasonMixin,
    WindowMixin,
):
    """Koordinator für eine einzelne Heizzone.

    Erbt von:
      TRVControlMixin    – Geräteerkennung, Kalibrierung, Quirks, Setpoint-Schreiben
      MaintenanceMixin   – wöchentliche Ventilübung
      SeasonMixin        – Sommer-Erkennung und Frostschutz
      WindowMixin        – Fenstererkennung mit Verzögerungslogik
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        weather_engine: WeatherEngine,
        learning_engine: LearningEngine,
        *,
        clock: "Clock | None" = None,
    ) -> None:
        super().__init__(
            hass, _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=(
                {**entry.data, **entry.options}.get(CONF_CONTROL_INTERVAL, 5)
            )),
        )
        self.entry = entry
        self.weather_engine = weather_engine
        self.learning_engine = learning_engine

        # Steuerungszustand
        self._active_control: bool = False
        # The single desired target of the zone (see target.py).
        self.target = TargetState()
        self.target.add_listener(self._on_target_changed)
        self._manual_change_pending: bool = False
        # Scheduling (events fire at exact times via the runner, see schedule.py).
        self._scheduling_enabled: bool = True
        self._scheduling_disabled_until: datetime | None = None
        self.schedule_error: str | None = None
        self.scheduler = ScheduleRunner(
            hass,
            now=self._now_local,
            is_active=self._scheduling_active_at,
            on_event=self._on_schedule_event,
            on_change=lambda: self.async_update_listeners(),
        )
        self._vacation_mode: bool = False

        # Event-Listener
        self._event_unsub: list = []

        # Fenster-Tracking (genutzt von WindowMixin)
        self._window_open_at: dict[str, datetime] = {}
        self._window_close_at: dict[str, datetime] = {}
        self._window_open_temp: dict[str, float] = {}
        # Cancel functions for scheduled delay-expiry refreshes (one slot per sensor)
        self._window_delay_cancel: dict[str, Callable[[], None]] = {}

        self._last_written_setpoints: dict[str, float] = {}
        self._trv_write_settle_until: dict[str, float] = {}
        self._manual_setpoint_cancel: Callable[[], None] | None = None
        self._pending_manual_setpoint: tuple[str, float] | None = None
        self._manual_setpoint_generation = 0
        # TRVs switched to HVAC off because of an open window (restored on close).
        self._window_off_entities: set[str] = set()
        # Boost tracking per zone (keyed by zone_id; used by TRVControlMixin._apply_temperature).
        # Must be initialized before the first coordinator refresh to avoid AttributeError.
        self._boost_active: dict = {}

        # Learning Support Critical Event dedupe state — tracks the previous cycle's
        # window/summer/manual hold flags so _maybe_record_support_hold_events()
        # emits a landmark only on a genuine transition, never every cycle.
        # Event production only; never read by any control/decision path.
        self._support_event_window_hold_active: bool = False
        self._support_event_summer_hold_active: bool = False
        # TRV dispatch event dedupe: fires only when the (event_type, reason,
        # setpoint) signature genuinely changes from the previous cycle —
        # a zone sitting at target for hours must not spam one event per
        # cycle. "Recommendation only" (active_control off) uses its own
        # transition flag, matching the hold-event pattern above.
        self._support_event_recommendation_only_active: bool = False
        self._support_event_last_trv_signature: tuple | None = None
        self._support_event_last_sent_setpoint: float | None = None
        # Boost event dedupe: started/ended follow a boolean transition flag
        # (like window/summer/manual above); blocked uses a small signature
        # cache (reason only) since it can persist across many cycles while
        # inactive, needing content-based dedup rather than a pure transition.
        self._support_event_boost_active: bool = False
        self._support_event_last_boost_signature: tuple | None = None
        # Room-temperature sensor dedupe: unavailable/restored follow a
        # boolean transition flag (True = last known state was available);
        # the fallback-in-use signal uses a small signature cache so it
        # records once per fallback episode, not once per cycle while the
        # room sensor stays unavailable.
        self._support_event_room_sensor_available: bool = True
        self._support_event_last_fallback_signature: tuple | None = None

        # Kalibrierung (genutzt von TRVControlMixin)
        self._calibration_offsets: dict[str, float] = {}
        self._trv_offline: set[str] = set()
        self._last_logged_control_modes: tuple[str, ...] | None = None
        # Per-entity consecutive setpoint-dispatch-failure counter (Device
        # Compatibility P1) — reset to 0 on any successful dispatch for that
        # entity; drives a single, rate-limited Support Critical Event after
        # DISPATCH_FAILURE_SUPPORT_EVENT_THRESHOLD consecutive failures.
        self._dispatch_consecutive_failures: dict[str, int] = {}
        # Entities already warned about missing TARGET_TEMPERATURE support
        # (dual-setpoint / HEAT_COOL-only devices) — warn once, not every cycle.
        self._dual_setpoint_warned: set[str] = set()

        # Device profiles — populated by async_detect_device_entities (TRVControlMixin)
        self._device_profiles: dict[str, DeviceProfile] = {}

        # Sommer (gesetzt vom System-Entry über set_system_state)
        self._is_summer: bool = False

        # Wartung (genutzt von MaintenanceMixin)
        self._last_maintenance: datetime | None = None
        self._maintenance_running: bool = False
        self._maintenance_task: object = None  # Optional[asyncio.Task] — set in maintenance.py

        # Sensor-Filterung
        self._sensor_ema: dict[str, float] = {}
        self._sensor_noise_count: dict[str, int] = {}

        # Live-Cache für sofortige Card-Aktualisierung (unabhängig vom 5-min-Zyklus)
        self._live_temp: float | None = None
        self._live_humidity: float | None = None
        # Raw room temperature at the last full cycle; reference for
        # sensor-triggered refreshes.
        self._cycle_ref_temp: float | None = None

        # Temperatur-Trend
        self._indoor_temp_prev: tuple[datetime, float] | None = None
        self._indoor_temp_slope: float | None = None
        self._slope_sample_fresh: bool = False
        self._ema_1h: float | None = None

        # Slope-basierte Fenstererkennung (genutzt von WindowMixin._check_window_slope)
        self._slope_win_ema: float | None = None
        self._slope_win_consec: int = 0
        self._slope_window_active: bool = False

        # Heizungsausfall-Erkennung
        self._heating_failure_since: datetime | None = None
        self._heating_failure_notified: bool = False

        # Restore initialization barrier: set True the first time Active Control
        # RestoreEntity calls its setter after restore.  Until then, boost is blocked.
        # Learning mode is read from config (not a RestoreEntity) → not in barrier.
        self._active_control_initialized: bool = False

        # Learning runtime is attached with adaptive-control capability. The
        # effective adaptation mode is derived per cycle from the Learning
        # and Active Control switches; with Active Control off, it remains
        # shadow-only.
        self._learning_shadow = None
        # Authoritative live decision record for the last completed cycle (Phase B1).
        # Built pre-dispatch and completed post-dispatch; consumed by compute_decision_trace_safe().
        self._last_live_decision: object = None

        # Coordinator-level decision identity (baseline, Learning-independent).
        # Stable across cycles as long as the decision type and effective target don't
        # change materially (> 0.05 °C); resets on Reload/Unload (correct: new session).
        # Uses make_decision_id() from capture.py as the single ID authority.
        self._coord_decision_id: object = None      # Optional[DecisionId]
        self._coord_decision_target: object = None   # Optional[float]
        self._coord_decision_type: object = None     # Optional[str]

        # TPI coef_int step-limiting state (transient, per zone, resets on restart).
        # _tpi_coef_int_smoothed: last used coef_int; initialized to deterministic default
        # so the first cycle starts from a known safe state regardless of Learning predictions.
        # _tpi_duty_previous: last duty cycle for trace/audit (None before first cycle).
        self._tpi_coef_int_smoothed: float = TPI_COEF_INT_DEFAULT
        self._tpi_duty_previous: float | None = None

        # Injectable clock — replaced by FakeClock in simulation/tests.
        # All fachliche Zeitaufrufe in this coordinator must go through self._clock.
        # Production callers pass nothing → SystemClock; tests/simulation pass FakeClock.
        self._clock: Clock = clock if clock is not None else SystemClock()

    # ── Eigenschaften ────────────────────────────────────────────────

    def attach_learning_shadow(self, shadow) -> None:
        """Attach the Learning runtime (adaptive-control capable; effective mode
        is derived per cycle from the Learning and Active Control switches)."""
        self._learning_shadow = shadow

    def _now_local(self):
        """Return current local time derived from the injected clock.

        Uses dt_util.as_local() for timezone conversion only — not as a time source.
        All fachliche local-time calls in coordinator and its mixins must use this.
        """
        return dt_util.as_local(self._clock.now_utc())

    @property
    def zone_id(self) -> str:
        return self.entry.entry_id

    @property
    def zone_name(self) -> str:
        return self.entry.data.get("name", "Zone")

    @property
    def zone_cfg(self) -> dict:
        return {**self.entry.data, **self.entry.options}

    # ── Scheduling ──────────────────────────────────────────────────

    @property
    def scheduling_enabled(self) -> bool:
        return self._scheduling_enabled

    @property
    def scheduling_disabled_until(self) -> datetime | None:
        return self._scheduling_disabled_until

    def _scheduling_active_at(self, now: datetime) -> bool:
        until = self._scheduling_disabled_until
        return self._scheduling_enabled and (until is None or now >= until)

    @property
    def scheduling_active(self) -> bool:
        """Events are applied: switched on and not paused by 'disabled until'."""
        return self._scheduling_active_at(self._now_local())

    def set_scheduling_enabled(self, enabled: bool) -> None:
        self._scheduling_enabled = bool(enabled)
        _LOGGER.info("ThermoSmart '%s': scheduling %s",
                     self.zone_name, "enabled" if enabled else "disabled")

    def set_scheduling_disabled_until(self, until: datetime | None) -> None:
        self._scheduling_disabled_until = until
        _LOGGER.info("ThermoSmart '%s': scheduling disabled until %s",
                     self.zone_name, until.isoformat() if until else "– (not paused)")

    async def async_load_schedule(self) -> None:
        """(Re-)read this zone's events from the schedule YAML.

        An invalid file never stops the zone: it runs without events and the
        reason is logged and exposed as ``schedule_error``.
        """
        try:
            events = await async_load_schedule(self.hass, self.entry)
            self.schedule_error = None
        except ScheduleError as err:
            events = []
            self.schedule_error = str(err)
            _LOGGER.error("ThermoSmart '%s': schedule not loaded – %s",
                          self.zone_name, err)
        self.scheduler.set_events(events)

    def start_scheduler(self) -> None:
        self.scheduler.start()

    def _on_schedule_event(self, occurrence: ScheduleOccurrence) -> None:
        event = occurrence.event
        value = resolve_target(event.target, self.zone_cfg)
        preset = event_preset(event.target)
        try:
            self.set_target(value, SOURCE_EVENT, preset=preset)
        except ValueError as err:
            _LOGGER.warning("ThermoSmart '%s': schedule event %s ignored – %s",
                            self.zone_name, event.key, err)
            return
        self.hass.async_create_task(self.async_request_refresh())

    # ── Setter ──────────────────────────────────────────────────────

    def set_active_control(self, active: bool) -> None:
        self._active_control = active
        self._active_control_initialized = True
        _LOGGER.info(
            "ThermoSmart '%s': Aktive Steuerung %s",
            self.zone_name,
            "AN – Thermostat wird gesteuert" if active else "AUS – Beobachtungsmodus",
        )

    def set_target(
        self, value: float, source: str, *, preset: str | None = None
    ) -> bool:
        """Write the zone's desired target (single writer; last writer wins).

        Raises ValueError for values outside the supported range.
        """
        return self.target.set(value, source, preset=preset, now=self._now_local())

    def set_preset(self, preset: str) -> bool:
        """Set the desired target to the temperature configured for ``preset``."""
        return self.set_target(
            preset_temperature(self.zone_cfg, preset), SOURCE_PRESET, preset=preset
        )

    def _on_target_changed(self, new, previous) -> None:
        _LOGGER.info(
            "ThermoSmart '%s': target %.1f°C → %.1f°C (source: %s%s)",
            self.zone_name, previous.value, new.value, new.source,
            f", preset {new.preset}" if new.preset else "",
        )
        if new.source in MANUAL_SOURCES:
            self._manual_change_pending = True

    def set_system_state(self, *, vacation: bool, summer: bool) -> bool:
        """Apply the system-wide vacation/summer state; returns True when it changed."""
        vacation, summer = bool(vacation), bool(summer)
        changed = vacation != self._vacation_mode or summer != self._is_summer
        self._vacation_mode = vacation
        self._is_summer = summer
        if changed:
            _LOGGER.info(
                "ThermoSmart '%s': system state – vacation %s, summer %s",
                self.zone_name, "ON" if vacation else "OFF", "ON" if summer else "OFF",
            )
        return changed
    @property
    def suspended(self) -> bool:
        """True while summer or vacation mode holds the TRVs."""
        return self._is_summer or self._vacation_mode

    # ── Event-Listener ───────────────────────────────────────────────

    def setup_event_listeners(self) -> None:
        cfg = self.zone_cfg
        window_sensors: set[str] = set(s for s in cfg.get("window_sensors", []) if s)
        climate_entities: set[str] = set(e for e in cfg.get("climate_entities", []) if e)

        all_tracked = window_sensors
        if all_tracked:
            @callback
            def _handle_state_change(event) -> None:
                old = event.data.get("old_state")
                new = event.data.get("new_state")
                if old is None or new is None or old.state == new.state:
                    return
                entity_id = event.data["entity_id"]
                now = self._now_local()

                if entity_id in window_sensors:
                    if new.state == "on":
                        self._window_open_at[entity_id] = now
                        # Cancel any pending timer (leftover close-delay or open-delay)
                        _cancel = self._window_delay_cancel.pop(entity_id, None)
                        if _cancel is not None:
                            _cancel()
                        self._window_close_at.pop(entity_id, None)
                        current = self._read_avg_sensor(cfg.get("temp_sensors", []), is_temperature=True)
                        if current is not None:
                            self._window_open_temp[entity_id] = current
                        # Schedule a coordinator refresh exactly when the open delay expires
                        # so window-open mode activates at the configured time, not at the
                        # next regular 5-minute coordinator cycle.
                        open_delay_secs = cfg.get("window_open_delay", 5) * 60

                        @callback
                        def _open_delay_expired(_now, _eid=entity_id):
                            self._window_delay_cancel.pop(_eid, None)
                            self.hass.async_create_task(self.async_request_refresh())

                        self._window_delay_cancel[entity_id] = async_call_later(
                            self.hass, open_delay_secs, _open_delay_expired
                        )
                    else:
                        # Cancel any pending open-delay timer (sensor closed before delay elapsed)
                        _cancel = self._window_delay_cancel.pop(entity_id, None)
                        if _cancel is not None:
                            _cancel()
                        if entity_id in self._window_open_at:
                            opened_at = self._window_open_at[entity_id]
                            open_delay_td = timedelta(minutes=cfg.get("window_open_delay", 5))

                            # Close-Timer nur setzen wenn open_delay tatsächlich abgelaufen war.
                            # Schließt das Fenster VOR Ablauf des open_delay, darf weder
                            # eine Heizpause noch ein close_delay ausgelöst werden.
                            if (now - opened_at) >= open_delay_td:
                                self._window_close_at[entity_id] = now
                                # Schedule refresh when close delay expires so the TRV
                                # returns to normal at the configured time, not the next cycle.
                                close_delay_secs = cfg.get("window_close_delay", 2) * 60

                                @callback
                                def _close_delay_expired(_now, _eid=entity_id):
                                    self._window_delay_cancel.pop(_eid, None)
                                    self.hass.async_create_task(self.async_request_refresh())

                                self._window_delay_cancel[entity_id] = async_call_later(
                                    self.hass, close_delay_secs, _close_delay_expired
                                )

                            # LE v1 window-cooling observation removed (frozen no-op).
                            self._window_open_temp.pop(entity_id, None)
                        self._window_open_at.pop(entity_id, None)

                _LOGGER.info(
                    "ThermoSmart '%s': %s geändert (%s → %s) – Sofort-Update",
                    self.zone_name, entity_id, old.state, new.state,
                )
                self.hass.async_create_task(self.async_request_refresh())

            cancel = async_track_state_change_event(
                self.hass, list(all_tracked), _handle_state_change
            )
            self._event_unsub.append(cancel)

        if climate_entities:
            @callback
            def _handle_trv_change(event) -> None:
                new = event.data.get("new_state")
                if new is None:
                    return
                # TRV-only zone: keep display cache current when no external sensor.
                if not temp_sensors:
                    trv_t = self._read_trv_avg_temp(list(climate_entities))
                    if trv_t is not None:
                        self._live_temp = trv_t
                        self.async_update_listeners()
                entity_id = event.data["entity_id"]
                self._handle_trv_setpoint_change(
                    entity_id, event.data.get("old_state"), new, cfg
                )

            cancel_trv = async_track_state_change_event(
                self.hass, list(climate_entities), _handle_trv_change
            )
            self._event_unsub.append(cancel_trv)

        # Temp- und Feuchtigkeitssensoren: Live-Cache für sofortige Card-Aktualisierung
        temp_sensors: set[str] = set(s for s in cfg.get("temp_sensors", []) if s)
        humidity_sensors: set[str] = set(s for s in cfg.get("humidity_sensors", []) if s)
        all_indoor_sensors = temp_sensors | humidity_sensors
        if all_indoor_sensors:
            @callback
            def _handle_sensor_change(event) -> None:
                new = event.data.get("new_state")
                if new is None or new.state in ("unknown", "unavailable"):
                    return
                if temp_sensors:
                    val = self._read_raw_avg_sensor(list(temp_sensors))
                    if val is not None:
                        self._live_temp = val
                        self._maybe_refresh_on_temp_change(val, cfg)
                if humidity_sensors:
                    val = self._read_avg_sensor(list(humidity_sensors))
                    if val is not None:
                        self._live_humidity = val
                self.async_update_listeners()

            cancel_sensors = async_track_state_change_event(
                self.hass, list(all_indoor_sensors), _handle_sensor_change
            )
            self._event_unsub.append(cancel_sensors)

        _LOGGER.debug(
            "ThermoSmart '%s': Event-Listener registriert (%d Fenster, %d TRVs)",
            self.zone_name, len(all_tracked), len(climate_entities),
        )

    def cleanup_event_listeners(self) -> None:
        self.scheduler.stop()
        for cancel in self._event_unsub:
            cancel()
        self._event_unsub.clear()
        for cancel in self._window_delay_cancel.values():
            cancel()
        self._window_delay_cancel.clear()
        if self._manual_setpoint_cancel is not None:
            self._manual_setpoint_cancel()
            self._manual_setpoint_cancel = None
        self._pending_manual_setpoint = None
        self._manual_setpoint_generation += 1
        self._trv_write_settle_until.clear()

    def _maybe_refresh_on_temp_change(self, raw_temp: float, cfg: dict) -> None:
        """Trigger a full cycle once the room temperature moved by the threshold.

        The reference is the raw temperature at the last full cycle, so slow drifts
        accumulate.
        """
        ref = self._cycle_ref_temp
        if ref is None or not self._active_control or self.suspended:
            return
        try:
            threshold = float(cfg.get(CONF_EXT_TEMP_THRESHOLD, 0.5))
        except (TypeError, ValueError):
            threshold = 0.5
        if abs(raw_temp - ref) < threshold:
            return
        _LOGGER.debug(
            "ThermoSmart '%s': room temperature %.1f°C moved ≥ %.1f°C since last cycle (%.1f°C) – refresh",
            self.zone_name, raw_temp, threshold, ref,
        )
        self.hass.async_create_task(self.async_request_refresh())

    def _mark_trv_write(self, entity_id: str, cfg: dict) -> None:
        """Suppress device echoes for the configured settling period after a write."""
        try:
            settling = max(0.0, float(cfg.get(CONF_TRV_SETPOINT_SETTLING, 5)))
        except (TypeError, ValueError):
            settling = 5.0
        if settling:
            self._trv_write_settle_until[entity_id] = time.monotonic() + settling
        else:
            self._trv_write_settle_until.pop(entity_id, None)

    def _trv_write_is_settling(self, entity_id: str) -> bool:
        deadline = self._trv_write_settle_until.get(entity_id)
        if deadline is None:
            return False
        if time.monotonic() < deadline:
            return True
        self._trv_write_settle_until.pop(entity_id, None)
        return False

    def _manual_setpoint_is_pending(self, entity_id: str) -> bool:
        return (
            self._pending_manual_setpoint is not None
            and self._pending_manual_setpoint[0] == entity_id
        )

    def _handle_trv_setpoint_change(
        self, entity_id: str, old_state, new_state, cfg: dict
    ) -> None:
        """Adopt physical TRV setpoint changes after debounce, or correct them."""
        if not self._active_control:
            return
            return
        if old_state is None or old_state.state in ("unknown", "unavailable"):
            return
        old_raw = old_state.attributes.get("temperature")
        new_raw = new_state.attributes.get("temperature")
        if old_raw is None or new_raw is None:
            if old_raw != new_raw:
                _LOGGER.debug(
                    "ThermoSmart '%s': ignoring %s state change (%s → %s): setpoint "
                    "attribute missing (old=%s, new=%s)",
                    self.zone_name, entity_id, old_state.state, new_state.state,
                    old_raw, new_raw,
                )
            return
        if self._trv_write_is_settling(entity_id):
            _LOGGER.debug(
                "ThermoSmart '%s': ignoring %s setpoint change %s → %s (settling after own write)",
                self.zone_name, entity_id, old_raw, new_raw,
            )
            return
        try:
            old_setpoint = to_internal_temperature_c(self.hass, old_raw)
            new_setpoint = to_internal_temperature_c(self.hass, new_raw)
        except (TypeError, ValueError):
            return
        if old_setpoint is None or new_setpoint is None or abs(new_setpoint - old_setpoint) < 0.05:
            return

        if self.suspended:
            # The TRVs are held (summer/vacation): the dial change is registered as a
            # manual change but not adopted; the TRV is driven back to the held value.
            self._manual_change_pending = True
            _LOGGER.info(
                "ThermoSmart '%s': physical setpoint change on %s (%.1f → %.1f°C) during "
                "%s mode – registered as manual change, not adopted; correcting back",
                self.zone_name, entity_id, old_setpoint, new_setpoint,
                "summer" if self._is_summer else "vacation",
            )
            self.hass.async_create_task(self.async_request_refresh())
            return

        settings = cfg.get(CONF_DEVICE_SETTINGS, {}).get(entity_id, {})
        if not settings.get(CONF_DEV_ACCEPT_SETPOINT_CHANGES, True):
            last_written = self._last_written_setpoints.get(entity_id)
            if last_written is not None and abs(new_setpoint - last_written) > 0.4:
                _LOGGER.info(
                    "ThermoSmart '%s': correcting manual TRV setpoint change on %s",
                    self.zone_name, entity_id,
                )
                self.hass.async_create_task(self.async_request_refresh())
            return

        self._pending_manual_setpoint = (entity_id, new_setpoint)
        self._manual_setpoint_generation += 1
        generation = self._manual_setpoint_generation
        if self._manual_setpoint_cancel is not None:
            self._manual_setpoint_cancel()
        try:
            debounce = max(0.0, float(cfg.get(CONF_TRV_SETPOINT_DEBOUNCE, 5)))
        except (TypeError, ValueError):
            debounce = 5.0

        @callback
        def _apply_manual_setpoint(_now) -> None:
            if generation != self._manual_setpoint_generation:
                return
            self._manual_setpoint_cancel = None
            pending = self._pending_manual_setpoint
            self._pending_manual_setpoint = None
            if pending is None:
                return
            if not self._active_control or self.suspended:
                return
            changed_entity, temperature = pending
            _LOGGER.info(
                "ThermoSmart '%s': adopting physical setpoint %.1f°C from %s",
                self.zone_name, temperature, changed_entity,
            )
            try:
                self.set_target(temperature, SOURCE_DIAL)
            except ValueError:
                _LOGGER.warning(
                    "ThermoSmart '%s': ignoring physical setpoint %.1f°C from %s "
                    "(outside the supported target range)",
                    self.zone_name, temperature, changed_entity,
                )
            self.hass.async_create_task(self.async_request_refresh())

        self._manual_setpoint_cancel = async_call_later(
            self.hass, debounce, _apply_manual_setpoint
        )

    # ── Authoritative Live Decision Builder ──────────────────────────

    def _get_coord_decision_id(self, recommendation, *, ts: str) -> object:
        """Return a stable coordinator-level decision_id for the current cycle.

        Uses the canonical ``make_decision_id()`` from the capture module — the same
        function that Learning's CaptureCoordinator uses — so there is exactly one ID
        authority.  The ID is stable across successive cycles as long as the decision
        type and effective target don't change materially (>0.05 °C threshold mirrors
        CaptureCoordinator._decision_changed).  It resets on Reload/Unload, which is
        correct: a new HA session starts a new decision lineage.

        When Learning is present, enrich_live_decision_pre_dispatch() will replace this ID
        with the Learning-generated one (which may be the same or a continuation).  Without
        Learning, this ID is authoritative for the Record, Trace and any future Outcome binding.
        """
        try:
            from .learning.runtime.capture import make_decision_id, DecisionType
        except ImportError:
            return None
        target = recommendation.get("effective_target")
        # Map coordinator state to DecisionType for ID stability domain
        if recommendation.get("preheat_status") in ("heating", "preheating"):
            dec_type = DecisionType.PREHEAT
            dtype_str = "preheat"
        elif recommendation.get("learning_boost_adjusted"):
            dec_type = DecisionType.BOOST
            dtype_str = "boost"
        else:
            dec_type = DecisionType.NORMAL
            dtype_str = "normal"
        target_f = float(target) if target is not None else None
        # A new decision opens when type or target changes materially.
        # For no-target decisions (summer, window-open, absence) the type change is enough.
        type_changed = (dtype_str != self._coord_decision_type)
        target_changed = (
            self._coord_decision_id is None
            or target_f is None
            or self._coord_decision_target is None
            or abs(target_f - self._coord_decision_target) > 0.05
        )
        if self._coord_decision_id is None or type_changed or target_changed:
            self._coord_decision_id = make_decision_id(self.zone_id, dec_type, ts)
            self._coord_decision_target = target_f
            self._coord_decision_type = dtype_str
        return self._coord_decision_id

    def _build_baseline_live_decision(self, recommendation, *, ts, mode_str):
        """Create authoritative LiveDecisionRecord from coordinator recommendation dict.

        Always called regardless of Learning shadow existence.  Learning-specific fields
        (decision_id, onset_delay) remain None and are added by
        ``LearningShadowController.enrich_live_decision_pre_dispatch()`` when a shadow
        is attached.  Never raises — returns None on unexpected error.
        """
        try:
            from .learning.decision.contracts import LiveDecisionRecord
            boost_applied = bool(recommendation.get("learning_boost_adjusted"))
            baseline_sp = (
                recommendation.get("tpi_baseline_setpoint")
                if boost_applied
                else recommendation.get("trv_setpoint")
            )
            _tpi_diag = recommendation.get("tpi_coef_diag") or {}
            return LiveDecisionRecord(
                decision_id=self._get_coord_decision_id(recommendation, ts=ts),
                zone_id=self.zone_id,
                ts=ts,
                mode=mode_str,
                baseline_setpoint_c=(float(baseline_sp) if baseline_sp is not None else None),
                final_setpoint_c=(float(recommendation["trv_setpoint"])
                                  if recommendation.get("trv_setpoint") is not None else None),
                boost_applied=boost_applied,
                boost_candidate_c=(float(recommendation["_boost_candidate_c"])
                                   if recommendation.get("_boost_candidate_c") is not None
                                   else None),
                boost_applied_c=(float(recommendation["_boost_applied_c"])
                                 if recommendation.get("_boost_applied_c") is not None
                                 else None),
                boost_rejected_reason=recommendation.get("_boost_rejection_reason"),
                boost_evaluation_status=recommendation.get(
                    "_boost_evaluation_status", "unknown"),
                preheat_minutes=float(recommendation.get("preheat_minutes") or 0.0),
                preheat_status=recommendation.get("preheat_status"),
                early_cutoff_state=recommendation.get("early_cutoff_state"),
                heat_rate_c_per_h=_tpi_diag.get("heat_rate_c_per_h"),
                heat_rate_confidence=_tpi_diag.get("hr_confidence"),
                heat_rate_source=recommendation.get("tpi_coef_source"),
                onset_delay_min=None,       # enriched by shadow if available
                onset_delay_source=None,    # enriched by shadow if available
            )
        except Exception:
            return None

    # ── Hauptschleife ────────────────────────────────────────────────

    async def _async_update_data(self) -> dict:
        try:
            # Reset per-cycle live decision record — prevents cross-cycle staleness
            # when the shadow is absent or removed between cycles.
            self._last_live_decision = None
            cfg = self.zone_cfg

            weather_data = await self.weather_engine.async_get_data()

            recommendation = await self._compute_recommendation(cfg, weather_data)

            effective_summer = self._is_summer
            recommendation["is_summer"] = effective_summer
            recommendation["vacation_mode"] = self._vacation_mode

            # Derive adaptation mode once per cycle from the two user-visible switches.
            # All Learning adaptive reads below are gated on _learning_mode_on so that
            # Learning OFF → only deterministic defaults influence real control.
            _learning_mode_on = bool(cfg.get("learning_enabled", True))
            _active_control_on = self._active_control
            _adaptation_mode = ControlAdaptationMode.derive(_learning_mode_on, _active_control_on)
            recommendation["adaptation_mode"] = _adaptation_mode

            # B2b-bootstrap: pass zone-configured device prior to the boost model so
            # it can run initial controlled trials before any adaptive evidence exists.
            _bsp = cfg.get("boost_bootstrap_prior_c")
            if _bsp is not None:
                try:
                    recommendation["boost_bootstrap_prior_c"] = float(_bsp)
                except (TypeError, ValueError):
                    pass

            # TPI/TRV calculations use effective_target (None: window release).
            target = recommendation.get("effective_target")
            current_temp = recommendation.get("current_temp")
            if target is not None:
                # TPI-Koeffizienten: Learning authoritative (Phase 19D).
                # LE v1 path (learning_engine.get_tpi_coefficients) is kept only as
                # emergency fallback when the Learning shadow is not yet attached.
                if _learning_mode_on and self._learning_shadow is not None:
                    # Learning is authoritative for TPI coefficients (Phase 19D).
                    # read_tpi_coefficients_safe() returns the blended *candidate* coef_int.
                    # The coordinator applies step-limiting to produce the *used* value so
                    # valid→stale and stale→valid transitions never cause abrupt duty jumps.
                    _coef_cand, coef_ext, _tpi_hl_rate, _tpi_src, _tpi_diag = (
                        self._learning_shadow.read_tpi_coefficients_safe()
                    )
                    # Step-limiting: used approaches candidate by at most one step per cycle.
                    _prev = self._tpi_coef_int_smoothed
                    _delta = _coef_cand - _prev
                    _step = max(-_TPI_COEF_INT_MAX_STEP_DOWN,
                                min(_TPI_COEF_INT_MAX_STEP_UP, _delta))
                    coef_int = max(_TPI_COEF_INT_CLAMP_MIN,
                                   min(_TPI_COEF_INT_CLAMP_MAX, _prev + _step))
                    self._tpi_coef_int_smoothed = coef_int
                    # Enrich diag with candidate/previous/transition for trace
                    _tpi_diag = dict(_tpi_diag)
                    _tpi_diag.update({
                        "coef_int_candidate": round(_coef_cand, 4),
                        "coef_int_previous": round(_prev, 4),
                        "transition_applied": abs(_step) > 1e-5,
                        "transition_reason": (
                            "step_limited" if abs(_step) < abs(_delta) - 1e-5 else "converged"
                        ),
                        "coef_int_max_step_up": _TPI_COEF_INT_MAX_STEP_UP,
                        "coef_int_max_step_down": _TPI_COEF_INT_MAX_STEP_DOWN,
                    })
                    recommendation["tpi_coef_source"] = _tpi_src
                    recommendation["tpi_hl_rate"] = _tpi_hl_rate
                    recommendation["tpi_coef_int"] = coef_int
                    recommendation["tpi_coef_ext"] = coef_ext
                    recommendation["tpi_coef_diag"] = _tpi_diag
                else:
                    # Learning shadow not attached (disabled). Deterministic defaults only —
                    # LE v1 _heat_loss_ema must never be read for control.
                    coef_int, coef_ext = TPI_COEF_INT_DEFAULT, TPI_COEF_EXT_DEFAULT
                    self._tpi_coef_int_smoothed = TPI_COEF_INT_DEFAULT  # keep in sync
                    recommendation["tpi_coef_source"] = "deterministic_baseline"
                    recommendation["tpi_hl_rate"] = None
                    recommendation["tpi_coef_int"] = coef_int
                    recommendation["tpi_coef_ext"] = coef_ext
                    recommendation["tpi_coef_diag"] = {
                        "coef_int_default": TPI_COEF_INT_DEFAULT,
                        "coef_int_candidate": TPI_COEF_INT_DEFAULT,
                        "coef_int_previous": TPI_COEF_INT_DEFAULT,
                        "coef_int_learned": None, "blend_weight": 0.0,
                        "relative_only": False, "outdoor_context_available": False,
                        "heat_rate_c_per_h": None, "heat_loss_c_per_h": None,
                        "hr_confidence": None, "hl_confidence": None,
                        "transition_applied": False,
                        "transition_reason": "no_shadow",
                        "coef_int_max_step_up": _TPI_COEF_INT_MAX_STEP_UP,
                        "coef_int_max_step_down": _TPI_COEF_INT_MAX_STEP_DOWN,
                    }
                if current_temp is not None:
                    duty_cycle = compute_tpi(
                        target, current_temp,
                        weather_data.get("temperature"),
                        coef_int, coef_ext,
                    )
                else:
                    duty_cycle = 0.0

                zone_mode = cfg.get(CONF_ZONE_CONTROL_MODE, CONTROL_MODE_TPI_BOOST)
                boosted_setpoint = duty_to_setpoint(
                    target, duty_cycle, TPI_MAX_BOOST_CELSIUS
                )
                trv_setpoint = (
                    target if zone_mode == CONTROL_MODE_SETPOINT else boosted_setpoint
                )
                recommendation["trv_setpoint"] = trv_setpoint
                recommendation["tpi_boosted_setpoint"] = boosted_setpoint
                # boost_offset_c / boost_factor: prediction semantics — set below from
                # _boost_candidate_c (raw Learning proposal, pre-guard) after resolver runs.
                # applied_boost_offset_c: confirmed dispatch truth — updated post-dispatch.
                recommendation["boost_offset_c"] = 0.0
                recommendation["boost_factor"] = 1.0
                recommendation["applied_boost_offset_c"] = 0.0
                recommendation["tpi_duty_cycle"] = round(duty_cycle, 1)
                recommendation["tpi_coef_int"] = round(coef_int, 3)
                recommendation["tpi_coef_ext"] = round(coef_ext, 4)
                device_settings = cfg.get(CONF_DEVICE_SETTINGS, {})
                modes_by_device = {}
                for eid in cfg.get("climate_entities", []):
                    setting = device_settings.get(eid, {})
                    device_mode = setting.get(
                        CONF_DEV_CONTROL_MODE, CONTROL_MODE_INHERIT
                    )
                    modes_by_device[eid] = (
                        device_mode if device_mode != CONTROL_MODE_INHERIT else zone_mode
                    )
                effective_modes = set(modes_by_device.values())
                recommendation["tpi_valve_direct"] = (
                    bool(effective_modes)
                    and effective_modes == {CONTROL_MODE_DIRECT_VALVE}
                )
                recommendation["tpi_control_entities"] = [
                    eid for eid, effective_mode in modes_by_device.items()
                    if effective_mode == CONTROL_MODE_TPI_BOOST
                ]
                if not recommendation["tpi_control_entities"]:
                    # No device consumes a boosted setpoint: don't advertise one
                    # (display, boost-active tracking, learning baseline).
                    recommendation["trv_setpoint"] = target
                recommendation["zone_control_type"] = (
                    "mixed" if len(effective_modes) > 1 else
                    "direct_valve" if effective_modes == {CONTROL_MODE_DIRECT_VALVE} else
                    "setpoint"
                )
                if len(effective_modes) > 1:
                    _modes = tuple(sorted(effective_modes))
                    if _modes != self._last_logged_control_modes:
                        _LOGGER.info(
                            "ThermoSmart '%s': devices use mixed control strategies: %s",
                            self.zone_name, list(_modes),
                        )
                        self._last_logged_control_modes = _modes
                else:
                    self._last_logged_control_modes = None
                # Enrich tpi_coef_diag with duty-level trace for transition audit.
                # duty_candidate: duty that would result from using candidate coef_int directly.
                # duty_previous: duty from the previous cycle (for delta/stability verification).
                # duty_used: actual duty applied this cycle (with step-limited coef_int).
                _tpi_d = recommendation.get("tpi_coef_diag")
                if _tpi_d is not None and current_temp is not None:
                    _cand_ci = _tpi_d.get("coef_int_candidate", coef_int)
                    _duty_cand = compute_tpi(
                        target, current_temp, weather_data.get("temperature"),
                        _cand_ci, coef_ext)
                    _tpi_d["duty_previous"] = (round(self._tpi_duty_previous, 1)
                                               if self._tpi_duty_previous is not None else None)
                    _tpi_d["duty_candidate"] = round(_duty_cand, 1)
                    _tpi_d["duty_used"] = round(duty_cycle, 1)
                self._tpi_duty_previous = duty_cycle if current_temp is not None else (
                    self._tpi_duty_previous)

            # Display fallback: when no active heating target exists (summer, window-open,
            # effective_target=None), populate trv_setpoint for sensor display so
            # the sensor does not show Unknown after restart. Does not affect control logic,
            # TPI, learning, or boost tracking.
            if "trv_setpoint" not in recommendation:
                if recommendation.get("is_summer"):
                    # In summer mode the frost-protection value is always sent to the TRV.
                    # Use it directly so the sensor reflects the actually applied setpoint
                    # rather than a stale heating value from _last_written_setpoints.
                    recommendation["trv_setpoint"] = TEMP_FROST_PROTECTION
                else:
                    for _eid in cfg.get("climate_entities", []):
                        _last = self._last_written_setpoints.get(_eid)
                        if _last is not None:
                            recommendation["trv_setpoint"] = _last
                            break
                    if "trv_setpoint" not in recommendation:
                        for _eid in cfg.get("climate_entities", []):
                            _st = self.hass.states.get(_eid)
                            if _st is not None:
                                _t = to_internal_temperature_c(
                                    self.hass, _st.attributes.get("temperature"))
                                if _t is not None and _t > 0:
                                    recommendation["trv_setpoint"] = _t
                                    break

            # Nach trv_setpoint-Berechnung: Heizungsausfall-Erkennung
            self._check_heating_failure(recommendation)

            await self._async_observe_trv_setpoints(cfg, recommendation, weather_data)

            # LE v1 outcome-scoring/observation removed (frozen no-op —
            # LearningEngine.freeze() in __init__.py). LE v1 now only serves
            # the schedule lookup via async_get_base_target().
            indoor_humidity = self._read_avg_sensor(cfg.get("humidity_sensors", []))

            # Learning Support Critical Events: record window/summer/manual hold
            # transitions only (never every cycle) — pure event production,
            # no control effect. See _maybe_record_support_hold_events().
            self._maybe_record_support_hold_events(recommendation)

            # External Temperature Input immer schreiben (auch im Beobachtungsmodus)
            # – verbessert die TRV-interne Regelung unabhängig von der aktiven Steuerung
            await self._async_write_external_temp(cfg, recommendation)

            # ── Learning Adaptive Boost Control ────────────────────────────
            # resolve_adaptive_boost_control() is the single authority for real boost.
            # Gate order: learning_mode_on → active_control_on
            #   → mixed_zone_check → device_unavailable_check → readiness → inner gates.
            # Runs BEFORE dispatch so the approved offset flows through device guards and
            # the single setpoint dispatch path.  Never raises into heating.
            _boost_result = None  # AdaptiveBoostControlResult | None; None = not evaluated this cycle
            if (
                self._learning_shadow is not None
                and not effective_summer
                and recommendation.get("tpi_control_entities")
                and cfg.get(CONF_ZONE_CONTROL_MODE, CONTROL_MODE_TPI_BOOST)
                != CONTROL_MODE_SETPOINT
            ):
                # Block adaptive boost only when a device that consumes setpoint
                # modulation is unavailable; direct-valve devices are independent.
                _setpoint_device_unavailable = False
                for _eid in recommendation.get("tpi_control_entities", []):
                    _dev_st = self.hass.states.get(_eid)
                    if _dev_st is None or _dev_st.state in ("unavailable", "unknown"):
                        _setpoint_device_unavailable = True
                        break
                recommendation["_setpoint_device_unavailable"] = _setpoint_device_unavailable
                _restore_pending = not self._active_control_initialized
                try:
                    _boost_result = self._learning_shadow.resolve_adaptive_boost_control(
                        recommendation,
                        learning_mode_on=_learning_mode_on,
                        active_control_on=self._active_control,
                        restore_pending=_restore_pending,
                        boost_runtime_limit=TPI_MAX_BOOST_CELSIUS,
                    )
                except Exception:
                    pass
                # B2b-3c: authoritative three-state boost emission (runs every cycle);
                # 0.0 (evaluated, no boost) is distinct from None (not evaluated).
                try:
                    self._learning_shadow.emit_boost_authority_status_safe(recommendation)
                except Exception:
                    pass
                # boost_offset_c: raw Learning prediction (historical semantics, pre-guard).
                # boost_factor: backward-compat multiplier derived from the same prediction.
                # Neither is updated post-dispatch; applied_boost_offset_c carries dispatch truth.
                _candidate_c = float(recommendation.get("_boost_candidate_c") or 0.0)
                recommendation["boost_offset_c"] = round(_candidate_c, 3)
                try:
                    from custom_components.thermosmart.learning.models.boost import (
                        boost_offset_c_to_compat_factor)
                    recommendation["boost_factor"] = boost_offset_c_to_compat_factor(
                        _candidate_c, TPI_MAX_BOOST_CELSIUS)
                except Exception:
                    recommendation["boost_factor"] = 1.0

            # Learning Support Critical Events: observe the boost decision already
            # made above (_boost_result, recommendation["learning_boost_adjusted"])
            # — pure observation, no influence on boost timing, offset,
            # cooldown, or lifecycle. See _maybe_record_boost_event().
            self._maybe_record_boost_event(recommendation, _boost_result, effective_summer)

            # ── Authoritative Live Decision Record (Phase B1b) ────────────────────
            # Baseline record is always built from coordinator-owned data, regardless of
            # whether Learning is attached.  The shadow optionally enriches with decision_id
            # and onset_delay from Learning predictions.  Neither build nor enrichment may
            # raise into the heating cycle.
            _cycle_ts = self._clock.now_utc().isoformat()
            _mode_str = "control" if self._active_control else "shadow"
            _live_dec_pre = self._build_baseline_live_decision(
                recommendation, ts=_cycle_ts, mode_str=_mode_str)
            if self._learning_shadow is not None and _live_dec_pre is not None:
                try:
                    _enriched = self._learning_shadow.enrich_live_decision_pre_dispatch(
                        _live_dec_pre, recommendation)
                    if _enriched is not None:
                        _live_dec_pre = _enriched
                except Exception:
                    pass  # baseline record remains authoritative

            # ── Dispatch ──────────────────────────────────────────────────────────
            # Actor/service failures are captured in _DispatchStats and do NOT
            # propagate as UpdateFailed — other control/diagnostic data must stay
            # available even when individual TRV service calls fail.
            duty = 0.0
            _sp_stats = _DispatchStats()   # setpoint/frost dispatch stats
            _vv_stats = _DispatchStats()   # direct valve dispatch stats
            if self._active_control and not effective_summer:
                self._state_settings_enforced = set()
                for _eid in cfg.get("climate_entities", []):
                    await self._async_enforce_device_state_settings(_eid, cfg)
                await self._watchdog_hvac(cfg, recommendation)
                await self._async_calibrate_trvs(cfg, recommendation)
                _sp_stats = await self._apply_temperature(cfg, recommendation)
                _dispatch_failure_crossed = self._update_dispatch_failure_counters(_sp_stats)
                self._maybe_record_dispatch_failure_event(_dispatch_failure_crossed)
                # Direkte Ventilsteuerung nach Setpoint-Schreiben
                duty = recommendation.get("tpi_duty_cycle", 0.0)
                if recommendation.get("window_open"):
                    duty = 0.0
                _vv_stats = await self._async_set_valve_percent(cfg, duty)
            elif self._active_control and effective_summer:
                self._state_settings_enforced = set()
                for _eid in cfg.get("climate_entities", []):
                    await self._async_enforce_device_state_settings(_eid, cfg)
                await self._apply_frost_protection(cfg)
                recommendation["trv_setpoint"] = TEMP_FROST_PROTECTION

            # ── Post-dispatch dispatch truth (before LiveDecisionRecord) ─────────────
            # Compute dispatch status now so both the per-device applied truth and the
            # LiveDecisionRecord completion can use the same authoritative values.
            _disp_attempted = self._active_control and not effective_summer
            _combined = _sp_stats.merge(_vv_stats) if _disp_attempted else _DispatchStats()
            _disp_status = _combined.status if _disp_attempted else "not_attempted"
            _eff_sps = _sp_stats.effective_setpoints

            # Learning Support Critical Events: observe the TRV dispatch outcome
            # already decided above — pure observation, no influence on the
            # decision, the service-call payload, or any filter/throttle
            # logic. See _maybe_record_trv_dispatch_event().
            self._maybe_record_trv_dispatch_event(recommendation, _sp_stats, effective_summer)

            # Per-device applied boost offset — updated BEFORE LiveDecisionRecord so
            # LDR.boost_applied_c reflects real dispatch truth, not the pre-dispatch
            # approved value.
            #
            # Counterfactual formula (per device):
            #   device_applied_i = effective_with_boost_i - effective_baseline_i
            # Both sides run through the same per-device transform path (min-clamp,
            # step-snap, re-clamp) — only the input differs (baseline vs baseline+boost).
            # This correctly accounts for device-specific priors (min_temp), step
            # quantization, and profile offsets without double service calls.
            #
            # Semantics:
            #   fully_succeeded  → min(device_applied_i) across written devices ≥ 0
            #                      (conservative zone truth; absorbs clamp/step reduction).
            #   partial/failed   → 0.0 public; per-device offsets preserved for provenance.
            #   not_attempted    → 0.0 (approved-but-not-dispatched must not appear as applied; lifecycle not touched).
            #
            # recommendation["_boost_per_device_applied_c"] (set in all three branches
            # below) is internal provenance, not part of the public/exported
            # diagnostics surface — no entity, export, or ha_integration.py code reads
            # it. Its purpose is to let the private boost test suite verify the
            # conservative-minimum derivation above (applied_boost_offset_c must never
            # exceed any individual device's own offset) without duplicating this
            # per-device counterfactual computation in test code.
            if self._learning_shadow is not None and bool(recommendation.get("learning_boost_adjusted")):
                _baseline_c = float(recommendation.get("tpi_baseline_setpoint") or 0.0)
                _eff_by_eid = _sp_stats.effective_setpoints_by_entity

                def _counterfactual_offsets(eff_by_eid: dict) -> list:
                    """Compute per-device applied offset via counterfactual baseline resolution."""
                    offsets = []
                    for _eid, _eff_sp in eff_by_eid.items():
                        _st = self.hass.states.get(_eid)
                        if _st is None:
                            # State unavailable post-dispatch: fall back to zone baseline.
                            offsets.append(_eff_sp - _baseline_c)
                            continue
                        _prof = self._device_profiles.get(_eid)
                        _eff_bl = self.resolve_device_effective_setpoint(
                            _eid, _st, _baseline_c, _prof)
                        offsets.append(_eff_sp - _eff_bl)
                    return offsets

                if _disp_status == "fully_succeeded" and _eff_by_eid:
                    _dev_offsets = _counterfactual_offsets(_eff_by_eid)
                    _real_applied = max(0.0, min(_dev_offsets))
                    recommendation["_boost_applied_c"] = round(_real_applied, 4)
                    recommendation["_boost_per_device_applied_c"] = [
                        round(o, 4) for o in _dev_offsets]
                    recommendation["applied_boost_offset_c"] = round(_real_applied, 3)
                    # boost_offset_c (prediction) and boost_factor are not updated post-dispatch.
                elif _disp_status == "not_attempted":
                    # Dispatch path skipped entirely (summer mode / active-control exclusion):
                    # approved-but-not-dispatched must not surface as applied truth.
                    recommendation["_boost_applied_c"] = 0.0
                    recommendation["applied_boost_offset_c"] = 0.0
                    recommendation["_boost_per_device_applied_c"] = []
                else:
                    # Partial or failed dispatch: zero applied truth, preserve per-device data.
                    if _eff_by_eid and _disp_status == "partially_succeeded":
                        _dev_offsets = _counterfactual_offsets(_eff_by_eid)
                        recommendation["_boost_per_device_applied_c"] = [
                            round(o, 4) for o in _dev_offsets]
                    recommendation["_boost_applied_c"] = 0.0
                    recommendation["applied_boost_offset_c"] = 0.0
                    # Hard-release lifecycle so next cycle re-evaluates from baseline.
                    try:
                        self._learning_shadow.invalidate_boost_after_failed_dispatch_safe(
                            _disp_status)
                    except Exception:
                        pass
                    # Learning Support Critical Events: observe the invalidation
                    # just performed above — pure observation, no influence
                    # on the lifecycle release itself.
                    self._maybe_record_boost_invalidated_event()

            # ── Complete LiveDecisionRecord with dispatch truth ────────────────────
            if _live_dec_pre is not None:
                _disp_path = (
                    ("direct_valve" if recommendation.get("tpi_valve_direct") else "setpoint")
                    if _disp_attempted else "none"
                )
                _outcome_elig = _disp_status == "fully_succeeded"
                _outcome_rel = (
                    "full" if _disp_status == "fully_succeeded" else
                    "partial" if _disp_status == "partially_succeeded" else
                    "none"
                )
                _eff_min = float(min(_eff_sps)) if _eff_sps else None
                _eff_max = float(max(_eff_sps)) if _eff_sps else None
                try:
                    self._last_live_decision = dataclasses.replace(
                        _live_dec_pre,
                        dispatch_attempted=_disp_attempted,
                        dispatch_path=_disp_path,
                        dispatch_setpoint_c=(
                            float(recommendation["trv_setpoint"])
                            if recommendation.get("trv_setpoint") is not None else None),
                        dispatch_effective_setpoint_min_c=_eff_min,
                        dispatch_effective_setpoint_max_c=_eff_max,
                        dispatch_effective_setpoints=tuple(float(x) for x in _eff_sps),
                        dispatch_duty_pct=(float(duty) if _disp_path == "direct_valve" else None),
                        dispatch_succeeded=_outcome_elig,
                        dispatch_status=_disp_status,
                        dispatch_targets_total=_combined.targets_total,
                        dispatch_targets_succeeded=_combined.targets_succeeded,
                        dispatch_targets_failed=_combined.targets_failed,
                        dispatch_failure_reasons=tuple(_combined.failure_reasons),
                        dispatch_ts=_cycle_ts if _disp_attempted else None,
                        outcome_eligible=_outcome_elig,
                        outcome_reliability=_outcome_rel,
                    )
                except Exception:
                    self._last_live_decision = None
            else:
                self._last_live_decision = None

            # ── Derive trace from live record ─────────────────────────────────────
            # Production coordinator always passes live_record; legacy resolver path
            # is never triggered.  When no live record, clear shadow's stale trace.
            if self._learning_shadow is not None:
                if self._last_live_decision is not None:
                    try:
                        self._learning_shadow.compute_decision_trace_safe(
                            recommendation,
                            active_control=self._active_control,
                            live_record=self._last_live_decision)
                    except Exception:
                        pass
                else:
                    self._learning_shadow.clear_last_trace()

            # Valve maintenance runs only in summer/vacation mode AND only
            # while Active Control is on — the function self-guards via
            # active_control plus valve_likely_idle = is_summer or in_vacation.
            # Called outside the active-control dispatch blocks above since
            # its own internal gate (not the outer branching) decides eligibility.
            await self._async_valve_maintenance(cfg, recommendation)

            recommendation["heating_failure"] = self._heating_failure_notified
            recommendation["indoor_humidity"] = indoor_humidity

            # Display cache: raw sensor average so climate.current_temperature
            # reflects the actual reading, not the EMA-smoothed control value.
            # Fallback: use TRV current_temperature when no external sensor.
            raw_temp = self._read_raw_avg_sensor(cfg.get("temp_sensors", []))
            if raw_temp is None:
                raw_temp = self._read_trv_avg_temp(cfg.get("climate_entities", []))
            if raw_temp is not None:
                self._live_temp = raw_temp
            self._cycle_ref_temp = raw_temp
            if indoor_humidity is not None:
                self._live_humidity = indoor_humidity

            _ct = recommendation.get("current_temp")
            _et = recommendation.get("effective_target")
            _dt = recommendation.get("desired_target")
            _ts = recommendation.get("trv_setpoint")
            _LOGGER.debug(
                "ThermoSmart '%s': cycle summary current=%s°C"
                " desired=%s°C driven=%s°C trv=%s°C duty=%s%% preheat=%dmin"
                " window=%s summer=%s vacation=%s active=%s",
                self.zone_name,
                f"{_ct:.1f}" if _ct is not None else "n/a",
                f"{_dt:.1f}" if _dt is not None else "n/a",
                f"{_et:.1f}" if _et is not None else "n/a",
                f"{_ts:.1f}" if _ts is not None else "n/a",
                f"{recommendation.get('tpi_duty_cycle', 0.0):.0f}",
                recommendation.get("preheat_minutes", 0),
                recommendation.get("window_open", False),
                recommendation.get("is_summer", False),
                self._vacation_mode,
                self._active_control,
            )

            # ── Learning: collect hvac_action for regime classifier ───────
            # Needed by both shadow and standalone ha_integration paths so that
            # the Learning regime classifier can confirm ACTIVE_HEATING without relying
            # solely on temperature slope.  "heating" wins if ANY TRV reports it.
            if _learning_mode_on:
                _zone_hvac_action = None
                for _eid in cfg.get("climate_entities", []):
                    if not _eid:
                        continue
                    _st = self.hass.states.get(_eid)
                    if _st is not None and _zone_hvac_action != "heating":
                        _ha = _st.attributes.get("hvac_action")
                        if _ha == "heating":
                            _zone_hvac_action = "heating"
                        elif _ha is not None and _zone_hvac_action is None:
                            _zone_hvac_action = _ha
                recommendation["_hvac_action"] = _zone_hvac_action

            # ── Learning passive shadow observation ───────────────────────
            # Runs AFTER the control decision is fully determined and applied.
            # Only when Learning Mode is ON — Learning OFF → no shadow observation.
            # Purely passive (diagnostics only); doubly guarded so a learning
            # failure can never become a heating failure or an UpdateFailed.
            if _learning_mode_on and self._learning_shadow is not None:
                try:
                    # No schedule-derived comfort time yet (scheduling arrives with WP2).
                    _sched_time = _sched_temp = None
                    # B2b-4e: live per-device availability for the post-reach boost observer
                    # (binding/entity -> available|unavailable|unknown). No entity ids leave
                    # the learning runtime; they are hashed into anonymous slots internally.
                    _dev_states: dict[str, str] = {}
                    for _eid in cfg.get("climate_entities", []):
                        if not _eid:
                            continue
                        _st = self.hass.states.get(_eid)
                        if _st is None or _st.state == "unavailable":
                            _dev_states[_eid] = "unavailable"
                        elif _st.state == "unknown":
                            _dev_states[_eid] = "unknown"
                        else:
                            _dev_states[_eid] = "available"
                    recommendation["device_states"] = _dev_states
                    self._learning_shadow.observe_safe(
                        recommendation, weather=weather_data,
                        schedule_comfort_time_utc=_sched_time,
                        schedule_comfort_temperature_c=_sched_temp,
                        heating_failure=self._heating_failure_since is not None,
                        live_record=self._last_live_decision)
                except Exception:  # never let Learning affect the heating path
                    pass

            # ── Learning periodic persistence ─────────────────────────────
            # Reachable every coordinator cycle regardless of the learning-mode
            # toggle above, so state left dirty from an earlier cycle still gets
            # flushed even if learning is disabled mid-session. async_save_if_due()
            # is internally debounced (30s, hourly safety net via
            # PersistenceOrchestrator) — a no-op on most cycles, never a second
            # parallel save mechanism. Awaited (not fire-and-forget); HA never runs
            # two _async_update_data cycles for the same coordinator concurrently,
            # so there is no save race. A save failure must never affect heating.
            if self._learning_shadow is not None:
                try:
                    await self._learning_shadow.async_save_if_due()
                except Exception:
                    pass

            return {
                "weather": weather_data,
                "zone": recommendation,
                "active_control": self._active_control,
            }
        except Exception as err:
            raise UpdateFailed(f"ThermoSmart '{self.zone_name}': {err}") from err

    def _record_support_hold_event(self, event_type, severity, *, reason: str,
                                    summary: str, details: dict) -> None:
        """Build and hand off ONE Support Critical Event for a hold/release
        transition. Memory-only: record_support_critical_event_safe() never
        performs storage I/O, never awaits, never triggers a save directly —
        persistence happens later via the existing periodic save cycle.
        Never raises; a missing shadow or any internal failure is silently
        skipped (event production must never affect the control cycle).
        """
        if self._learning_shadow is None:
            return
        try:
            from .learning.support_event_schemas import SupportCriticalEvent
            from .learning.storage.support_event_serialization import SUPPORT_EVENT_SCHEMA_VERSION
            now = self._clock.now_utc()
            event_id = f"hold_{event_type.value}_{int(now.timestamp() * 1000)}"
            event = SupportCriticalEvent(
                schema_version=SUPPORT_EVENT_SCHEMA_VERSION, event_id=event_id,
                event_type=event_type, ts=now, severity=severity,
                reason=reason, summary=summary, details=details,
            )
            self._learning_shadow.record_support_critical_event_safe(event)
        except Exception:
            pass  # event production is best-effort; must never affect the control cycle

    def _maybe_record_support_hold_events(self, recommendation: dict) -> None:
        """Record Support Critical Events for window/summer/manual hold
        TRANSITIONS only — never every cycle. Compares this cycle's
        window_open/is_summer flags and the one-shot manual_change marker (already computed in
        ``recommendation``) against the previous cycle's own dedupe state;
        emits a landmark only when a flag actually flips. Pure event
        production: reads recommendation, never mutates it, never touches
        control state (setpoints, TPI, boost, preheat, TRV commands). Never
        raises — see _record_support_hold_event()'s own guard.
        """
        if self._learning_shadow is None:
            return
        try:
            from .learning.support_event_schemas import SupportEventSeverity, SupportEventType

            window_open = bool(recommendation.get("window_open", False))
            if window_open and not self._support_event_window_hold_active:
                self._record_support_hold_event(
                    SupportEventType.WINDOW_OPEN_HOLD, SupportEventSeverity.INFO,
                    reason="window_open",
                    summary="Heating held due to an open window",
                    details={"source": "window", "heating_allowed": False},
                )
            elif not window_open and self._support_event_window_hold_active:
                self._record_support_hold_event(
                    SupportEventType.WINDOW_CLOSED_RELEASE, SupportEventSeverity.INFO,
                    reason="window_closed",
                    summary="Window hold released",
                    details={"source": "window", "heating_allowed": True},
                )
            self._support_event_window_hold_active = window_open

            # No dedicated "summer mode release" event type exists in the
            # schema (unlike the window open/closed pair) — reusing
            # SUMMER_MODE_HOLD for both start and end, disambiguated by
            # reason + current_active, was preferred over adding a new event
            # type for a single asymmetric case (see report).
            summer_active = bool(recommendation.get("is_summer", False))
            if summer_active and not self._support_event_summer_hold_active:
                self._record_support_hold_event(
                    SupportEventType.SUMMER_MODE_HOLD, SupportEventSeverity.INFO,
                    reason="summer_mode_active",
                    summary="Heating held due to summer mode",
                    details={"mode": "summer", "previous_active": False, "current_active": True},
                )
            elif not summer_active and self._support_event_summer_hold_active:
                self._record_support_hold_event(
                    SupportEventType.SUMMER_MODE_HOLD, SupportEventSeverity.INFO,
                    reason="summer_mode_ended",
                    summary="Summer mode hold ended",
                    details={"mode": "summer", "previous_active": True, "current_active": False},
                )
            self._support_event_summer_hold_active = summer_active

            if recommendation.get("manual_change"):
                self._record_support_hold_event(
                    SupportEventType.MANUAL_OVERRIDE_START, SupportEventSeverity.INFO,
                    reason="manual_target_change",
                    summary="Manual target change",
                    details={"source": "manual_target_change"},
                )
        except Exception:
            pass  # event production is best-effort; must never affect the control cycle

    def _maybe_record_recommendation_only_event(self) -> None:
        """Record ONE learning_recommendation_only landmark on the transition
        into observation-only mode (active_control off) — not every cycle.
        Mirrors the hold-transition dedupe pattern from Increment 2.
        """
        if self._support_event_recommendation_only_active:
            return
        self._support_event_recommendation_only_active = True
        from .learning.support_event_schemas import SupportEventSeverity, SupportEventType
        self._record_support_hold_event(
            SupportEventType.LEARNING_RECOMMENDATION_ONLY, SupportEventSeverity.INFO,
            reason="learning_recommendation_only",
            summary="Zone is in observation/recommendation mode (active control off)",
            details={"active_control": False},
        )

    def _maybe_record_trv_dispatch_event(
        self, recommendation: dict, sp_stats: _DispatchStats, effective_summer: bool,
    ) -> None:
        """Record ONE Support Critical Event observing the TRV setpoint
        dispatch OUTCOME already decided this cycle — pure observation, never
        influences the dispatch decision, the service-call payload, or any
        filter/throttle/setpoint logic. ``sp_stats`` is read-only here (the
        same ``_DispatchStats`` already computed by ``_apply_temperature()``,
        privacy-safe by construction — see trv_control.py).

        Deduped via a small in-memory "last signature" cache
        (event_type + reason + rounded requested setpoint) so an unchanged
        situation (e.g. a zone sitting at target for hours) records at most
        one event per genuinely new situation, never one per cycle — same
        intent as the foundation's time-window dedupe helper, but a cheaper same-cycle-chain
        check since this fires far more often (every control cycle) than the
        storage/setup landmarks that helper was built for.

        Only maps to a specific block reason when the EXISTING logic already
        makes that reason determinable at this zone level (same-setpoint
        match against ``_last_written_setpoints``, TRV unavailability via
        ``_trv_offline`` — both already-tracked coordinator state, read-only
        here). Falls back to the generic ``trv_command_blocked`` rather than
        guessing a more specific reason the current code cannot actually
        distinguish (e.g. "min interval" — no such gate exists in this
        codebase; not fabricated here, see the accompanying report).
        """
        if self._learning_shadow is None:
            return
        try:
            if not self._active_control:
                self._maybe_record_recommendation_only_event()
                return
            self._support_event_recommendation_only_active = False
            if effective_summer:
                return  # covered by the existing summer_mode_hold event (Increment 2)

            from .learning.support_event_schemas import SupportEventSeverity, SupportEventType

            requested = recommendation.get("trv_setpoint", recommendation.get("effective_target"))
            requested_rounded = round(float(requested), 1) if isinstance(requested, (int, float)) else None

            if sp_stats.effective_setpoints:
                event_type = SupportEventType.TRV_COMMAND_SENT
                severity = SupportEventSeverity.INFO
                reason = "trv_command_sent"
                summary = "TRV setpoint command sent"
                details: dict = {}
                if requested_rounded is not None:
                    details["requested_setpoint"] = requested_rounded
                if self._support_event_last_sent_setpoint is not None:
                    details["previous_setpoint"] = self._support_event_last_sent_setpoint
            elif self._trv_offline:
                event_type = SupportEventType.TRV_UNAVAILABLE
                severity = SupportEventSeverity.WARNING
                reason = "trv_unavailable"
                summary = "TRV setpoint command blocked: TRV unavailable"
                details = {"active_control": True}
            elif requested_rounded is not None and any(
                abs(v - requested_rounded) < 0.5 for v in self._last_written_setpoints.values()
            ):
                event_type = SupportEventType.SAME_SETPOINT_BLOCK
                severity = SupportEventSeverity.INFO
                reason = "same_setpoint_block"
                summary = "TRV setpoint command blocked: already at target"
                details = {"requested_setpoint": requested_rounded}
            elif sp_stats.status == "not_attempted":
                event_type = SupportEventType.TRV_COMMAND_BLOCKED
                severity = SupportEventSeverity.INFO
                reason = "not_attempted"
                summary = "TRV setpoint command not attempted"
                details = {"active_control": True}
            else:
                return  # failed/partially-succeeded dispatch: out of scope for this step

            signature = (event_type.value, reason, requested_rounded)
            if signature == self._support_event_last_trv_signature:
                return
            self._support_event_last_trv_signature = signature
            if event_type is SupportEventType.TRV_COMMAND_SENT and requested_rounded is not None:
                self._support_event_last_sent_setpoint = requested_rounded

            self._record_support_hold_event(
                event_type, severity, reason=reason, summary=summary, details=details,
            )
        except Exception:
            pass  # event production is best-effort; must never affect the control cycle

    def _update_dispatch_failure_counters(self, stats: _DispatchStats) -> bool:
        """Update per-entity consecutive setpoint-dispatch-failure counters
        from this cycle's ``_apply_temperature()`` stats (Device Compatibility
        P1 — ``_maybe_record_trv_dispatch_event()`` above deliberately left
        failed/partially-succeeded dispatch "out of scope"; this covers that
        gap without touching that method's existing behavior).

        A successful dispatch always resets that entity's counter to 0 —
        processed after failures so success wins even in the rare same-cycle
        edge case where an entity appears in both (e.g. two dispatch paths
        writing the same climate entity in one cycle).

        Returns True the cycle any entity's counter reaches exactly
        ``DISPATCH_FAILURE_SUPPORT_EVENT_THRESHOLD`` — a one-time crossing
        signal, not "still above threshold", so a persistent failure streak
        fires the caller's event once, not once per cycle.
        """
        crossed = False
        for eid in stats.failed_entity_ids:
            n = self._dispatch_consecutive_failures.get(eid, 0) + 1
            self._dispatch_consecutive_failures[eid] = n
            if n == DISPATCH_FAILURE_SUPPORT_EVENT_THRESHOLD:
                crossed = True
        for eid in stats.effective_setpoints_by_entity:
            self._dispatch_consecutive_failures[eid] = 0
        return crossed

    def _maybe_record_dispatch_failure_event(self, crossed: bool) -> None:
        """Fire ONE Support Critical Event the cycle a TRV's consecutive
        setpoint-dispatch failures first reach
        ``DISPATCH_FAILURE_SUPPORT_EVENT_THRESHOLD`` (see
        ``_update_dispatch_failure_counters()``). No entity id in the event
        (schema forbids it, see support_event_schemas.py) — this is a
        zone-level "a TRV command is persistently failing" signal, not a
        per-device diagnosis. Fires again only after that entity recovers
        (a success resets its counter to 0) and then fails
        ``DISPATCH_FAILURE_SUPPORT_EVENT_THRESHOLD`` times again — never once
        per cycle while a streak continues, never a command flood.
        """
        if not crossed or self._learning_shadow is None:
            return
        try:
            from .learning.support_event_schemas import SupportEventSeverity, SupportEventType
            self._record_support_hold_event(
                SupportEventType.TRV_COMMAND_FAILED, SupportEventSeverity.WARNING,
                reason="repeated_dispatch_failure",
                summary="TRV setpoint command failed repeatedly",
                details={"consecutive_failures": DISPATCH_FAILURE_SUPPORT_EVENT_THRESHOLD},
            )
        except Exception:
            pass  # event production is best-effort; must never affect the control cycle

    def _maybe_record_boost_event(
        self, recommendation: dict, boost_result, effective_summer: bool,
    ) -> None:
        """Record Support Critical Events observing the boost decision
        already made this cycle by resolve_adaptive_boost_control() — pure
        observation, never influences boost timing, offset, cooldown, or
        lifecycle. ``boost_result`` is the read-only
        ``AdaptiveBoostControlResult`` already returned by that resolver
        (None when it was not invoked this cycle — summer mode or no Learning
        shadow attached).

        started/ended follow the SAME boolean-transition-flag pattern as the
        window/summer/manual hold events (Increment 2): an event fires only
        when ``_support_event_boost_active`` genuinely flips. blocked uses a
        small (event_type, reason) signature cache instead, since a zone can
        stay blocked for the SAME reason across many cycles (e.g. a
        persistent cooldown) — that must record once, not once per cycle;
        but a genuinely different reason must still be visible.

        ``reason`` values come directly from ``boost_result.blocking_reason``
        (the resolver's own typed BoostBlockReason / inner-gate rejection
        string, e.g. "active_control_off", "window_open", "cooldown_active",
        "device_unavailable") — never guessed or invented.
        """
        if self._learning_shadow is None:
            return
        try:
            from .learning.support_event_schemas import SupportEventSeverity, SupportEventType

            if boost_result is None:
                # Resolver not invoked this cycle (summer mode, or shadow gone).
                if self._support_event_boost_active:
                    self._support_event_boost_active = False
                    reason = "summer_mode" if effective_summer else "shadow_unavailable"
                    self._record_support_hold_event(
                        SupportEventType.BOOST_ENDED, SupportEventSeverity.INFO,
                        reason=reason, summary="Boost ended", details={"active": False},
                    )
                return

            boost_active_now = bool(recommendation.get("learning_boost_adjusted", False))

            if boost_active_now and not self._support_event_boost_active:
                details: dict = {"active": True}
                try:
                    details["offset"] = round(float(boost_result.approved_boost_offset_c), 3)
                except (TypeError, ValueError):
                    pass
                self._record_support_hold_event(
                    SupportEventType.BOOST_STARTED, SupportEventSeverity.INFO,
                    reason="boost_started", summary="Boost started", details=details,
                )
            elif not boost_active_now and self._support_event_boost_active:
                reason = str(boost_result.blocking_reason or "unknown")
                self._record_support_hold_event(
                    SupportEventType.BOOST_ENDED, SupportEventSeverity.INFO,
                    reason=reason, summary="Boost ended", details={"active": False},
                )
            elif not boost_active_now and boost_result.blocking_reason:
                reason = str(boost_result.blocking_reason)
                signature = ("boost_blocked", reason)
                if signature != self._support_event_last_boost_signature:
                    self._support_event_last_boost_signature = signature
                    self._record_support_hold_event(
                        SupportEventType.BOOST_BLOCKED, SupportEventSeverity.INFO,
                        reason=reason, summary="Boost blocked", details={"active": False},
                    )

            self._support_event_boost_active = boost_active_now
        except Exception:
            pass  # event production is best-effort; must never affect the control cycle

    def _maybe_record_boost_invalidated_event(self) -> None:
        """Record ONE boost_ended landmark when boost was hard-released this
        cycle after a failed/partial TRV dispatch — pure observation, called
        right after the EXISTING invalidate_boost_after_failed_dispatch_safe()
        lifecycle call, never influencing it. A no-op if boost was not
        already recorded as active this cycle (avoids a spurious "ended"
        when there was nothing to end from an event-production perspective).
        """
        if self._learning_shadow is None or not self._support_event_boost_active:
            return
        try:
            from .learning.support_event_schemas import SupportEventSeverity, SupportEventType
            self._support_event_boost_active = False
            self._record_support_hold_event(
                SupportEventType.BOOST_ENDED, SupportEventSeverity.INFO,
                reason="invalidated_after_failed_dispatch",
                summary="Boost invalidated after failed dispatch",
                details={"active": False},
            )
        except Exception:
            pass  # event production is best-effort; must never affect the control cycle

    def _maybe_record_room_sensor_events(
        self, cfg: dict, primary_room_temp: float | None, resolved_current_temp: float | None,
    ) -> None:
        """Record Support Critical Events observing the room-temperature
        sensor / TRV-temperature fallback outcome already decided this cycle
        by the EXISTING read order (``_read_avg_sensor()`` then, only if
        that returned None, ``_read_trv_avg_temp()``) — pure observation,
        never influences sensor selection or the fallback itself.

        ``primary_room_temp`` is the room sensor's OWN reading (None when
        unavailable/unknown/non-numeric for every configured sensor —
        ``_read_avg_sensor()`` does not distinguish those sub-cases, so
        neither does this method; a ``temperature_invalid`` event is
        deliberately NOT produced in this step since no such distinct state
        exists anywhere in the current code — see the accompanying report).
        ``resolved_current_temp`` is the value actually used this cycle
        (room sensor value, or the TRV fallback, or still None if both
        failed).

        A zone with no ``temp_sensors`` configured at all is not "sensor
        unavailable" — it deliberately relies on the TRV fallback by design
        (see ``_read_trv_avg_temp()``'s own docstring) — no event is
        produced for that case, only for a genuine unavailable/restored
        TRANSITION on an actually-configured room sensor.

        sensor_unavailable/sensor_restored follow the SAME boolean-
        transition-flag pattern as the other hold/boost events. fallback_used
        uses a small (event_type, fallback_type, reason) signature cache so
        a room sensor staying unavailable for many cycles records the
        fallback once per episode, not once per cycle; the cache resets when
        the room sensor is available again, so a LATER new unavailable
        episode is still visible.
        """
        if self._learning_shadow is None:
            return
        try:
            if not cfg.get("temp_sensors"):
                return  # role not in use for this zone by design — nothing to observe

            from .learning.support_event_schemas import SupportEventSeverity, SupportEventType

            primary_available = primary_room_temp is not None

            if not primary_available and self._support_event_room_sensor_available:
                self._support_event_room_sensor_available = False
                self._record_support_hold_event(
                    SupportEventType.SENSOR_UNAVAILABLE, SupportEventSeverity.WARNING,
                    reason="room_sensor_unavailable",
                    summary="Room temperature sensor unavailable",
                    details={"sensor_role": "room_temperature", "state": "unavailable"},
                )
            elif primary_available and not self._support_event_room_sensor_available:
                self._support_event_room_sensor_available = True
                self._support_event_last_fallback_signature = None
                self._record_support_hold_event(
                    SupportEventType.SENSOR_RESTORED, SupportEventSeverity.INFO,
                    reason="room_sensor_restored",
                    summary="Room temperature sensor restored",
                    details={"sensor_role": "room_temperature"},
                )

            if not primary_available and resolved_current_temp is not None:
                signature = ("fallback_used", "trv_temperature", "room_sensor_unavailable")
                if signature != self._support_event_last_fallback_signature:
                    self._support_event_last_fallback_signature = signature
                    self._record_support_hold_event(
                        SupportEventType.FALLBACK_USED, SupportEventSeverity.INFO,
                        reason="room_sensor_unavailable",
                        summary="Using TRV temperature as fallback",
                        details={"fallback_type": "trv_temperature", "reason": "room_sensor_unavailable"},
                    )
        except Exception:
            pass  # event production is best-effort; must never affect the control cycle

    # ── Berechnung ───────────────────────────────────────────────────

    async def _compute_recommendation(self, cfg: dict, weather_data: dict) -> dict:
        _learning_mode_on = bool(cfg.get("learning_enabled", True))
        _primary_room_temp = self._read_avg_sensor(cfg.get("temp_sensors", []), is_temperature=True)
        current_temp = _primary_room_temp
        if current_temp is None:
            current_temp = self._read_trv_avg_temp(cfg.get("climate_entities", []))
        # Learning Support Critical Events: observe the room-sensor/fallback
        # outcome already decided above — pure observation, no influence on
        # sensor selection or the fallback itself. See
        # _maybe_record_room_sensor_events().
        self._maybe_record_room_sensor_events(cfg, _primary_room_temp, current_temp)

        now = self._now_local()
        self._slope_sample_fresh = False
        if current_temp is not None:
            # The slope is sampled over >= 1 min only, so extra (event-triggered)
            # cycles in between neither shorten the baseline nor re-count a stale slope.
            if self._indoor_temp_prev is None:
                self._indoor_temp_prev = (now, current_temp)
            else:
                prev_time, prev_temp = self._indoor_temp_prev
                elapsed_min = (now - prev_time).total_seconds() / 60
                if elapsed_min >= 1.0:
                    if elapsed_min <= 30.0:
                        self._indoor_temp_slope = round((current_temp - prev_temp) / elapsed_min, 4)
                        self._slope_sample_fresh = True
                    self._indoor_temp_prev = (now, current_temp)
            if self._ema_1h is None:
                self._ema_1h = current_temp
            else:
                self._ema_1h = round(
                    EMA_1H_ALPHA * current_temp + (1 - EMA_1H_ALPHA) * self._ema_1h, 2
                )

        window_open = self._check_window_open(cfg, current_temp=current_temp)

        desired_target = self.target.value
        manual_change = self._manual_change_pending
        self._manual_change_pending = False

        # ── Preheat duration estimate: Learning is the sole adaptive source ────
        # LE v1 is NEVER read for preheat; the frozen LE v1 store is historical.
        # When Learning has no evidence, or Learning Mode is OFF, the deterministic
        # baseline is used. Learning OFF → no adaptive preheat, no onset delay.
        # Onset delay is separate from HeatRate learning (never folded in).
        from .learning.runtime.ha_integration import LearningShadowController
        if _learning_mode_on and self._learning_shadow is not None:
            preheat_minutes, preheat_status = self._learning_shadow.read_preheat_minutes_safe(
                current_temp, desired_target,
                outdoor_temp=weather_data.get("temperature") if weather_data else None,
            )
            _onset_delay_min, _onset_delay_status = self._learning_shadow.read_onset_delay_safe()
        else:
            # No shadow: deterministic baseline (1.5 °C/h prior; onset prior 5 min).
            preheat_minutes, preheat_status = (
                LearningShadowController.compute_deterministic_preheat_baseline(
                    current_temp, desired_target
                )
            )
            _onset_delay_min = LearningShadowController._ONSET_DELAY_PRIOR_MIN
            _onset_delay_status = "cold_start_prior"

        # ── Driven target ──────────────────────────────────────────────────────
        # The desired target is never altered here. What the TRVs are driven to
        # is derived from it by the suspension that applies (summer > vacation >
        # window): summer/vacation hold a fixed temperature, an open window
        # releases the setpoint (None), otherwise it is the desired target.
        suspension = resolve(
            summer=self._is_summer, vacation=self._vacation_mode, window_open=window_open,
        )
        held = held_at(suspension, float(cfg.get(CONF_VACATION_TEMP, TEMP_FROST_PROTECTION)))
        if suspension is None:
            effective_target = desired_target
        else:
            effective_target = held  # None for a window: release the setpoint

        # Learning progress from real per-zone model updates (display value, 0.0–1.0).
        # Uses learning_progress_safe() so the value matches ThermoSmartConfidenceSensor.
        # Guarded: a learning failure must never become a heating failure -> neutral 0.0.
        learning_confidence = 0.0
        if self._learning_shadow is not None:
            try:
                _prog_pct, _ = self._learning_shadow.learning_progress_safe()
                learning_confidence = _prog_pct / 100.0
            except Exception:
                learning_confidence = 0.0

        return {
            "zone_name": self.zone_name,
            "current_temp": current_temp,
            "window_open": window_open,
            "desired_target": desired_target,
            "desired_target_source": self.target.current.source,
            "preset": self.target.preset,
            "manual_change": manual_change,
            "effective_target": effective_target,  # what the TRVs are driven to (None: window release)
            "suspension": suspension,
            "held_at": held,
            "preheat_minutes": preheat_minutes,
            "preheat_active": False,
            "preheat_status": preheat_status,
            # Onset delay and baseline for DecisionTrace (computed once above)
            "onset_delay_min": _onset_delay_min,
            "onset_delay_status": _onset_delay_status,
            "preheat_baseline_minutes": LearningShadowController.compute_deterministic_preheat_baseline(
                current_temp, desired_target)[0],
            "temperature_gap_c": ((desired_target - current_temp)
                                  if current_temp is not None else None),
            "learning_confidence": learning_confidence,
            "outdoor_temp": weather_data.get("temperature"),
            "weather_condition": weather_data.get("condition"),
            "temp_slope": self._indoor_temp_slope,
            "temp_ema_1h": self._ema_1h,
        }

    # ── Sensor-Lesen ─────────────────────────────────────────────────

    def _read_raw_avg_sensor(self, sensor_ids: list[str]) -> float | None:
        """Raw average of available temperature sensors — no EMA smoothing, for display only.

        Filters out invalid states (unknown, unavailable, None) but does not
        apply EMA or spike detection. Used exclusively for _live_temp so that
        climate.current_temperature reflects the actual sensor reading instead
        of the EMA-smoothed value used by the control path. Each sensor's own
        unit_of_measurement is normalized to °C before averaging — a mix of
        °C and °F sensors is never averaged raw.
        """
        values = []
        seen: set[str] = set()
        for sid in sensor_ids:
            if not sid or sid in seen:
                continue
            seen.add(sid)
            state = self.hass.states.get(sid)
            if state and state.state not in ("unknown", "unavailable", "None"):
                c = to_internal_temperature_c(
                    self.hass, state.state, unit=state.attributes.get("unit_of_measurement"))
                if c is not None:
                    values.append(c)
        return round(sum(values) / len(values), 1) if values else None

    def _read_trv_avg_temp(self, climate_entity_ids: list[str]) -> float | None:
        """Average current_temperature from TRV entities — fallback when no external sensor.

        Reads the current_temperature attribute directly; no EMA, no spike filter.
        Used when temp_sensors is empty so TPI and the display cache have a valid value.
        External temp sensors always take priority — this method is only called as fallback.
        climate.current_temperature carries no unit attribute of its own — Home
        Assistant's climate platform already normalizes it to the HA system
        unit before it reaches state.attributes, so no explicit unit is passed
        here (to_internal_temperature_c falls back to the system unit).
        """
        values = []
        seen: set[str] = set()
        for eid in climate_entity_ids:
            if not eid or eid in seen:
                continue
            seen.add(eid)
            state = self.hass.states.get(eid)
            if state and state.state not in ("unknown", "unavailable"):
                trv_t = state.attributes.get("current_temperature")
                c = to_internal_temperature_c(self.hass, trv_t)
                if c is None:
                    continue
                if not is_plausible_temperature_c(c, context=eid):
                    _LOGGER.debug(
                        "ThermoSmart '%s': TRV-Fallback %s unplausibler Wert verworfen: %.1f°C",
                        self.zone_name, eid, c,
                    )
                    continue
                values.append(c)
        return round(sum(values) / len(values), 1) if values else None

    def _read_avg_sensor(self, sensor_ids: list[str], *, is_temperature: bool = False) -> float | None:
        """Durchschnitt über alle verfügbaren Sensoren – ignoriert Ausfälle automatisch.

        Used for both temperature and humidity sensors. ``is_temperature=True``
        normalizes each sensor's own unit_of_measurement to °C before EMA
        filtering — humidity (percent) is never unit-converted.
        """
        values = []
        seen: set[str] = set()
        for sid in sensor_ids:
            if not sid or sid in seen:
                continue
            seen.add(sid)
            state = self.hass.states.get(sid)
            if state and state.state not in ("unknown", "unavailable", "None"):
                try:
                    if is_temperature:
                        raw = to_internal_temperature_c(
                            self.hass, state.state,
                            unit=state.attributes.get("unit_of_measurement"))
                        if raw is None:
                            continue
                    else:
                        raw = float(state.state)
                    filtered = self._filter_sensor_value(sid, raw, is_temperature=is_temperature)
                    if filtered is not None:
                        values.append(filtered)
                except ValueError:
                    pass
        return round(sum(values) / len(values), 1) if values else None

    def _filter_sensor_value(
        self, sensor_id: str, raw: float, *, is_temperature: bool = True,
    ) -> float | None:
        """EMA-Glättung mit Spike-Erkennung – Ausreißer werden ignoriert.

        Plausibility guard runs first (temperature sensors only —
        ``is_temperature=False`` skips it so humidity/percent readings are
        never checked against a Celsius band), before any EMA state is
        touched: a garbage/out-of-range reading (e.g. a 127°C/-100°C device
        sentinel) is rejected before it can become the EMA baseline or be
        compared against an existing one. It never enters
        ``self._sensor_ema``, so the next genuinely plausible reading still
        sets a fresh baseline normally instead of being spike-rejected
        relative to a garbage seed. ``_read_avg_sensor()`` (this method's
        only caller) passes ``is_temperature`` through explicitly for both
        its temperature and humidity sensor paths; the default here only
        matters for direct/test callers.
        """
        if is_temperature and not is_plausible_temperature_c(raw, context=sensor_id):
            count = self._sensor_noise_count.get(sensor_id, 0) + 1
            self._sensor_noise_count[sensor_id] = count
            if count == 1:
                _LOGGER.debug(
                    "ThermoSmart '%s': Sensor %s unplausibler Wert verworfen: %.1f°C",
                    self.zone_name, sensor_id, raw,
                )
            return None

        if sensor_id not in self._sensor_ema:
            self._sensor_ema[sensor_id] = raw
            return raw

        ema = self._sensor_ema[sensor_id]
        if abs(raw - ema) > NOISE_FILTER_SPIKE_THRESHOLD:
            count = self._sensor_noise_count.get(sensor_id, 0) + 1
            self._sensor_noise_count[sensor_id] = count
            if count == 1:
                _LOGGER.debug(
                    "ThermoSmart '%s': Sensor %s Spike ignoriert %.1f°C (EMA=%.1f°C)",
                    self.zone_name, sensor_id, raw, ema,
                )
            return None

        self._sensor_noise_count[sensor_id] = 0
        self._sensor_ema[sensor_id] = NOISE_FILTER_EMA_ALPHA * raw + (1 - NOISE_FILTER_EMA_ALPHA) * ema
        return round(self._sensor_ema[sensor_id], 1)

    # ── Heizungsausfall-Erkennung ─────────────────────────────────────

    def _check_heating_failure(self, recommendation: dict) -> None:
        """Erkennt Heizungsausfall: TRV soll heizen, aber Raumtemperatur fällt.

        Bedingungen für Alarm:
          - Aktive Steuerung an (Beobachtungsmodus → kein Alarm, da ein externer Regler steuert)
          - TRV-Setpoint > aktuelle Temp + HEATING_FAILURE_CMD_DELTA (echter Heizbefehl)
          - Temperatur-Slope < –HEATING_FAILURE_SLOPE_THRESH (°C/min) für HEATING_FAILURE_DELAY_MIN
          - Kein offenes Fenster (das wäre erklärter Temperaturabfall)

        Alarm: HA Persistent Notification + Log-Warning.
        Alarm wird automatisch quittiert wenn Zieltemperatur erreicht ist.
        """
        if not self._active_control:
            self._heating_failure_since = None
            return

        trv_setpoint = recommendation.get("trv_setpoint")
        current_temp = recommendation.get("current_temp")
        target = recommendation.get("effective_target")   # use actual heating goal
        window_open = recommendation.get("window_open", False)

        if window_open or trv_setpoint is None or current_temp is None or target is None:
            self._heating_failure_since = None
            return

        is_heating_commanded = (trv_setpoint - current_temp) >= HEATING_FAILURE_CMD_DELTA
        slope = self._indoor_temp_slope

        if is_heating_commanded and (slope is None or slope < HEATING_FAILURE_SLOPE_THRESH):
            if self._heating_failure_since is None:
                self._heating_failure_since = self._clock.now_utc()
                _LOGGER.debug(
                    "ThermoSmart '%s': Möglicher Heizungsausfall – "
                    "Temp fällt trotz Heizbefehl (SP=%.1f°C, Ist=%.1f°C, Slope=%s°C/min)",
                    self.zone_name, trv_setpoint, current_temp,
                    f"{slope:.4f}" if slope is not None else "n/a",
                )

            elapsed_min = (self._clock.now_utc() - self._heating_failure_since).total_seconds() / 60
            if elapsed_min >= HEATING_FAILURE_DELAY_MIN and not self._heating_failure_notified:
                self._heating_failure_notified = True
                _LOGGER.warning(
                    "ThermoSmart '%s': HEIZUNGSAUSFALL – SP=%.1f°C, Ist=%.1f°C, "
                    "Slope=%s°C/min, seit %.0f min. TRV oder Wärmequelle prüfen!",
                    self.zone_name, trv_setpoint, current_temp,
                    f"{slope:.4f}" if slope is not None else "n/a", elapsed_min,
                )
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "persistent_notification", "create",
                        {
                            "title": f"ThermoSmart: Heizungsausfall – {self.zone_name}",
                            "message": (
                                f"**{self.zone_name}**: TRV soll auf **{trv_setpoint:.1f}°C** heizen, "
                                f"aber die Raumtemperatur fällt seit **{elapsed_min:.0f} Minuten** "
                                f"(aktuell **{current_temp:.1f}°C**, Ziel **{target:.1f}°C**).\n\n"
                                f"Bitte TRV, Heizkörper und Wärmequelle prüfen."
                            ),
                            "notification_id": f"thermosmart_heating_failure_{self.zone_id}",
                        },
                        blocking=False,
                    )
                )
        else:
            self._heating_failure_since = None
            # Alarm quittieren sobald Zieltemperatur erreicht
            if self._heating_failure_notified and current_temp >= target - 0.5:
                self._heating_failure_notified = False
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "persistent_notification", "dismiss",
                        {"notification_id": f"thermosmart_heating_failure_{self.zone_id}"},
                        blocking=False,
                    )
                )

    # ── TRV-Setpoint-Beobachtung ──────────────────────────────────────

    async def _async_observe_trv_setpoints(
        self, cfg: dict, recommendation: dict, weather_data: dict
    ) -> None:
        """No-op: existed solely to feed LE v1's setpoint-efficiency learning.

        LE v1 is frozen (LearningEngine.freeze() in __init__.py) and now only
        serves the schedule lookup via async_get_base_target(); the
        setpoint-efficiency observation this method fed was never applied.
        """
        return
