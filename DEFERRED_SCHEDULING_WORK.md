# Deferred Flexible-Scheduling Work

This file temporarily tracks behaviours that were coupled to the legacy
`auto` mode and must be reworked for the flexible-scheduling model. These
features are intentionally deferred and must not be reintroduced by treating a
temperature preset as an automatic control mode.

## Deferred behaviours

- **Presence-based automatic Away switching**  
  Reimplement presence handling as an independent target-selection or
  scheduling rule rather than as an `auto`-mode side effect.
- **Weather offsets and forecast suppression**  
  Define how weather adjustments apply to explicit preset targets, numeric
  schedule targets, and periods without an active event.
- **Automatic pre-heating before a schedule event**  
  Attach pre-heating to the relevant schedule event without changing the
  selected preset state.
- **Learning-based early-cutoff/coasting holds**  
  Rework learning holds so they operate as an independent control adjustment
  and do not depend on `auto`.
- **Automatic expiry of manual overrides at a schedule-period boundary**  
  Define whether and how a manual numeric target is replaced when the next
  schedule event becomes active.

## Internal mode and target-source rework

The coordinator currently maintains both:

- `_mode`, a legacy internal mode that defaults to Comfort and is still used
  by control and recommendation code; and
- `_preset_mode`, the explicit user-visible preset, which is `None` when the
  current target is numeric/manual or no preset has been selected.

This split prevents the legacy default mode from highlighting the Comfort
preset, but `_mode` still represents the old mode-based architecture. It must
be replaced or narrowed so that target selection is based on explicit sources:

1. a named temperature preset selected by the user;
2. the currently active schedule event, containing either a numeric target or a
   named preset; and
3. independent deferred adjustments or control features listed above.

The rework should identify a single authoritative target source, remove
mode-based assumptions from the coordinator, and keep the climate entity's
`preset_mode` set to `None` whenever the target is not provided by a named
preset.

## Current implementation constraint

The behaviours above currently contain legacy `auto` checks in the
coordinator. They remain deferred while the preset/event model is introduced.
Future changes must implement them as independent features and must not
silently restore `auto` as a user-facing preset.
