<p align="center">
  <img src="custom_components/thermosmart/brand/logo.png" alt="ThermoSmart" width="256"/>
</p>

<h1 align="center">ThermoSmart</h1>
<p align="center"><strong>Self-learning, schedule-driven heating control for Home Assistant TRVs</strong></p>

<p align="center">
  <a href="https://hacs.xyz"><img src="https://img.shields.io/badge/HACS-Custom-orange.svg" alt="HACS Custom"/></a>
  <a href="https://www.home-assistant.io"><img src="https://img.shields.io/badge/HA-2024.12%2B-brightgreen.svg" alt="HA min"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"/></a>
</p>

> ⚠️ **Use at your own risk.** ThermoSmart controls physical heating devices and is not affiliated with Home Assistant or Nabu Casa. Set a safe minimum temperature and verify the behaviour in observation mode before you let it control anything.

ThermoSmart controls the thermostatic radiator valves (TRVs) of a **heating zone**. One config entry is one zone (a room or a group of rooms). Everything is local: no cloud, no subscription.

This is a fork that reworks control and scheduling. If you know the original project, read [What is different from the original](#what-is-different-from-the-original) first.

---

## Contents

1. [Concepts](#concepts)
2. [Installation and setup](#installation-and-setup)
3. [Scheduling](#scheduling)
4. [Pre-heating](#pre-heating)
5. [Window, summer and vacation (suspensions)](#window-summer-and-vacation-suspensions)
6. [How the TRVs are controlled](#how-the-trvs-are-controlled)
7. [Outdoor data and learning](#outdoor-data-and-learning)
8. [Entities](#entities)
9. [Device compatibility](#device-compatibility)
10. [FAQ](#faq) · [Diagnostics](#diagnostics)
11. [Known leftovers and deferred work](#known-leftovers-and-deferred-work)

---

## Concepts

### One target temperature

A zone has exactly **one desired target temperature**. It is what the climate entity shows. Anything can change it, and **the most recent change wins**:

| Source | Example |
|---|---|
| You | Moving the temperature slider of the climate entity |
| A preset | Selecting *Comfort*, *Eco*, *Night*, *Away* or *Vacation* |
| A TRV dial | Turning the knob on a TRV (if enabled for that TRV) |
| A schedule event | "Mon–Fri 06:30 → 21 °C" |

There is no *Auto* mode and no separate "override" state. A schedule event does not "take over" or "end" anything; it just sets the target at its time, like you would by hand. After a manual temperature change no preset is highlighted.

Things ThermoSmart does internally (for example the boost of TPI control) never change the desired target.

The desired target survives Home Assistant restarts. A restart does not replay schedule events.

### Presets

Presets are named temperatures: **Comfort, Eco, Night, Away, Vacation**, each configurable per zone. Selecting a preset sets the target once and nothing else. *Away* is a plain preset; there is no automatic presence detection.

### Suspensions

Three conditions can *suspend* normal control: an **open window**, **summer mode** and **vacation mode**. While suspended, the TRVs are driven by the suspension, but **everything else keeps being registered**: target changes and schedule events still update the desired target, and the pre-heat plan is still computed. When the suspension ends, the zone simply continues with the then-current desired target. See [Window, summer and vacation](#window-summer-and-vacation-suspensions).

### Active Control, Learning and observation mode

Two independent switches per zone:

| Active Control | Learning | Result |
|---|---|---|
| on | off | Deterministic control (TPI), no learned parameters |
| on | on | Control with learned parameters (pre-heat time, TPI coefficients, boost) |
| off | on | **Observation mode**: nothing is written to your TRVs (including the room-temperature feed), ThermoSmart only watches and learns. The target, schedule events and pre-heat plan are still tracked. |
| off | off | Zone inactive |

The climate entity's HVAC mode `heat`/`off` maps to Active Control on/off. Start with observation mode if you want to verify sensors and settings first.

---

## Installation and setup

1. HACS → Integrations → **⋮** → **Custom repositories** → add this repository as category *Integration*, then download **ThermoSmart**.
2. Restart Home Assistant, then **Settings → Devices & services → Add Integration → ThermoSmart**.
3. Add the **ThermoSmart System** entry first (needed for vacation and summer mode), then one entry per heating zone.

### System entry

Optional **weather entity** and/or **outdoor temperature sensor** (used only for automatic summer mode), and the automatic-summer thresholds (default: on above 18 °C, off below 15 °C).

### Zone entry

The setup flow and the options flow ask **the same questions with the same texts**. The options flow additionally starts with a menu so you can change one section at a time; the only other difference is that setup pre-fills the outdoor section with the system's weather entity. Steps:

| Step | What you configure |
|---|---|
| **Devices** | Zone name, TRVs, room temperature and humidity sensors (optional – the TRV's own sensor is the fallback), window/door sensors with open and close delay |
| **Control strategy** | Zone control mode, minimum control interval, temperature tolerance, external temperature write threshold, physical-setpoint debounce and settling time |
| **Per TRV** | See [Per-TRV options](#per-trv-options); one pass per TRV |
| **Temperatures** | Comfort, Eco, Night, Away, Vacation and the window-open temperature |
| **Schedule** | Add, edit and remove weekly events |
| **Outdoor** | Optional outdoor sensors (temperature, humidity, wind, solar, rain) and an optional weather entity as fallback |

Changes in the options flow reload the zone and run one control pass.

---

## Scheduling

A schedule is a list of **events**. An event means: *at time T on these weekdays, set the target to X* – nothing else. X is a temperature (5–30 °C) or a preset name (resolved to that preset's temperature when the event fires). There are no time slots and no "active event".

Events fire **at their exact time**, not at the next control cycle. An event that was missed because scheduling was off, paused, or Home Assistant was not running is **skipped, never caught up**.

Edit the event list in the zone's setup flow or under **Settings → Devices & services → ThermoSmart zone → Configure → Schedule**. Each event has one or more weekdays, a local time, a target temperature or preset, and an optional pre-heat flag. Events with an overlapping weekday and time are rejected rather than silently overriding each other. The schedule is stored with that zone's Home Assistant config entry; editing it does not require a separate YAML file. Existing `thermosmart_schedules.yaml` files are not imported; recreate those events in the flow.

Controls per zone:

* **Scheduling enabled** (switch): persistent on/off.
* **Scheduling disabled until** (date/time): temporary pause; the scheduler continues afterwards, without catching up.
* **Next scheduled event** (sensor): when it is, and its target.

While vacation or summer mode is active, events still fire but only update the desired target that will apply when the suspension ends.

---

## Pre-heating

An event with `preheat: true` makes the zone start heating early so that the event's target is reached **at** the event time instead of merely starting to heat then. It applies when the event's target is higher than the current target (numeric or preset).

* The lead time comes from learned heat-up behaviour once there is enough data, otherwise from a deterministic default (about 1.5 °C per hour plus a 5 minute onset), capped at 180 minutes. No pre-heating if the gap is small.
* Only the **next** event is considered. If its pre-heat would begin before the previous event, it begins right after the previous event instead.
* Once started, pre-heating continues until the event fires (a shrinking estimate cannot end it early).
* The **Pre-heat start** sensor shows when it starts (attributes: state, reason, duration and where it came from). The status becomes `preheating`.
* **A manual target change while pre-heating cancels it** for that event (climate entity, preset, TRV dial). The event itself still fires on time. A manual change *before* the planned start does not cancel the plan.
* **Suspensions:** pre-heating is not performed while a window is open or summer/vacation is on, but the plan is kept. If the suspension ends after the planned start and before the event, pre-heating starts at that moment. If the event has fired in the meantime, its target simply applies.

---

## Window, summer and vacation (suspensions)

| Suspension | TRVs are held at | Ends when |
|---|---|---|
| **Summer** | Frost protection (12 °C); valves are exercised periodically | Summer mode is switched off (or automatic summer ends) |
| **Vacation** | The zone's *Vacation* temperature | Vacation mode is switched off |
| **Window open** | Per TRV: the window-open temperature (default 5 °C), or HVAC `off` | The window is closed (after the close delay) |

If several apply, the highest priority drives the TRVs: **summer > vacation > window**. The others are registered state only.

During any suspension:

| What happens | Behaviour |
|---|---|
| Schedule events | Fire normally and update the desired target |
| You change the target (slider, preset) | Registered as the desired target, applied when the suspension ends |
| TRV dial turned | Registered as the desired target, the TRV is corrected back to the held temperature immediately and the correction is logged (while a window is open the TRV stays in its window state) |
| Pre-heating | Not performed; the plan is kept (see above) |
| Climate entity | Keeps showing the desired target |
| Status sensor | `window_open`, `vacation` or `summer`, with attributes for the active suspension and the temperature held |

There is one mechanism, not one restore routine per case: when the last suspension ends, the zone continues with its current desired target.

**Windows.** Open/close are detected from window/door sensors with separate open and close delays; without sensors, a falling-temperature (slope) detection is used. Per TRV, *Turn off when window is open* switches HVAC to `off` instead of using the window-open temperature (only enable for TRVs with frost protection while off).

**Vacation and summer** are controlled by the ThermoSmart System entry (required):

| Entity | Meaning |
|---|---|
| Vacation Mode | All zones hold their vacation temperature |
| Force Summer Mode | Summer on, irrespective of the weather |
| Auto-enable Summer Mode | Switches summer on/off from the outdoor temperature (72 h average, on/off thresholds from the System entry). Needs an outdoor temperature (sensor, else weather entity) and a full 72 h of samples before it first decides (history is not persisted, so after a restart the previous state is kept until 72 h have been collected again; the switch's `sample_hours` attribute shows how much exists). |

---

## How the TRVs are controlled

### Zone control mode (default for all TRVs of the zone)

| Mode | What is sent |
|---|---|
| **Setpoint only** | The plain target; the TRV regulates itself |
| **TPI setpoint modulation** | ThermoSmart computes the heating demand and raises or lowers the setpoint sent to the TRV (boost up to +8 °C). The setpoint actually sent is shown by the *TPI boosted TRV setpoint* sensor; the climate entity still shows the desired target |
| **Direct valve control** | The calculated valve opening (0–100 %) is written to the TRV's valve entity; the plain target is sent as setpoint |

Other zone options: minimum control interval (default 5 min), temperature tolerance (a new setpoint is only written if it differs from the TRV's by this much), external temperature write threshold, physical-setpoint debounce and settling time (default 5 s each).

### Per-TRV options

Control mode override (or inherit), valve entity, feeding the room temperature to the TRV (+ entity), calibration (entity, on/off, invert), valve exercise, *Turn off when window is open*, *accept physical setpoint changes* and **state settings**: any `select`/`number`/`switch` entity of the TRV that ThermoSmart keeps at a value you choose (e.g. child lock off); a service is only called if the value differs. Known devices get these fields pre-filled from a device preset. Detection happens only in the flow; at runtime exactly the configured values are used, and a cleared field turns the feature off. TRVs in one zone may use different strategies.

### Physical changes on a TRV

If enabled for that TRV, the TRV's reported setpoint (after the debounce) becomes the zone target and is applied to all TRVs, like any other manual change. Echoes of ThermoSmart's own writes are ignored during the settling time. If disabled for that TRV, the change is corrected back.

### When does a control pass run?

Periodically (every control interval), and **only** for the reasons below. Anything else (TRV attribute noise, humidity changes, small room-temperature drift) does not cause a pass. Bursts are merged into one pass. The last pass's reasons are in the `pass_triggers` attribute of the status sensor.

| Trigger | Cause |
|---|---|
| `periodic` | The control interval elapsed |
| `target_change` | You changed the target or preset |
| `schedule_event` | A schedule event fired (exact timer) |
| `preheat_start` | The planned pre-heat start was reached (exact timer) |
| `window` | A window open/close delay expired |
| `room_temperature` | The room temperature moved by at least the refresh threshold |
| `physical_setpoint` | A physical TRV change was adopted or must be corrected |
| `switch` | Active Control or Learning was switched |
| `scheduling` | Scheduling was switched or paused/resumed |
| `system_state` | The System entry changed summer or vacation |
| `startup` | The zone was (re)loaded, including options changes |

### Robustness

Missing room sensors fall back to the TRV's own sensor; spikes are filtered; offline sensors are averaged out (control pauses only if all are unavailable); heating-failure detection (commanded heating, but the temperature falls for 35+ minutes); valve maintenance in summer and vacation; device profiles with a generic fallback.

---

## Outdoor data and learning

**Outdoor data.** Each zone has one view of the *current* outdoor conditions: temperature, humidity, wind, solar radiation and precipitation. Each value comes from the zone's own outdoor sensor if configured and available, otherwise from the *current conditions* of the zone's weather entity, otherwise it is unavailable. Forecasts are not used.

**Everything works without outdoor data.** Missing values are never an error and never block heating: learning and control simply continue with their outdoor-independent behaviour.

Outdoor data is used by **learning** and by **control** (the outdoor part of the TPI calculation and the pre-heat estimate). It is **not** used to change the target.

**Learning** observes heat-up rate, heat loss, onset delay, boost and after-heat behaviour of the zone and exposes *Learning progress* and diagnostic sensors. Data is stored locally. Learned values are used for the pre-heat duration, TPI coefficients and adaptive boost.

---

## Entities

### Per zone

| Entity | Description |
|---|---|
| `climate.*` | Virtual thermostat: target, current temperature, HVAC `heat`/`off`, presets |
| `select.*_preset` | Preset selection |
| `switch.*_active_control` / `*_learning` | Active Control and Learning |
| `switch.*_scheduling_enabled` | Scheduling on/off |
| `datetime.*_scheduling_disabled_until` | Temporary scheduling pause |
| `sensor.*_status` | `observation_mode`, `heating`, `idle`, `preheating`, `window_open`, `vacation`, `summer`, `heating_failure`, `disabled` (+ attributes) |
| `sensor.*_learning_progress` | 0–100 % |
| `sensor.*_next_scheduled_event` | Time of the next event (attributes: target, minutes until) |
| `sensor.*_pre_heat_start` | Planned pre-heat start (attributes: state, reason, duration) |

Diagnostic sensors (disabled by default): Pre-heat minutes · TPI boosted TRV setpoint · TPI duty cycle · Temperature slope · Heat loss · Heating power · Outcome score · Solar heat gain · TRV observations · Window cooling rate · Temperature EMA 1h.

### System entry

`switch.*_vacation_mode` · `switch.*_force_summer_mode` · `switch.*_auto_summer_mode`

---

## Device Compatibility

ThermoSmart includes a Device Profile system that enables correct behaviour across a wide range of TRVs out of the box. Each profile encodes the device's `target_temp_step`, temperature limits, and any quirks — so ThermoSmart can write precise setpoints without manual calibration.

For devices without a specific profile, ThermoSmart falls back to a generic profile that works with any `climate` entity supporting `set_temperature`.

### Confirmed working

Real-world tested with active ThermoSmart control:

| Device | Protocol | Notes |
|---|---|---|
| SONOFF TRVZB | Zigbee (Z2M) | Fully tested — external temperature, valve control, motor-protection |
| Eve Thermo (SEA80x) | HomeKit | Fully tested — observation mode, read-only valve sensor |

**Using a different TRV?** Open a [GitHub Discussion](https://github.com/Mikasmarthome/ThermoSmart/discussions) with your device model and protocol — real-world feedback is the fastest path to a dedicated profile.

### Actively supported — profile-specific behavior

These devices have a named profile with device-specific handling applied automatically.

| Device | Protocol | Profile-specific behavior |
|---|---|---|
| SONOFF TRVZB | Zigbee (Z2M) | `valve_opening_degree` treated as max-opening limit (not written). Room temperature pushed to `external_temperature_input` for improved TRV-internal accuracy. Motor-protection on valve close. HVAC watchdog active. |
| Eurotronic Spirit | Zigbee (Z2M / ZHA) | HVAC mode written to `heat` before each setpoint — required for the TRV to accept external setpoints reliably. |
| Eurotronic SPZB0001 | Zigbee (Z2M / ZHA) | Same HVAC-first setpoint workflow as Eurotronic Spirit. |
| Avatto ME167 | Zigbee (Z2M) | Calibration offset sign is inverted to match this device's firmware convention. |
| Eve Thermo (HomeKit / SEA80x) | HomeKit | Valve position exposed as read-only sensor — ThermoSmart reads it for learning but does not write to it. External temperature and calibration writes are disabled. |
| Beca BHT-002 | Zigbee (Z2M) | Calibration plausibility limit tightened to ±3 °C to account for known firmware offset behaviour on some revisions. |

### Prepared profiles — standard behavior

These devices have a named profile. Valve, calibration, and quirk entities are detected once when you set up the TRV and pre-filled in its settings. No device-specific overrides apply beyond what the hardware exposes. Dedicated profiles allow future device-specific optimizations and quirks handling while remaining compatible with generic climate devices.

| Device | Protocol |
|---|---|
| Tuya TS0601 | Zigbee (Z2M) |
| Moes TV02 | Zigbee (Z2M) |
| Aqara TRV (SRTS-A01) | Zigbee (Z2M / ZHA) |
| Danfoss Ally | Zigbee (Z2M / ZHA) |
| Bosch BTH-RA | Zigbee (Z2M / ZHA) |
| Innr COZB001 | Zigbee (Z2M) |

### Warning profiles — observation only recommended

ThermoSmart can observe these devices and record learning data, but active setpoint control is not recommended. Cloud-based devices periodically override setpoints from external sources — ThermoSmart would learn the cloud's behaviour rather than your building's.

| Device | Reason |
|---|---|
| tado° | Relies on the tado° cloud API. External setpoints are routinely overridden by the cloud schedule. |
| Daikin Climate | Heat pump / multi-split system. ThermoSmart's TPI control targets single-zone radiator heating and is not designed for this device class. |
| Netatmo Smart Radiator Valve | Cloud connectivity required for primary control. Local entity availability varies by setup. |

### Generic fallback

Any `climate` entity supporting `set_temperature` that does not match a named profile receives the generic fallback profile. Setpoint boost and all detected features (valve, calibration) are pre-filled in the TRV settings during setup.

| Profile | Applies to |
|---|---|
| Generic TRV | All unrecognised `climate` entities |
| Generic Zigbee TRV | Prepared — platform detection planned |
| Generic HomeKit TRV | Prepared — platform detection planned |

### Protocol compatibility

| Protocol | Obs. Mode | Active Control | Direct Valve | Auto-Calibration |
|---|---|---|---|---|
| **Zigbee2MQTT** | ✅ | ✅ | ✅ auto-detected¹ | ✅ auto-detected¹ |
| **ZHA** | ✅ | ✅ setpoint boost | ⚠️ device-dependent | ⚠️ device-dependent |
| **Z-Wave JS** | ✅ | ✅ setpoint boost | ❌ | ⚠️ rarely exposed |
| **Matter** | ✅ | ✅ setpoint boost | ❌ | ❌ |
| **Homematic IP** (local) | ✅ | ✅ setpoint boost | ⚠️ `level` pattern, untested | ⚠️ `temperature_offset` |
| **Fritz!DECT / Bosch** | ✅ | ✅ setpoint boost | ❌ | ❌ |
| **Tado / Netatmo** | ⚠️ | ⚠️ not recommended | ❌ | ❌ |

¹ Auto-detection finds a matching entity and writes values to it — it does not guarantee physical function on every device.

**Direct valve control** auto-detects writable `number` entities matching: `pi_heating_demand`, `valve_position`, `heating_demand`, or `level`. Note: `valve_opening_degree` (used on some devices as a max-opening limit, not a live position) is intentionally excluded and not written by ThermoSmart.

**Setpoint boost** converts TPI duty-cycle to `setpoint = target + duty% × 8 °C` via `climate.set_temperature`. Works on all devices; less precise than direct valve control.

---

## FAQ

**What is observation mode?**  
Active Control off, Learning on: ThermoSmart reads what your existing controller does and learns from it, and writes nothing to your TRVs. Switch Active Control on when you are ready.

**How long until learning is effective?**  
Learning builds from actual heating cycles, so it depends on how often your heating runs. *Learning progress* (0–100 %) combines how much, how diverse and how clean the data is. Early on, built-in physical defaults carry more weight.

**A sensor goes offline – what happens?**  
The remaining sensors are averaged. Control pauses only if all room sensors of the zone are unavailable.

**Where is learning data stored?**  
Locally under `/config/.storage/`. Share the downloaded diagnostics instead of raw storage files.

**Which TRVs use direct valve control?**  
When you set up a TRV, a detected valve entity is pre-filled; with *Direct valve control* the calculated opening (0–100 %) is written to it. Recognised patterns: `valve_position` (Eurotronic Spirit), `pi_heating_demand` (Tuya TS0601, some Danfoss), `heating_demand`, `level` (Homematic IP). TRVs without one – including the **SONOFF TRVZB** – use setpoint modulation. On the TRVZB the room temperature is additionally written to `external_temperature_input`. `valve_opening_degree` is never written: on that device it is a maximum-opening limit.

**SONOFF TRVZB via Zigbee2MQTT – `heat` vs `auto`**  
In `heat` the TRV follows the setpoint written by ThermoSmart; in `auto` it follows its internal weekly schedule and ignores external setpoints. With Active Control on, every setpoint write switches the TRV to `heat`, so the internal schedule is bypassed (no need to delete it). In observation mode nothing is written, so the TRV stays in its last mode.

**Why did the TRVs not do what the climate entity shows?**  
A suspension is active (window, vacation, summer). The climate entity shows the *desired* target; check the status sensor and its attributes for the held temperature.

**Why did a control pass run / not run?**  
Look at `pass_triggers` on the status sensor (and `outdoor_sources` for where outdoor values came from) and the trigger table above.

---

## Diagnostics

### Temporary entity unavailability

After Home Assistant has fully started, ThermoSmart reports missing, `unknown`
or `unavailable` configured entities under **Settings → System → Repairs**.
Monitoring covers each zone's sensors, weather source and TRVs, the System
entry's outdoor sources, and enabled per-TRV valve-control, external-temperature
input, calibration and state-setting entities. Unused optional entities and
disabled device features are not monitored.

Each Repair identifies the affected zone/System entry, entity and its role. It
clears automatically when that entity becomes available again. Short outages
produce no warning in the log. After **three continuous minutes**, ThermoSmart
logs one warning per affected entity and entry; after recovery it logs one
informational message. Repeated unavailable/unknown updates do not restart that
timer. Removing or reconfiguring an entry cleans up its Repairs.

Monitoring does not delay control, send commands or change fallback behavior.
An available fallback does not hide a Repair for a configured primary source.
An entity becoming available does not prove its readings are valid or that writes
succeed. Write failures and heating-failure alarms retain their existing handling.

### Downloading diagnostics

**Settings → Devices & services → ThermoSmart →** pick an entry **→ ⋮ → Download diagnostics.** A zone's file contains analytics, learning progress, episode history, storage health and recent critical events. The System entry's file is a short overview.

The files contain numeric learning data, feature flags and timestamps. They do **not** contain credentials, entity IDs, device or person names, or locations. Nothing is sent anywhere; review the file before you share it.

For debug logs use Home Assistant's built-in logger configuration for `custom_components.thermosmart`.

---

## What is different from the original

* One desired target; no *Auto* mode, no override state, no presence-based *Away*, no weekday/weekend fields.
* Free-form schedule events configured in the zone flow, independent of presets and modes, with exact-time firing and optional per-event pre-heating.
* Window, summer and vacation share one suspension mechanism.
* Control strategy (zone and per-TRV), state settings and physical-setpoint handling are configurable; setup and options flows are one implementation.
* A fixed list of reasons for control passes.
* Outdoor data: sensors first, then the weather entity's current conditions; no forecasts.
* **Weather-based correction of the target (weather offset, forecast suppression) was removed.** Re-add or remove it deliberately later.
* Removed: *Adjusted target* sensor, *Weather offset* sensor, `away` status, the debug-log switch and the indoor summer bypass.
* There is no migration from earlier configurations; set zones up again.

---

## Known leftovers and deferred work

**Leftovers to resolve:**

* The `learning/` package still contains the forecast trust/bias models (e.g. `forecast_high` fields, notes in `legacy_map.py`). Nothing feeds or uses them. Remove them, or reconnect them if weather-based correction is re-introduced.
* Learning stores a `mode_context` per outcome (a former mode name); it is informational only.

**Deferred (wanted, not implemented):**

* Translations other than English: new strings exist in English only; other languages fall back to English and are not checked by the completeness test.
* The bundled Lovelace card has not been updated; it still shows an *Auto* button that no longer has an effect.
* Early cutoff / coasting (stop heating slightly early and let after-heat finish).
* Weather-based correction (see above).

---

## Contributing

**Bugs** and **feature requests**: GitHub Issues of this repository – include HA version, ThermoSmart version, what happened and log lines (`Settings → System → Logs → thermosmart`). **New TRV model?** Open a discussion with the model, protocol and log lines; that is the fastest way to a device profile.

Tests: `uv run python -m pytest -p no:cacheprovider --asyncio-mode=auto`.

## License

MIT – see [LICENSE](LICENSE)
