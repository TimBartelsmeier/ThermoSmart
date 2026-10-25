# ThermoSmart (fork) – Project Status

This document replaces the former `PROJECT_DESCRIPTION.md` and `IMPLEMENTATION_PLAN.md`.
Part 1 describes the **current state** (user-facing behaviour, plus the few
architecture facts needed to work on it). Part 2 lists **todos**: deferred features,
open decisions, leftovers and things that still need a live test.
The README is the user documentation; this file is the maintainer's view.

Backwards compatibility with the original project or earlier versions of this fork
is **not** a goal (no migration of old config entries).

---

# Part 1 – Current status

## 1. Concept

ThermoSmart controls the thermostatic radiator valves (TRVs) of a **heating zone**
(one config entry = one zone). A zone has exactly **one desired target temperature**.
Anything may change it – the user, a preset, a physical turn of a TRV dial, or a
schedule event – and the most recent change wins. There is no *Auto* mode and no
separate *override* state. Optional features on top: scheduling, pre-heating,
learning-based tuning, window handling, summer and vacation mode, and an
observation-only mode.

Two kinds of config entries exist:

* **ThermoSmart System** (one per installation, required for summer/vacation).
* **Heating zone** (one per room/zone).

## 2. Target model

* The **desired target** (5–30 °C) is what the climate entity shows. It is never
  silently altered; internal adjustments (TPI boost, pre-heat, suspensions) change
  only what is sent to the TRVs.
* **Sources (last writer wins):** user (climate entity/card), preset, physical TRV
  dial (optional per TRV), schedule event. User, preset and dial count as *manual*.
* **Presets** are plain named temperatures: Comfort, Eco, Night, Away, Vacation
  (per zone). Selecting one sets the target once. After a manual temperature
  change no preset is highlighted. *Away* is a plain preset (no presence detection).
* The desired target is restored after a Home Assistant restart; a restart does not
  replay schedule events.
* The climate entity advertises 5–30 °C, matching the target model.

## 3. Suspensions (window, summer, vacation)

Three conditions suspend normal control. While suspended the TRVs are driven by the
suspension, **but everything else keeps being registered** (target changes,
schedule events, the pre-heat plan). When the suspension ends the zone continues
with the then-current desired target – one mechanism, no per-case restore logic.

| Suspension | TRVs held at |
|---|---|
| Summer | Frost protection (12 °C), valves exercised periodically |
| Vacation | The zone's Vacation temperature |
| Window open | Per TRV: window-open temperature (default 5 °C) or HVAC `off` |

Priority: **summer > vacation > window**; the others are registered state only.

* A **dial change** during a suspension is registered as the desired target and the
  TRV is corrected back to the held temperature immediately (logged). During an
  open window the TRV stays in its window state.
* The climate entity keeps showing the desired target; the status sensor shows
  `window_open` / `vacation` / `summer` and attributes `suspension` and `held_at`.
* **Windows:** sensors with separate open/close delays; without sensors a
  falling-temperature (slope) detection is used.
* **System entry** (setup + options): optional weather entity, optional outdoor
  temperature sensor, summer on/off thresholds (defaults 18/15 °C). Entities:
  *Vacation Mode*, *Force Summer Mode*, *Auto-enable Summer Mode* (switches).
  Automatic summer uses a rolling 72 h average of the System entry's outdoor
  temperature (sensor, else weather entity) with hysteresis and takes **no
  decision before a full 72 h of samples exist** (history is not persisted).
  Without any outdoor source it never enables summer.

## 4. Scheduling

* A schedule is a list of events: *at time T on these weekdays, set the target to X*
  (X = temperature or preset name). No time slots, no "active event".
* Events fire at their **exact time** (timers), not at the next control cycle.
  Events missed while scheduling was off/paused or HA was down are **skipped, never
  caught up**.
  The timer handler is marked as an HA callback so target updates, refresh
  requests and entity notifications run on the event loop (covered by a real-timer
  integration test).
* Events are edited in the setup/options config flow and stored with the zone's
  config entry. Overlapping weekday/time events are rejected. Existing
  `/config/thermosmart_schedules.yaml` files are not read or migrated.
* Per event flag `preheat`.
* Controls per zone: *Scheduling enabled* switch, *Scheduling disabled until*
  date/time; sensors *Next scheduled event* and *Pre-heat start*.
* During vacation/summer events still fire but only update the desired target that
  applies after the suspension.

## 5. Pre-heating

* An event with `preheat: true` and a target higher than the current target makes
  the zone start heating early so the target is reached **at** the event time.
* Lead time: learned heat-up behaviour if learning is on and has data, otherwise a
  deterministic default (≈1.5 °C/h + 5 min onset), capped at 180 min.
* Only the **next** event is considered; never earlier than the previous event.
* Once started, pre-heat stays active until the event fires (a shrinking estimate
  cannot end it).
* A **manual change while pre-heat is running** (or after its planned start but
  before the start pass ran) cancels it for that event; the event itself still
  fires. A manual change *before* the planned start does not cancel.
* During a suspension pre-heat is not performed but the plan is kept: if the
  suspension ends after the planned start and before the event, pre-heat starts at
  that moment. No special branch – the plan is computed every pass.
* Exposed: *Pre-heat start* sensor (attributes state, reason, event, duration and
  source), diagnostic *Pre-heat minutes*, status `preheating`.

## 6. TRV control

* **Zone control mode** (default for all TRVs): *Setpoint only*, *TPI setpoint
  modulation* (boost up to +8 °C), *Direct valve control*. Further zone options:
  minimum control interval (5 min), temperature tolerance, external-temperature
  write threshold, physical-setpoint debounce and settling time (5 s each).
* **Per TRV:** control-mode override, valve entity, feed room temperature to the
  TRV (+ entity), calibration (entity, on/off, invert), valve exercise, *turn off
  when window open*, *accept physical setpoint changes*, **state settings**
  (select/number/switch entities kept at a value; written only if different).
  Known devices are pre-filled from a device profile; detection happens only in
  the flow, at runtime exactly the configured values apply.
* **Physical changes on a TRV:** if enabled, the reported setpoint (after debounce)
  becomes the zone target; echoes of own writes are ignored during the settling
  time; if disabled the change is corrected back.
* **Active Control / Learning:** on+off = deterministic TPI; on+on = control with
  learned parameters; off+on = **observation mode** (no writes to TRVs of any
  kind – including the external-temperature feed; target, events and pre-heat plan
  are still tracked); off+off = inactive. HVAC `heat`/`off` on the climate entity
  maps to Active Control.
* **Safety:** missing room sensors fall back to the TRV's own sensor; spike
  filtering; offline sensors averaged out; heating-failure detection (35+ min);
  valve maintenance in summer/vacation; device profiles with generic fallback.
* **Availability:** configured sensor/weather/TRV entities and enabled device
  inputs are monitored via state-change listeners after HA startup completes.
  Missing/unknown/unavailable entities create non-persistent, auto-clearing
  Repairs, one per entry/entity. A continuous 3 min outage logs once; recovery
  logs at info level only after that warning. No polling or control changes;
  fallbacks do not hide missing configured sources. Options reload/unload cleans
  up issues and timers. Write failures and heating-failure alarms are unchanged.

### Control-pass triggers

Every refresh goes through `coordinator.request_pass(trigger)` with a name from
`triggers.py` (unknown names raise; a test asserts nothing else refreshes). The last
pass's reasons are exposed as `pass_triggers`. Bursts coalesce through the
coordinator's refresh debouncer.

`periodic` · `target_change` · `schedule_event` · `preheat_start` · `window`
(open/close delay expired – a raw sensor change does not trigger) ·
`room_temperature` (moved ≥ threshold) · `physical_setpoint` · `switch` (Active
Control/Learning) · `hvac_mode` (a controlled TRV enters an unwanted HVAC mode;
the existing watchdog's profile and suspension checks still apply) · `scheduling` ·
`system_state` · `startup` (incl. options reload).
Nothing else (attribute noise, humidity, small drift) causes a pass.

## 7. Outdoor data and learning

* **Outdoor data** per zone: temperature, humidity, wind, solar, rain – each from the
  zone's outdoor sensor if available, else the weather entity's *current
  conditions*, else `None` (`outdoor_sources` records which). No forecasts.
* Used by **learning** and **control** (outdoor part of TPI, pre-heat estimate); it
  does **not** change the target. Everything works without outdoor data.
* **Learning** (self-contained `learning/` package): heat-up rate, heat loss, onset
  delay, boost, after-heat; exposes *Learning progress* and diagnostic sensors,
  local-only storage and diagnostics download. Used for pre-heat duration, TPI
  coefficients and adaptive boost.

## 8. Config flow

Setup and options flow use one implementation (`_ZoneStepsMixin`), the same steps and
identical texts (tested): **Devices → Control strategy → Per TRV (incl. state
settings) → Temperatures → Schedule editor → Outdoor**. The only differences: the
options flow starts with a section menu; setup pre-fills the outdoor section from
the System entry's weather entity.

## 9. Entities

**Per zone:** climate · Preset select · Active Control switch · Learning switch ·
Scheduling enabled switch · Scheduling disabled until · Status sensor
(`observation_mode`, `controlling_no_learning`, `heating`, `idle`, `preheating`,
`window_open`, `vacation`, `summer`, `heating_failure`, `disabled`, `active`;
attributes incl. `suspension`, `held_at`, `pass_triggers`, `outdoor_sources`) ·
Learning progress · Next scheduled event · Pre-heat start · diagnostic sensors
(disabled by default): Pre-heat minutes, *TPI boosted TRV setpoint*, TPI duty cycle,
temperature slope, heat loss, heating power, outcome score, solar heat gain, TRV
observations, window cooling rate, EMA temperature.

**System:** Vacation Mode · Force Summer Mode · Auto-enable Summer Mode.

**Removed vs. the original:** Auto mode and override, weekday/weekend schedule
fields, presence-based Away, weather offsets, forecast suppression, summer indoor
safety bypass, global debug switch, *Adjusted target* and *Weather offset* sensors,
`away` status.

## 10. Code map

| Module | Role |
|---|---|
| `target.py` | `TargetState`/`DesiredTarget`, single writer API, change listeners |
| `suspension.py` | `resolve()` (priority, held temperature), `SummerDetector` |
| `schedule.py` | event validation, next/previous occurrence, timers |
| `preheat.py` | pure pre-heat planner |
| `outdoor.py` | `OutdoorConditions.read()` (sensor → weather → None) |
| `availability.py` | configured-entity availability Repairs, delayed warning/recovery logging |
| `system.py` | System entry controller (summer detection, vacation/summer state) |
| `triggers.py` | the trigger table |
| `coordinator.py` | thin glue: inputs → decide → dispatch → publish |
| `trv_control.py`, `device_profiles/`, `tpi.py`, `window.py`, `maintenance.py` | TRV dispatch and helpers (kept from the original) |
| `learning/`, `learning_engine.py` | learning package (+ legacy LE v1, now only what shadow/export still need) |

Tests: `uv run python -m pytest -q -p no:cacheprovider --asyncio-mode=auto`
(≈1290 passed, 23 skipped – the skipped ones are non-English translations; ≈95 s).
Guard tests: `test_no_legacy_concepts.py` (removed concepts must not return),
`test_triggers.py` (single refresh entry point), `test_zone_flow_parity.py`,
`test_zone_setup_entities.py` (full HA setup of a zone).

---

# Part 2 – Todos

## A. Deferred features (planned, but not implemented yes)

1. **Translations other than English.** New/changed strings exist in English only
   (`strings.json` == `translations/en.json`, enforced). Other language files were
   left untouched and fall back to English; the completeness test skips them
   (marker in `tests/test_translations_complete.py` – remove it when translating).
   Note the renamed/removed steps (`devices`, `temperatures`, `outdoor`) and new
   entities when updating.
2. **Lovelace card** (`www/thermosmart-card.js`, bundled). Not updated: still has the
   *Auto* mode button/labels (`mode_auto`) that no longer do anything, and does not
   know about suspensions, scheduling or the pre-heat sensors. It keeps working as
   far as possible.
3. **Early cutoff / coasting.** When the zone heats towards a target that was
   *raised* (from any source), heating may stop slightly before the target and let
   after-heat finish; released on window open and when the target changes again.
   It was removed from the coordinator (it had no tests and was entangled with the
   Auto/target code); the learning-side models still exist, and the old
   implementation is in git history (`main`, `feature/improved-control-modes`).
   Re-implement as a separate pure module with tests, consuming the desired target
   change event.
5. **Weather-based correction of the target** (weather offset, forecast
   suppression). Deliberately removed; outdoor data is used for learning/control
   only. Re-add it as an explicit option, or remove the leftovers below for good.
6. **Migration path for configs created under the "old" version**
7. **Cleanup documentation** Ensure README is comprehensive
8. **Check** logic, edge cases, presets, etc.

## B. Leftovers to resolve

* The `learning/` package (and `learning_engine.py`, `const.py`, `export.py`) still
  holds the **forecast trust/bias models** (`FORECAST_*` constants, `forecast_bias`,
  `forecast_high`, `has_forecast`, notes in `learning/decision/legacy_map.py`). Nothing feeds or uses
  them. Remove them – or reconnect them if item A5 is done. Storage schema impact
  must be considered when removing.
* `mode_context` (learning outcome/history schema) is a former mode name, kept only
  as informational data; `control_reason` still appears in learning contracts.
* Legacy `learning_engine.py` (LE v1) is frozen; remove what the shadow controller
  and export no longer need.
* Pre-existing unused imports in `export.py` and `device_profiles/profiles.py`.
* No migration from earlier config entries/entity ids (e.g. the select's unique id
  changed from `_mode` to `_preset`; old entities become orphans). Remove them by
  hand or re-create the zones.

## C. Open decisions / assumptions to confirm

* **Observation mode** keeps tracking target, events and the pre-heat plan (only TRV
  writes are skipped). Assumed, not explicitly confirmed.
* The **summer frost temperature** stays the constant 12 °C (not configurable).
* A **window-sensor state change no longer forces an immediate pass**; only the
  open/close delay timers do.
* A manual change **before** the planned pre-heat start does not cancel it (so
  "set 21 °C in the evening" does not kill tomorrow's 06:00 pre-heat).
* **Auto-summer** needs a full 72 h of samples after every HA restart (history is
  not persisted). Option: persist the samples, or make the span configurable.
* **Schedule file reload:** only on options reload. A `thermosmart.reload_schedules`
  service or a file watcher was discussed and judged unnecessary for now.

## D. Needs a live test (not testable in the test harness)

* Burst coalescing with the real HA refresh debouncer (default 10 s cooldown): rapid
  dial turns must not noticeably delay the first reaction.
* Outdoor fallback with real weather entities (units, wind in m/s).
* Pre-heat and schedule timers across HA restarts (nothing is caught up, the plan is
  rebuilt on the first refresh).
* Real TRV dispatch of the pre-heat target (effective target above the desired
  target).
* The zone setup/options flow in the real UI: schedule event add/edit/remove and the
  options section menu.
* After the climate-attribute fix (`target.current.source`): all entities of a new
  zone must be available; any other entity errors in the HA log should be reported.
* TRV state-settings and external-temperature feed in Active Control (and no writes
  in observation mode).
