# ThermoSmart (fork) – Project Description

Status: **v4 – all open questions settled, pending your final read-through** – derived from `main`, `feature/improved-control-modes` and
`feature/flexible-scheduling` plus the decisions made in the planning session.
This describes *what the user sees and can do*, not how it is implemented.
Backwards compatibility with earlier versions is explicitly **not** a goal.

Items marked **[deferred]** are wanted but not part of the first milestone.
Items marked **[removed]** exist in `main` and are intentionally taken out of the
code base for now (not just disabled).

---

## 1. Concept in one paragraph

ThermoSmart controls the thermostatic radiator valves (TRVs) of a **heating zone**
(one config entry = one zone). A zone has exactly **one current target
temperature**. Anything may change it – the user, a preset, a physical turn of a
TRV dial, or a schedule event – and the most recent change wins. There is no
"Auto" mode and no separate "override" state. On top of this basic behaviour,
optional features add value: scheduling, pre-heating, learning-based tuning
(including early cutoff), window handling, summer and vacation mode. The
integration can also run in an observation-only mode and learns the thermal
behaviour of each zone.

Two kinds of config entries exist:

* **ThermoSmart System** (global, one per installation): vacation and summer controls.
* **Heating zone** (one per room/zone).

---

## 2. Target temperature model

* **Desired target**: the temperature the zone wants to have. It is what the
  climate entity, the card and the *Adjusted Target* sensor show. It is never
  silently altered; internal adjustments (e.g. TRV control boost) do not change
  it.
* **Sources that set the desired target (last writer wins):**
  1. the user via the climate entity / card (temperature slider),
  2. selecting a **preset**,
  3. a **physical change on a TRV** (optional per TRV, see §5),
  4. a **schedule event** (see §3).
* **Suspensions (general rule).** Three conditions can suspend normal control of
  a zone: an **open window**, **summer mode** and **vacation mode**. While
  suspended, the TRVs are driven by the suspension (window-open temperature or
  HVAC off / frost protection / vacation temperature), **but everything else
  keeps being registered**: target changes from any source and schedule events
  update the desired target, and the pre-heat plan keeps being computed. When
  the suspension ends, the zone simply continues with the then-current desired
  target (and pre-heat, if its start time has already passed and the event has
  not fired yet). There is no special restore/resume logic per case; this is one
  mechanism. If several suspensions overlap, the one with the highest priority
  drives the TRVs (summer > vacation > window); the others are still just
  registered state.
* **Presets** are named temperatures only: *Comfort, Eco, Night, Away, Vacation*
  (each configurable per zone). Selecting one sets the target once and nothing
  else. After a manual temperature change, no preset is highlighted.
* **Away** is a plain preset. Automatic presence-based switching is **[removed]**.
* The desired target is restored after a Home Assistant restart. Restarting does
  not re-apply schedule events.

---

## 3. Scheduling

* A zone's schedule is a list of **events**. An event means: *"at time T on
  these weekdays, set the target to X"* – nothing more. X is a number (4–35 °C)
  or a preset name (resolved to that preset's temperature when the event fires).
  There is no notion of a "currently active event" or time slots.
* Events fire **at their exact time** (not at the next control cycle).
* Events are skipped, not caught up, while scheduling is disabled/paused or HA
  was not running. Enabling scheduling (again) does not apply anything
  retroactively; the next event simply takes effect when it fires.
* While **vacation or summer** is active, events still fire but only update the
  zone's to-restore target (§7).
* Events are edited in `/config/thermosmart_schedules.yaml` (one block per zone,
  keyed by a slug of the zone name). The file is managed: missing zones are
  added with an empty list, keys are kept sorted, existing blocks and orphaned
  blocks are never deleted or modified. Duplicate day/time: last entry wins.
  A config-flow event editor is a **[deferred]** nice-to-have.
* Per event flag **`preheat`** (see §4).
* Controls per zone:
  * **Scheduling enabled** switch (persistent on/off, takes precedence),
  * **Scheduling disabled until** date/time entity (temporary pause).
* Debug/info entities: **Next scheduled event** and **Pre-heat start** timestamps
  (with attributes such as event target, minutes until, pre-heat state and reason
  why pre-heating is not active).

---

## 4. Pre-heating

* Per event (`preheat: true`): the zone starts heating early so that the event's
  target is reached *at* the event time, instead of only starting to heat then.
* Applies to any event whose target is higher than the current target
  (numeric or preset).
* The lead time is estimated from learned heat-up behaviour once enough data
  exists, otherwise from a deterministic default; it is capped (180 min) and
  nothing happens if the temperature gap is small.
* Exposed: *Pre-heat start* sensor (when; the duration is an attribute and also
  available as the diagnostic *Pre-heat minutes* sensor), zone status
  `preheating`.
* Only the **immediately next** event is considered. If its pre-heat would start
  before the previous event, it starts right after the previous event instead –
  never earlier.
* Pre-heating needs scheduling to be active. During a suspension (§2: window,
  summer, vacation) the zone does not heat towards the pre-heat target, but the
  plan is kept: if the suspension ends while the event is still ahead and the
  planned start has passed, pre-heating starts at that moment. If the event has
  fired in the meantime, there is nothing to pre-heat and the event's target
  simply applies.
* Interruptions:
  * **Window opens**: see above – pre-heat is suspended and resumes on close.
  * **Any manual target change during pre-heat** (climate entity/card, preset,
    physical TRV dial) cancels pre-heating for that event; it is not resumed.
    The displayed target therefore always equals what the zone heats towards.
  * The event itself always still fires on time and sets its target.

---

## 5. Control of the TRVs

### 5.1 Zone-level options (set in setup and options flow alike)
* **Zone control mode** (default for all TRVs in the zone):
  * *Setpoint only* – the plain target is sent; the TRV regulates itself.
  * *TPI setpoint modulation* – ThermoSmart computes heating demand and raises or
    lowers the setpoint sent to the TRV (boost, up to +8 °C).
  * *Direct valve control* – valve opening (0–100 %) is written to the TRV's
    valve entity and the plain target is sent as setpoint.
* **Minimum control cycle interval** (default 5 min).
* **Temperature tolerance** (a new setpoint is written only if it differs from the
  TRV's by at least this much).
* **External temperature write threshold**.
* **Physical setpoint debounce** and **settling time** (both default 5 s).

### 5.2 Per-TRV options
Control mode override (or inherit), valve entity, feed room temperature to the
TRV (+ entity), calibration (entity, on/off, invert), valve exercise/maintenance,
**window: turn off** (HVAC off instead of the window-open temperature),
**accept physical setpoint changes**, and **state settings**: any select/number/
switch entity of the TRV that ThermoSmart keeps at a chosen value (e.g. child
lock off); written only when it differs. Known devices get these pre-filled from
a **device preset**; detection happens only in the flow, at runtime exactly the
configured values are used (a cleared field turns the feature off). TRVs in one
zone may use different strategies.

### 5.3 Physical changes on a TRV
If enabled for that TRV, the last reported setpoint (after debounce) becomes the
zone target and is applied to all TRVs (last writer wins; also accepted while a
window is open). Echoes of ThermoSmart's own writes are ignored for the settling
time. If disabled for that TRV, the change is corrected back. During
**vacation/summer** holds, physical changes are ignored, corrected back and
logged.

### 5.4 When a control cycle runs
* periodically (configurable interval), and additionally on:
  * target change from any source,
  * schedule event and pre-heat start (exact timers),
  * window state change (after the configured open/close delay),
  * room temperature moved ≥ the threshold since the last cycle,
  * physical TRV setpoint change (after debounce),
  * Active Control / Learning / Scheduling / Summer / Vacation switch change,
  * options reload.
* Nothing else triggers a cycle.

### 5.5 Windows
* Window/door sensors with separate open and close delays; without sensors, a
  falling-temperature (slope) detection is used.
* While open: each TRV is either set to the **window-open temperature**
  (default 5 °C) or switched to HVAC `off` (per TRV; only for TRVs with frost
  protection while off). On close: back to `heat` and the target.
* Pre-heating pauses, early cutoff is released.

### 5.6 Active Control and Learning switches (unchanged from main)
* Learning on + Active Control off → *observation mode*: no writes to TRVs.
* Learning off + Active Control on → deterministic TPI control.
* Both on → control with learned parameters (pre-heat time, TPI coefficient, early
  cutoff, adaptive boost); both off → zone inactive.
* The climate entity's HVAC mode `heat`/`off` maps to Active Control on/off.

### 5.7 Safety / robustness
Missing room sensors fall back to the TRV's own sensor; spikes are filtered;
sensors going offline are averaged out; heating-failure detection (commanded
heating but temperature falls ≥ 35 min); valve maintenance during summer/
vacation; the device profile system (SONOFF TRVZB, Eurotronic, Avatto, Eve, …)
with a generic fallback.

---

## 6. Outdoor data, learning and control

**Outdoor data (zone level).** Each zone has one consistent view of the *current*
outdoor conditions: temperature, humidity, wind speed, solar radiation and
precipitation. Every value comes from the zone's own outdoor sensor if one is
configured and available; otherwise from the *current conditions* of the zone's
weather entity (if configured and it provides that value); otherwise the value is
simply unavailable. Weather **forecasts** are not used anywhere.

**Everything must work without outdoor data.** If neither sensors nor a weather
entity provide a value, learning and control continue with their outdoor-
independent behaviour (as `main` does without outdoor sensors: e.g. the learned
heat-loss model runs "relative only" and the outdoor part of the TPI calculation
is neutral). Missing outdoor data is never an error and never blocks heating.

**Where outdoor data is used:**
* **Learning** (heat loss, heat-up rate, pre-heat duration, TPI coefficients, …).
* **Control** (the outdoor-dependent part of the TPI calculation and the
  pre-heat duration estimate).
* It is **not** used to change the target: weather offsets and forecast
  suppression are removed (§10).

(Summer mode uses the System entry's own outdoor source, §7.)

**Learning** is otherwise retained as a self-contained component with the same
user-facing behaviour as in `main`: it learns heat-up rate, heat loss, onset
delay, boost and after-heat behaviour and exposes *Confidence* and diagnostic
sensors, diagnostics download and local-only storage.

* Used for control: pre-heat duration, TPI coefficient, adaptive boost, early
  cutoff.
* **Early cutoff / coasting** (**[deferred]**, to be restored after the core): when
  the zone is heating up to a target that was *raised* (from any source), heating
  may stop slightly before the target and let after-heat finish. Released on
  window open and when the target changes again.

---

## 7. Summer and Vacation (ThermoSmart System entry)

Set-up of the System entry asks for an optional **weather entity** and/or
**outdoor temperature sensor**. Entities:

| Entity | Meaning |
|---|---|
| Vacation Mode (toggle) | Suspends normal control of all zones; TRVs hold the zone's vacation temperature. |
| Force Summer Mode (toggle) | Summer mode on, irrespective of weather. |
| Auto-enable Summer Mode (toggle) | Summer mode is switched on/off automatically from the outdoor temperature (72 h average) **if an outdoor temperature is available** (sensor or weather entity). |

* **Summer** and **Vacation** are suspensions as described in §2: TRVs are held at
  frost protection (12 °C) in summer (valves are exercised periodically) or at the
  vacation temperature in vacation. Meanwhile, schedule events and manual target
  changes are registered as the desired target, and no pre-heating is performed.
  **Summer takes precedence over Vacation** when both are active. When the last
  suspension ends, the zone continues with its desired target.
* **Thresholds for automatic summer mode are configurable** in the System setup
  and options flow (defaults 18 °C on / 15 °C off).
* The former indoor safety bypass is **[removed]**; the global debug-log switch
  is **[removed]** (use Home Assistant's built-in debug logging).

---

## 8. Set-up and options (config flow)

The setup flow and the options flow offer **the same steps and fields**
(options that only make sense in one are marked in the UI).

**System entry:** weather entity, outdoor temperature sensor, summer thresholds.

**Zone entry:**
1. **Devices & sensors** – zone name (shows the schedule key), TRVs, temperature
   and humidity sensors, window sensors with open/close delays.
2. **Control strategy** – §5.1, plus detected devices overview.
3. **Per TRV** – §5.2 including state settings.
4. **Temperatures** – presets (Comfort, Eco, Night, Away, Vacation) and window
   open temperature.
5. **Weather** – optional zone outdoor sensors (temperature, humidity, wind, solar,
   rain) and an optional weather entity as fallback (§6); used by learning and
   control, not for changing the target.

Removed steps/fields: presence step, weekday/weekend time fields, schedule
enable toggle, tolerance moved to control strategy, calibration invert moved
per TRV.

---

## 9. Entities

**Per zone:** climate entity (HVAC heat/off, presets, target) · Preset select ·
Active Control switch · Learning switch · Scheduling enabled switch · Scheduling
disabled-until datetime · Status sensor (`observation_mode`, `heating`, `idle`,
`preheating`, `window_open`, `vacation`, `summer`, `heating_failure`, `disabled`)
· Confidence · Next scheduled event · Pre-heat start (with the duration as an
attribute) · diagnostic sensors (disabled by default): *Pre-heat minutes*,
*TPI boosted TRV setpoint* (the setpoint actually sent to the TRVs in TPI mode),
TPI duty cycle, temperature slope, heat loss, heating power, outcome score, solar
gain, TRV observations, window cooling rate, EMA temperature.
The status sensor carries attributes for the active suspension and the
temperature the TRVs are held at (§2).
(*Adjusted target* – it duplicated the climate entity's target –, the `away`
status and the *Weather offset* sensor are removed.)

**Global:** Vacation Mode · Force Summer Mode · Auto-enable Summer Mode.

**Card:** updating the bundled Lovelace card is **[deferred]** (§11).

**Languages:** only English is maintained for now; other languages are
**[deferred]**.

---

## 10. Out of scope / removed for now

Auto mode and any "override" concept · weekday/weekend schedule fields and
migration from them · presence-based Away · weather offsets · forecast
suppression (forecasts are not used at all) · automatic summer safety
bypass · debug switch · backwards compatibility with existing config entries.

---

## 11. Scope of the first milestone and known leftovers

**In scope now:** control logic, test coverage, README (rewritten for clarity and
completeness; structure of the old README need not be kept).

**[deferred]:**
* Translations other than English (new/changed strings exist in English only;
  other language files are left as they are and fall back to English; the
  translation-completeness test is adjusted accordingly).
* Updating the Lovelace card (it keeps working as far as possible; the Auto
  button is not removed yet, so it will be a no-op/hidden at most).
* Early cutoff / coasting, config-flow schedule editor.
* Re-adding weather-based correction (see below).

**Physical dial changes during a suspension:** registered as the desired target
(applied when the suspension ends) and the TRV is corrected back to the held
temperature immediately, with a log entry. (Window: the TRV stays in its
window-open state.)

**Display during a suspension:** the climate entity's target shows the desired
target. The status sensor says why the TRVs differ (`window_open`, `summer`,
`vacation`) and an attribute shows the temperature the TRVs are held at.

**Known leftover (to be documented in the README as a reminder):** the learning
package still contains the forecast trust/bias models. Nothing feeds or uses
them any more. They must either be removed or be reconnected if weather-based
correction is re-introduced.
