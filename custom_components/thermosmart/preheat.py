"""Pure pre-heat planner (no Home Assistant dependency).

Pre-heating starts the zone early so that the *next* schedule event's target is
reached at the event time. The planner only decides *when*; whether the TRVs
may actually heat towards the pre-heat target is decided by the suspension
logic in the coordinator.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Plan states
NONE = "none"          # nothing to pre-heat (see ``reason``)
PLANNED = "planned"    # start time lies ahead
ACTIVE = "active"      # start time reached (or already started), event still ahead

# Reasons why there is no plan
NO_EVENT = "no_event"
SCHEDULING_INACTIVE = "scheduling_inactive"
EVENT_NO_PREHEAT = "event_without_preheat"
TARGET_NOT_HIGHER = "target_not_higher"
NO_TEMPERATURE = "no_room_temperature"
TARGET_REACHED = "target_reached"
CANCELLED = "cancelled_by_manual_change"


@dataclass(frozen=True, slots=True)
class PreheatPlan:
    state: str
    reason: str | None = None
    start: datetime | None = None
    duration_min: float = 0.0
    event_at: datetime | None = None
    event_target: float | None = None


def plan(
    *,
    now: datetime,
    scheduling_active: bool,
    event_at: datetime | None,
    event_target: float | None,
    event_preheat: bool,
    desired_target: float,
    previous_event_at: datetime | None,
    duration_min: float,
    duration_status: str | None,
    cancelled: bool,
    started_at: datetime | None,
) -> PreheatPlan:
    """Plan pre-heating for the immediately next event.

    ``started_at`` latches an already running pre-heat (its start time) so that
    a shrinking duration estimate (the room warms up) can never end it early.
    """
    def none(reason: str) -> PreheatPlan:
        return PreheatPlan(NONE, reason, event_at=event_at, event_target=event_target)

    if event_at is None or event_target is None:
        return none(NO_EVENT)
    if not scheduling_active:
        return none(SCHEDULING_INACTIVE)
    if not event_preheat:
        return none(EVENT_NO_PREHEAT)
    if cancelled:
        return none(CANCELLED)
    if event_target <= desired_target:
        return none(TARGET_NOT_HIGHER)
    if started_at is None:
        if duration_status == "unavailable":
            return none(NO_TEMPERATURE)
        if duration_min <= 0:
            return none(TARGET_REACHED)

    if started_at is not None:
        start = started_at
    else:
        start = event_at - timedelta(minutes=duration_min)
        if previous_event_at is not None and start < previous_event_at:
            start = previous_event_at  # never earlier than the previous event
    state = ACTIVE if (started_at is not None or now >= start) else PLANNED
    return PreheatPlan(
        state, None, start, float(duration_min), event_at, event_target
    )
