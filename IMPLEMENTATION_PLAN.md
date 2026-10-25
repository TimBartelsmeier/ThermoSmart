# ThermoSmart (fork) – Implementation Plan

Companion to [PROJECT_DESCRIPTION.md](./PROJECT_DESCRIPTION.md) (the *what*).
This document is the *how and in which order*. Status: **WP0–WP10 implemented**
(kept as a record; the code and README are authoritative).

Run tests with `uv run python -m pytest --asyncio-mode=auto` (repo root `.venv`).

---

## 1. Starting point and baseline

* Base: `feature/improved-control-modes` (as agreed). The current branch
  `feature/improved-control-and-scheduling` still equals `main`, so step 0 is a
  fast-forward to the modes branch (no other branch is touched).
* Baseline measured on the modes branch: **1124 tests pass** (~96 s).
* From `feature/flexible-scheduling` I *port* (not merge): the schedule YAML
  parsing/management (`schedule.py`), the scheduling switch + datetime entities,
  the next-event / pre-heat sensors, and the ideas from its README/tests. Its
  coordinator changes are **not** taken – they are the "band-aid" part.
* Impact surface measured on the modes branch (files / test files touching it):
  auto mode 7 / 10 · override 12+ / 18 · presence 6 / 10 · forecast 12+ / 14 ·
  weather offset 5 / 5 · old schedule fields 2 / 3 · early cutoff 3 / **0** ·
  debug switch 2 / 0.
  Early cutoff has no tests today, which is one more reason to take it out of
  the core and restore it later with tests.

## 2. Approach: rewrite the decision core, keep the leaves

`coordinator.py` (2700 lines; `_async_update_data` ≈ 600 lines and
`_compute_recommendation` ≈ 430 lines interleave target choice, weather, presence,
pre-heat, early cutoff, learning, dispatch) is rewritten around small **pure,
HA-independent modules** that are unit-testable with an injected clock. The
coordinator becomes thin glue: *collect inputs → decide → dispatch → publish*.

**Kept as-is (leaves):** `trv_control.py` (dispatch, device profiles, state
settings, window-off), `device_profiles/`, `tpi.py`, `window.py` (detection),
`temperature_units.py`, `maintenance.py`, the whole `learning/` package and its
storage/export/diagnostics, `diagnostics.py`, the physical-setpoint debounce and
temperature-trigger logic from the modes branch (re-wired onto the new core).

**New / rewritten pure modules**

| Module | Responsibility |
|---|---|
| `target.py` | `DesiredTarget` (value, source ∈ user/preset/dial/event, preset name or none). Single writer API `set(value, source, preset=None)`; emits "changed" so pre-heat cancellation and refresh trigger hang off one place. |
| `suspension.py` | Pure function `resolve(window, summer, vacation, cfg) -> Suspension(kind, held_temp \| hvac_off)`; priority summer > vacation > window. TRV-facing temperature only; never touches the desired target. |
| `schedule.py` | Ported: YAML read/validate/manage (no legacy migration, no "active event" resolution). Provides `next_occurrence(events, now)` and `occurrences_between` only. |
| `preheat.py` | Pure planner: inputs (desired target, next occurrence, previous occurrence, duration, now, cancelled-for-event flag, scheduling active) → `PreheatPlan(start, state, reason)`. Encodes "only next event", "never before the previous event", "target must be higher", "manual change cancels for this event". |
| `outdoor.py` | Zone outdoor view: per value sensor → weather-entity *current conditions* → `None`. Replaces `weather_engine` usage in zones; no forecast, no offsets, no suppression. |

**Coordinator pipeline per control pass**

1. inputs: room temp, window state, outdoor view, global summer/vacation flags
2. desired target (already up to date: set by user/preset/dial/event writers)
3. `suspension.resolve(...)`
4. `preheat.plan(...)` (computed even while suspended, so it can start on resume)
5. *setpoint target* = suspension.held_temp if suspended, else pre-heat target if
   pre-heat active, else desired target
6. TPI / learning reads (outdoor view, may be `None`) → per-TRV dispatch via the
   existing `TRVControlMixin`
7. publish zone data (status, attributes: suspension, held-at temperature,
   pre-heat plan, next event)

**Timers.** One `async_track_point_in_time` timer for the next schedule event
and one for the planned pre-heat start, re-armed whenever any input changes
(target change, schedule reload, options reload, duration re-estimate at each
pass, suspension change). Both fire a refresh pass. All time comes from the
coordinator's injectable clock so tests can drive it.

## 3. Work packages (in order; each ends with a green suite and a commit)

Intermediate commits are fine per your note; branches other than the current
one are never modified. Each package lists its tests.

### WP0 – Setup
* Fast-forward the branch to `feature/improved-control-modes`; run the suite
  (expect 1124 passed); commit `PROJECT_DESCRIPTION.md` and this plan.
* Remove the temporary read-only worktree I created for analysis.

### WP1 – Strip legacy concepts, introduce single desired target
Remove: auto mode and `HEATING_MODE_AUTO`, override (`_override*`,
`set_override`, override-period expiry, override clearing on presence), presence
(config step, listeners, `_get_presence_state`, `all_away`, away status),
weather offset, forecast suppression, forecast/trust reads in the coordinator,
early-cutoff lifecycle (learning-side models stay), summer indoor-safety bypass,
global debug switch, weekday/weekend schedule fields and
`_minutes_until_next_comfort` / `_current_schedule_period`, LE v1
`async_get_base_target`, `Adjusted target` sensor, `Weather offset` sensor.
Introduce `target.py`; climate entity, preset select and physical-dial adoption
write through it. Presets become plain temperatures (no highlighted preset after
a manual change). Restore desired target on HA restart (climate `RestoreEntity`).
*Tests:* delete/rewrite the tests for removed concepts (≈10 auto, 10 presence,
14 forecast, 5 offset, 3 schedule test files); new `test_target.py` (last writer
wins from every source, preset semantics, restore); climate/select entity tests.
Grep-based guard test: no `HEATING_MODE_AUTO`, `override`, `presence` identifiers
in non-learning source (excluding the documented learning leftover).

### WP2 – Scheduling
Port `schedule.py` (parse, zone key, managed YAML, duplicate/orphan warnings),
drop legacy migration. Exact-time timers apply events through `target.py`
(preset events set the preset temperature). Events missed while HA was down or
scheduling was off are not caught up. Add `Scheduling enabled` switch and
`Scheduling disabled until` datetime (restorable). Observation mode: events still
update the desired target (assumption, see §5). YAML is re-read on options reload
(see question Q1).
*Tests:* port `test_schedule.py`; timer tests with fake clock (exact firing,
weekday/week rollover, duplicate timestamps, DST day, timezone, disabled /
paused / disabled-until expiry, no catch-up after restart, preset vs numeric
event, invalid YAML → clear error); YAML management tests (add missing zones,
sort, never modify/delete existing).

### WP3 – Suspensions and the System entry
`suspension.py`; window handling from `window.py` feeds it. System entry
reworked: setup/options with weather entity, outdoor temperature sensor and
summer thresholds; entities *Vacation Mode*, *Force Summer Mode*,
*Auto-enable Summer Mode* (switches, restorable); automatic summer detection from
the System entry's own outdoor source (72 h average, configurable on/off
thresholds, hysteresis kept), only when an outdoor temperature exists.
Precedence summer > vacation > window. Desired target keeps being updated during
suspension; physical dial change during suspension is registered **and** corrected
back immediately with a log line. Status sensor shows `window_open` / `summer` /
`vacation` and attribute *held at* temperature; the climate entity target stays
the desired target. Valve maintenance during summer/vacation retained.
*Tests:* pure `resolve` truth table (all 8 combinations); coordinator: events and
user changes during each suspension update the desired target and are applied on
resume; overlapping suspensions release in the right order; dial change
corrected + registered; auto-summer hysteresis and "no outdoor data → never
auto-enables"; force toggle overrides auto; restart restores toggles.

### WP4 – Pre-heating
`preheat.py` + coordinator integration. Duration from learning shadow if
available, otherwise deterministic baseline (existing code), capped 180 min,
minimum delta kept. Entities: *Pre-heat start* (timestamp, attributes: event,
event target, duration, state, reason), *Next scheduled event* (timestamp,
attributes), diagnostic *Pre-heat minutes*; status `preheating`.
Rules covered: higher target only; scheduling must be active; plan kept during
suspension and started on resume if the start time has passed and the event has
not fired; start never earlier than the previous event; any manual change
(user/preset/dial) cancels it for that event only; event still fires on time.
*Tests (scenario timelines with fake clock):* basic lead-time boundaries; numeric
and preset events; no preheat for lower/equal target or `preheat: false`;
overlapping events A→B; window open at planned start → starts on close; window
opens mid-pre-heat and closes before / after the event; summer/vacation across
the event; manual change before and during pre-heat; scheduling paused during
pre-heat; duration re-estimate moving the start; learning vs deterministic vs
no-temperature; stale plan after the next event changes; sensor attributes in
every state.

### WP5 – Outdoor data, learning and control inputs
`outdoor.py` (sensor → weather-entity current conditions → `None`; unit
normalisation reused from `weather_engine`). Feed learning shadow and TPI
(outdoor part) and pre-heat estimate from it. Remove `weather_engine` forecast
and offset code (keep sensor reading + unit conversion). Verify full operation
with no outdoor data at all.
*Tests:* per-value fallback matrix (sensor ok / unavailable / absent × weather
ok / absent); unit conversion cases kept; "no outdoor anything" run through
several coordinator cycles with learning on and off without errors and with
neutral TPI outdoor term; confirm no forecast attribute is read anywhere.

### WP6 – Control-cycle triggers audit
Make the trigger list from the description the single mechanism, one table in
code and one test per row: periodic · target change · schedule/pre-heat timers ·
window state (after delay) · room temperature ≥ threshold · physical setpoint
(after debounce) · Active Control/Learning/Scheduling/Summer/Vacation switches ·
options reload. Assert that other state changes (e.g. TRV attribute noise) do
**not** trigger a pass, and that bursts coalesce into one pass.

### WP7 – Config flow (setup = options)
One shared implementation of the step sequence (devices & sensors → control
strategy → per-TRV settings/state settings → temperatures → weather) used by
both flows; flow-specific differences only where unavoidable and flagged in the
UI. System entry flow as in WP3. Zone-name step shows the schedule key and
warnings. English strings only (`strings.json` + `translations/en.json`);
other languages untouched/deferred; completeness test restricted to English plus
a check that other languages contain no *removed* keys that would crash
(validate whether HA tolerates stale keys, otherwise prune minimal).
*Tests:* both flows produce identical data for identical input; validation
errors; per-TRV loop; state-settings add/remove; System flow; reconfigure keeps
existing values.

### WP8 – Entities, diagnostics, export
Final entity set per description §9; status values; attributes; rename
*TPI boosted TRV setpoint* (display name only, keep diagnostics-sensor
disabled by default); diagnostics/export updated for removed concepts
(learning-schema fields such as `manual_override_count` stay in the schema as
they are part of persisted/exported formats; they are populated from physical /
user target changes or 0 – decision in Q3).
*Tests:* entity registry snapshot (names, defaults, enabled-by-default flags),
sensor attribute tests, diagnostics/export privacy tests (existing) stay green.

### WP9 – README rewrite
Rewrite for clarity and completeness (concepts first: desired target, presets,
schedules, suspensions, pre-heat; then setup, entities, device compatibility,
FAQ, diagnostics). Include the **learning leftover note** (forecast trust/bias
models unused – remove or reconnect if weather correction returns) and the
deferred list. Remove the stale statements (Auto mode, presence, weekday/weekend
fields, "auto" status). Add the schedule YAML reference with examples and a
behaviour table for suspensions. Markdown-only; no tests besides a link check by
eye.

### WP10 – Consistency sweep and final verification
Grep for leftover legacy concepts (the pitfall noted in your WIP file), dead
constants/imports/strings, unused `weather_engine`/LE v1 paths; run full suite;
update `hacs.json`/manifest only if needed; delete my analysis helpers; final
review pass of the diff. Update both planning docs to "implemented" status or
remove them at your discretion.

## 4. Test strategy summary
* **Pure modules** (`target`, `suspension`, `schedule`, `preheat`, `outdoor`):
  table-driven unit tests, no HA.
* **Coordinator scenarios**: existing fake-hass helpers + injected clock;
  timeline tests (advance clock, fire timers, assert TRV writes and entity data).
* **Entities/flows**: existing HA test harness (`pytest-homeassistant-custom-component`).
* Every behaviour sentence in PROJECT_DESCRIPTION.md should map to at least one
  test; I will keep a short traceability list in the PR/commit messages, not in
  the repo.
* No live-HA testing is possible here; WP-level "manual check" notes will be
  listed for you at the end of each package where real devices matter
  (dispatch, timers across HA restarts).

## 5. Assumptions I'm making (please correct)
1. In **observation mode** (Active Control off) the desired target, schedule
   events and pre-heat plan still run; only TRV writes are skipped.
2. **Away** and **Vacation** presets remain plain presets; *Vacation Mode*
   (global) is separate and holds the zone's vacation temperature.
3. Frost-protection temperature in summer stays the existing constant (12 °C).
4. Pre-heat plan and desired target survive restarts only as far as the climate
   entity restores the target; the plan is recomputed on start.
5. The schedule file is read at setup and on options reload.
6. LE v1 (`learning_engine.py`) stays only for what the learning shadow/export
   still needs; its target helper is deleted.

## 6. Questions for you
* **Q1 – Schedule file reload.** Re-reading only at setup/options reload means
  editing the YAML requires a reload. Options: (a) reload via the integration's
  "reload" menu (default), (b) file-watch with auto reload, (c) a service
  `thermosmart.reload_schedules`. I suggest (c) in addition to (a) – explicit,
  no polling. OK?
* **Q2 – Early cutoff removal now.** It is deferred, has no tests, and is
  entangled with Auto/target code, so WP1 *deletes* it from the coordinator
  (learning-side models remain; git history has the code). OK, or do you want it
  parked behind a feature flag instead?
* **Q3 – Learning "manual override" counters/events.** Keep the persisted
  schema field and feed it with manual target changes (user/dial), or feed 0 and
  mark it obsolete? I suggest feeding manual changes, since it stays meaningful.
* **Q4 – Commit granularity.** One commit per work package, plus the fast-forward
  – fine?
* **Q5 – Checkpoints.** I'd stop for your review after WP1 (core model) and WP4
  (pre-heat), then continue unless you want more. Fine?
