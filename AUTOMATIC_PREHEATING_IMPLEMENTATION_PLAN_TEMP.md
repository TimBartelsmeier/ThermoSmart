# Automatic Pre-Heating Before Schedule Events

Temporary recovery plan for the implementation described in
`FLEXIBLE_SCHEDULING_WIP.md`.

## Confirmed behavior

- `preheat: true` applies to any scheduled target increase, including numeric
  targets and preset targets.
- Pre-heating is independent of the legacy `auto` mode.
- Existing scheduling controls remain authoritative:
  `switch.*_scheduling_enabled` and
  `datetime.*_scheduling_disabled_until`.
- Preset selection remains a direct target-temperature selection and does not
  enable scheduling.
- Add dedicated debug sensors for the next event and planned preheat start.

## Implementation sequence

### 1. Centralize next-event timing

Update `custom_components/thermosmart/schedule.py` and
`custom_components/thermosmart/coordinator.py` to provide one canonical
calculation for the next weekly event. The result should include:

- event
- local event datetime
- minutes until the event
- resolved target temperature
- preheat flag

It must handle same-day events, weekday and weekly-boundary rollover,
duplicate timestamps, local timezone behavior, and the injected coordinator
clock. Reuse this result in control logic and entities.

### 2. Remove legacy preheat coupling

Refactor the coordinator preheat path around the event target rather than
`HEATING_MODE_AUTO` and a hard-coded comfort transition:

1. Resolve the active schedule target and next event.
2. Resolve the next event's target temperature.
3. Compare it with the current active target.
4. Read the learned or deterministic preheat duration.
5. Preheat only when scheduling is active, the event has `preheat: true`, the
   next target is higher, and the event is within the calculated lead time.
6. Use the upcoming target during the preheat interval without prematurely
   mutating the schedule event.
7. Let normal schedule application take over at the event time.

Retain the existing learning/deterministic fallback and maximum preheat
duration. Preserve higher-priority suppression from windows, summer/vacation
state, inactive control, and scheduling pauses. A user-selected temperature is
the current target, not a separate override mode or a preheat suppression
condition; a later schedule event may replace it normally.

### 3. Add canonical coordinator state

Expose canonical zone data for:

- next event key and datetime
- next event target and resolved temperature
- next event preheat flag
- planned preheat start datetime
- preheat state
- preheat suppression reason

Existing status and diagnostic fields should consume these values rather than
recomputing them independently.

### 4. Add dedicated debug sensors

Update `sensor.py`, `strings.json`, and translations with:

1. **Next scheduled event**: datetime state with event target, resolved target,
   preheat flag, minutes until event, and event key attributes.
2. **Preheat start**: datetime state with event datetime, duration, current
   preheat state, and suppression reason.

Keep `sensor.*_preheat_minutes` as the numeric calculated duration. Keep
`sensor.*_status` as the coarse overall state, including `preheating`.
Do not duplicate planned start time in the duration sensor; use the new
dedicated sensors for timing and state detail.

### 5. Add tests

Extend `tests/test_schedule.py` for next-event timing, rollover, duplicate
timestamps, target resolution, and the preheat flag.

Extend coordinator tests for:

- numeric and preset target increases
- non-auto operation
- disabled or absent preheat flags
- lead-time boundaries
- event-time termination
- scheduling pause/disable
- window, summer/vacation, and active-control suppression
- learning, deterministic, and shadow-only paths
- stale-state prevention when the next event changes

Add entity tests for the two new sensors, their attributes, and no-event or
ineligible-event behavior.

### 6. Update documentation

Update `README.md`, `strings.json`, translations, and schedule examples:

- remove the statement that general per-event preheating is not enabled
- document that `preheat: true` applies to target increases
- document the two debug sensors
- distinguish duration, next event, planned start, and overall status

## Validation

Run targeted tests first:

```bash
uv run python -m pytest \
  tests/test_schedule.py \
  tests/test_coordinator_modes.py \
  tests/test_coordinator_recommendation.py \
  tests/test_four_mode_matrix.py \
  tests/test_four_mode_matrix_extended.py
```

Then run the full suite:

```bash
uv run python -m pytest
```

## Main implementation risk

Event timing, preheat state, and schedule application must use one canonical
calculation. Otherwise the new debug entities could hide inconsistent control
behavior instead of exposing it.

Ensure edge cases are handled properly (for example: multiple schedule events close to each other with the pre-heating potentially overlapping the previous event; manual changes, opened windows, drops in temperature, etc. in between calculation of pre-heating start and actual start; ...)