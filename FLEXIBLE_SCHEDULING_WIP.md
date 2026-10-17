# Deferred Flexible-Scheduling Work

A significant change in how this thermostat coordinator works has been made on this branch (feature/flexible-scheduling) compared to how it previously worked (branch main).

Description of the intended behaviour:
The behaviour of the coordinator should not depend on special modes or presets like the "auto" mode (was also sometimes called or presented to the user as a preset, which was confusing) from the previous implementation. Presets are just variables for temperatures. When applied, they simply set the target temperature for the zone, nothing else. They don't encode other information.

"Advanced features" like scheduling, pre-heating, target temperature overrides based on presence detection, etc. (see below) should be optional and _supplement_ this basic behaviour. In the previous implementation, most of them relied on a special "auto" mode/preset, which does not fit the new scheme and should be removed. Because the nature of these advanced featurs differs significantly, there is no one-fits-all solution for how to rework them for the new scheme/behaviour.

Because refactoring everything at once would be a huge change to the codebase and difficult to test properly (since proper testing must be done on a live homeassistant instance), it was split into smaller work packages. This document should track the progress on necessary under-the-hood changes and feature reworks.

## Rules for AI agents working on this
Coding agents may not make decisions about how the features listed below should work, i.e. not about the "user facing" logic. If the developer has not described something sufficiently, or if you identify an oversight, present the developer with your results and ask for input/a decision. 

You may make decisions about implementation details if there is a clear favorite. When in doubt, also ask the user.

Do not try to "tick off" this entire list in one go. Only work on the tasks the developer has explicetly specified. If there is some foreseeable coupling between tasks and you think the current "work package" has to be broadened, ask for permission.

## Step 1: implement/rework features
- **Scheduling**
  Done. Scheduling now happens via "change events". The user can define these events in a yaml file. An event is set for a specific time of day on specific days of the week. When the event is fired, the target temperature is changed to a defined numeric value; or a preset is applied (which in turn also changes the target temperature).
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
As mentioned above, the nature of the features differs significantly and there are multiple valid ways to implement them.

## Step 2: ensure internal consistency & test coverage
Once all features are reworked, the codebase should be evaluated to make sure that there is no "legacy" code left and that the new behaviour is covered sufficiently by tests. This encompasses checking whether some "legacy concepts" have sneaked into the logic, not just checking for unused properties/functions.

## Step 3: Quality-of-life and user facing controls
Finally, once the logic is working properly and has been verified, some quality-of-life features can be implemented. Not all of these are planned, this is a do-not-forget-list, not an absolutely-do-this-list.

- Convenience features regarding the yaml file. The yaml-file should be "managed" by the software to some extent: missing zones should be added (without any events set), and the list should be sorted by key. Existing zones may not be changed in any way (except for their order in the list), and if a zone if configured which currently does not exist it may not be removed.
- Update or add card(s) to reflect these changes
  - "Info" card to show the next scheduled events, pre-heating times, etc.
  - Toggle and datetime-picker for scheduling override either in dedicated card or integrated into main card
  - Toggles for away mode etc.