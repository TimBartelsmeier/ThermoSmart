"""The complete list of reasons a zone runs a control pass.

Every refresh of a zone goes through ``ThermoSmartCoordinator.request_pass`` with
one of these names; nothing else may call ``async_request_refresh``. Anything that
is not in this table (e.g. TRV attribute noise, humidity changes, indoor sensor
changes below the threshold) does **not** cause a pass.

Bursts of requests are coalesced by the coordinator's refresh debouncer: the first
request runs immediately, further requests inside the cooldown merge into one
trailing pass.
"""
from __future__ import annotations

TRIGGER_PERIODIC = "periodic"
TRIGGER_TARGET_CHANGE = "target_change"
TRIGGER_SCHEDULE_EVENT = "schedule_event"
TRIGGER_PREHEAT_START = "preheat_start"
TRIGGER_WINDOW = "window"
TRIGGER_ROOM_TEMPERATURE = "room_temperature"
TRIGGER_PHYSICAL_SETPOINT = "physical_setpoint"
TRIGGER_HVAC_MODE = "hvac_mode"
TRIGGER_SWITCH = "switch"
TRIGGER_SCHEDULING = "scheduling"
TRIGGER_SYSTEM_STATE = "system_state"
TRIGGER_STARTUP = "startup"

TRIGGERS: dict[str, str] = {
    TRIGGER_PERIODIC: "the regular control interval elapsed",
    TRIGGER_TARGET_CHANGE: "the user changed the target or preset (climate entity, select)",
    TRIGGER_SCHEDULE_EVENT: "a schedule event fired",
    TRIGGER_PREHEAT_START: "the planned pre-heat start time was reached",
    TRIGGER_WINDOW: "a window open/close delay expired",
    TRIGGER_ROOM_TEMPERATURE: "the room temperature moved by at least the refresh threshold",
    TRIGGER_PHYSICAL_SETPOINT: "a physical TRV setpoint change was adopted or must be corrected",
    TRIGGER_HVAC_MODE: "a controlled TRV entered an unwanted HVAC mode",
    TRIGGER_SWITCH: "Active Control or Learning was switched",
    TRIGGER_SCHEDULING: "scheduling was switched or paused/resumed",
    TRIGGER_SYSTEM_STATE: "the System entry changed summer or vacation",
    TRIGGER_STARTUP: "the zone was (re)loaded, including after an options change",
}
