"""Pre-heat timelines through the coordinator with an injected clock."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.thermosmart import coordinator as coord_mod
from custom_components.thermosmart import schedule as sched
from custom_components.thermosmart.learning.clock import FakeClock
from custom_components.thermosmart.schedule import parse_events
from custom_components.thermosmart.target import SOURCE_DIAL, SOURCE_PRESET, SOURCE_USER
from tests.helpers import make_coordinator, make_state, set_hass_states
from tests.test_coordinator_integration import _TestableCoordinator
from tests.test_schedule import _Timers

BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN)  # a Wednesday
EVENT_15 = [{"days": ["wed"], "time": "15:00", "target": 21, "preheat": True}]
# 19 -> 21 °C: (2 / 1.5 °C/h) * 60 + 5 min onset prior = 85 min
LEAD = timedelta(minutes=85)
EVENT_AT = datetime(2026, 5, 6, 15, 0, tzinfo=BERLIN)
PREHEAT_AT = EVENT_AT - LEAD


@pytest.fixture(autouse=True)
def berlin_timezone():
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(BERLIN)
    yield
    dt_util.set_default_time_zone(previous)


@pytest.fixture
def timers():
    schedule_timers, preheat_timers = _Timers(), _Timers()
    with patch.object(sched, "async_track_point_in_utc_time", schedule_timers), \
         patch.object(coord_mod, "async_track_point_in_utc_time", preheat_timers):
        yield SimpleTimers(schedule_timers, preheat_timers)


class SimpleTimers:
    def __init__(self, schedule, preheat):
        self.schedule, self.preheat = schedule, preheat


class Scenario:
    def __init__(self, events, *, temp=19.0, desired=18.0, overrides=None, start=START):
        self.clock = FakeClock(start)
        base = make_coordinator(overrides, clock=self.clock)
        with patch("homeassistant.helpers.frame.report_usage"):
            self.coord = _TestableCoordinator(
                base.hass, base.entry, base.outdoor, base.learning_engine,
                clock=self.clock,
            )
        self.coord.hass.async_create_task = lambda coro: coro.close()
        self.coord.async_update_listeners = MagicMock()
        self.coord.scheduler.set_events(parse_events(events))
        self.coord.start_scheduler()
        self.coord.set_target(desired, SOURCE_USER)
        self.coord._manual_change_pending = False
        self.temp = temp
        self.window = False
        self.coord._check_window_open = lambda cfg, current_temp=None: self.window

    def at(self, moment: datetime) -> None:
        self.clock.set_utc(moment)

    async def run(self) -> dict:
        set_hass_states(self.coord, {"sensor.test_temp": make_state(str(self.temp))})
        return (await self.coord._async_update_data())["zone"]

    def fire_event(self, timers) -> None:
        (timer,) = timers.schedule.live
        timer["cancelled"] = True
        self.clock.set_utc(timer["when"])
        timer["action"](timer["when"])


class TestLeadTime:
    async def test_planned_before_lead_time(self, timers):
        s = Scenario(EVENT_15)
        zone = await s.run()
        plan = s.coord.preheat_plan
        assert (plan.state, plan.start, plan.event_target) == ("planned", PREHEAT_AT, 21.0)
        assert zone["preheat_active"] is False
        assert zone["effective_target"] == 18.0
        assert zone["preheat_minutes"] == 85.0

    async def test_timer_armed_exactly_at_planned_start(self, timers):
        s = Scenario(EVENT_15)
        await s.run()
        (timer,) = timers.preheat.live
        assert timer["when"] == PREHEAT_AT.astimezone(timezone.utc)

    async def test_timer_not_rearmed_when_plan_unchanged(self, timers):
        s = Scenario(EVENT_15)
        await s.run()
        await s.run()
        assert len(timers.preheat.armed) == 1

    async def test_timer_moves_with_duration_estimate(self, timers):
        s = Scenario(EVENT_15)
        await s.run()
        (first,) = timers.preheat.live
        s.temp = 20.0  # warmer room (smoothed) -> shorter lead
        await s.run()
        (second,) = timers.preheat.live
        assert second["when"] > first["when"]
        assert second is not first and first["cancelled"]

    async def test_starts_at_planned_time(self, timers):
        s = Scenario(EVENT_15)
        await s.run()
        s.at(PREHEAT_AT)
        zone = await s.run()
        assert zone["preheat_active"] is True
        assert zone["effective_target"] == 21.0
        assert zone["desired_target"] == 18.0
        assert not timers.preheat.live

    async def test_one_minute_before_not_yet(self, timers):
        s = Scenario(EVENT_15)
        s.at(PREHEAT_AT - timedelta(minutes=1))
        zone = await s.run()
        assert zone["preheat_active"] is False

    async def test_estimate_shrinking_does_not_end_pre_heat(self, timers):
        s = Scenario(EVENT_15)
        s.at(PREHEAT_AT)
        await s.run()
        s.at(EVENT_AT - timedelta(minutes=30))
        s.temp = 20.9
        zone = await s.run()
        assert zone["preheat_active"] is True
        assert s.coord.preheat_plan.start == PREHEAT_AT

    async def test_event_fires_on_time_and_ends_pre_heat(self, timers):
        s = Scenario(EVENT_15)
        s.at(PREHEAT_AT)
        await s.run()
        s.fire_event(timers)
        zone = await s.run()
        assert s.coord.target.value == 21.0
        assert zone["preheat_active"] is False
        assert zone["effective_target"] == 21.0
        assert zone["desired_target_source"] == "event"


class TestEventKinds:
    async def test_preset_event(self, timers):
        s = Scenario(
            [{"days": ["wed"], "time": "15:00", "target": "comfort", "preheat": True}],
            overrides={"comfort_temp": 21.0},
        )
        s.at(PREHEAT_AT)
        zone = await s.run()
        assert zone["preheat_active"] and zone["effective_target"] == 21.0

    @pytest.mark.parametrize("target", [18.0, 15.0])
    async def test_no_pre_heat_for_equal_or_lower_target(self, timers, target):
        s = Scenario([{"days": ["wed"], "time": "15:00", "target": target, "preheat": True}])
        s.at(EVENT_AT - timedelta(minutes=5))
        zone = await s.run()
        assert zone["preheat_active"] is False
        assert s.coord.preheat_plan.reason == "target_not_higher"
        assert not timers.preheat.live

    async def test_no_pre_heat_without_flag(self, timers):
        s = Scenario([{"days": ["wed"], "time": "15:00", "target": 21}])
        s.at(EVENT_AT - timedelta(minutes=5))
        zone = await s.run()
        assert zone["preheat_active"] is False
        assert s.coord.preheat_plan.reason == "event_without_preheat"

    async def test_room_already_warm_means_no_pre_heat(self, timers):
        s = Scenario(EVENT_15, temp=21.0)
        zone = await s.run()
        assert zone["preheat_active"] is False
        assert s.coord.preheat_plan.reason == "target_reached"

    async def test_no_room_temperature_means_no_pre_heat(self, timers):
        s = Scenario(EVENT_15)
        set_hass_states(s.coord, {})
        s.coord._read_trv_avg_temp = lambda ids: None
        zone = (await s.coord._async_update_data())["zone"]
        assert zone["preheat_active"] is False
        assert s.coord.preheat_plan.reason == "no_room_temperature"

    async def test_only_the_next_event_is_considered(self, timers):
        s = Scenario([
            {"days": ["wed"], "time": "15:00", "target": 19, "preheat": True},
            {"days": ["wed"], "time": "18:00", "target": 23, "preheat": True},
        ], temp=17.0)
        s.at(datetime(2026, 5, 6, 14, 30, tzinfo=BERLIN))
        await s.run()
        assert s.coord.preheat_plan.event_target == 19.0

    async def test_start_not_before_previous_event(self, timers):
        s = Scenario([
            {"days": ["wed"], "time": "14:30", "target": 17},
            {"days": ["wed"], "time": "15:00", "target": 22, "preheat": True},
        ], temp=17.0, desired=17.0)
        s.fire_event(timers)  # the 14:30 event has fired; 15:00 is next
        s.at(datetime(2026, 5, 6, 14, 31, tzinfo=BERLIN))
        await s.run()
        plan = s.coord.preheat_plan
        assert plan.start == datetime(2026, 5, 6, 14, 30, tzinfo=BERLIN)
        assert plan.state == "active"


class TestSchedulingState:
    async def test_scheduling_disabled_means_no_pre_heat(self, timers):
        s = Scenario(EVENT_15)
        s.coord.set_scheduling_enabled(False)
        s.at(PREHEAT_AT)
        zone = await s.run()
        assert zone["preheat_active"] is False
        assert s.coord.preheat_plan.reason == "scheduling_inactive"

    async def test_scheduling_paused_during_pre_heat_ends_it(self, timers):
        s = Scenario(EVENT_15)
        s.at(PREHEAT_AT)
        assert (await s.run())["preheat_active"] is True
        s.coord.set_scheduling_disabled_until(EVENT_AT + timedelta(hours=1))
        zone = await s.run()
        assert zone["preheat_active"] is False
        assert zone["effective_target"] == 18.0

    async def test_disabled_until_expiring_before_start_keeps_plan(self, timers):
        s = Scenario(EVENT_15)
        s.coord.set_scheduling_disabled_until(PREHEAT_AT - timedelta(minutes=10))
        s.at(PREHEAT_AT)
        assert (await s.run())["preheat_active"] is True


class TestSuspensions:
    async def test_window_open_at_start_pre_heats_once_closed(self, timers):
        s = Scenario(EVENT_15)
        s.window = True
        s.at(PREHEAT_AT + timedelta(minutes=10))
        zone = await s.run()
        assert zone["preheat_active"] is False
        assert zone["effective_target"] is None  # setpoint released
        s.window = False
        s.at(PREHEAT_AT + timedelta(minutes=20))
        zone = await s.run()
        assert zone["preheat_active"] is True and zone["effective_target"] == 21.0

    async def test_window_opens_mid_pre_heat_and_closes_before_event(self, timers):
        s = Scenario(EVENT_15)
        s.at(PREHEAT_AT)
        await s.run()
        s.window = True
        s.at(PREHEAT_AT + timedelta(minutes=20))
        assert (await s.run())["preheat_active"] is False
        s.window = False
        s.at(PREHEAT_AT + timedelta(minutes=40))
        zone = await s.run()
        assert zone["preheat_active"] is True and zone["effective_target"] == 21.0

    async def test_window_closing_after_event_applies_event_target(self, timers):
        s = Scenario(EVENT_15)
        s.at(PREHEAT_AT)
        await s.run()
        s.window = True
        s.at(PREHEAT_AT + timedelta(minutes=20))
        await s.run()
        s.fire_event(timers)
        s.at(EVENT_AT + timedelta(minutes=10))
        assert (await s.run())["effective_target"] is None
        s.window = False
        zone = await s.run()
        assert zone["preheat_active"] is False
        assert zone["effective_target"] == 21.0

    @pytest.mark.parametrize("kind", ["vacation", "summer"])
    async def test_no_pre_heat_while_suspended_but_starts_on_resume(self, timers, kind):
        s = Scenario(EVENT_15)
        s.coord.set_system_state(vacation=kind == "vacation", summer=kind == "summer")
        s.at(PREHEAT_AT + timedelta(minutes=10))
        zone = await s.run()
        assert zone["preheat_active"] is False
        assert zone["suspension"] == kind
        assert s.coord.preheat_plan.state == "active"  # plan kept
        s.coord.set_system_state(vacation=False, summer=False)
        s.at(PREHEAT_AT + timedelta(minutes=30))
        zone = await s.run()
        assert zone["preheat_active"] is True and zone["effective_target"] == 21.0

    async def test_suspension_across_event_applies_event_target_on_resume(self, timers):
        s = Scenario(EVENT_15)
        s.coord.set_system_state(vacation=True, summer=False)
        s.at(PREHEAT_AT + timedelta(minutes=10))
        await s.run()
        s.fire_event(timers)
        s.coord.set_system_state(vacation=False, summer=False)
        zone = await s.run()
        assert zone["preheat_active"] is False
        assert zone["effective_target"] == 21.0


class TestManualChanges:
    @pytest.mark.parametrize("source", [SOURCE_USER, SOURCE_PRESET, SOURCE_DIAL])
    async def test_manual_change_during_pre_heat_cancels_for_this_event(self, timers, source):
        s = Scenario(EVENT_15)
        s.at(PREHEAT_AT)
        await s.run()
        s.coord.set_target(19.5, source)
        zone = await s.run()
        assert zone["preheat_active"] is False
        assert zone["effective_target"] == 19.5
        assert s.coord.preheat_plan.reason == "cancelled_by_manual_change"
        s.at(PREHEAT_AT + timedelta(minutes=30))
        assert (await s.run())["preheat_active"] is False  # not resumed

    async def test_event_still_fires_after_cancellation(self, timers):
        s = Scenario(EVENT_15)
        s.at(PREHEAT_AT)
        await s.run()
        s.coord.set_target(19.5, SOURCE_USER)
        await s.run()
        s.fire_event(timers)
        zone = await s.run()
        assert zone["effective_target"] == 21.0

    async def test_cancellation_is_per_event(self, timers):
        s = Scenario([
            {"days": ["wed"], "time": "15:00", "target": 21, "preheat": True},
            {"days": ["thu"], "time": "07:00", "target": 22, "preheat": True},
        ])
        s.at(PREHEAT_AT)
        await s.run()
        s.coord.set_target(19.5, SOURCE_USER)
        await s.run()
        s.fire_event(timers)
        s.at(datetime(2026, 5, 7, 5, 0, tzinfo=BERLIN))
        s.temp = 17.0
        await s.run()
        assert s.coord.preheat_plan.state == "active"
        assert s.coord.preheat_plan.event_target == 22.0

    async def test_manual_change_after_planned_start_but_before_the_pass_cancels(self, timers):
        s = Scenario(EVENT_15)
        await s.run()  # plan is PLANNED
        s.at(PREHEAT_AT)  # start time passed, the requested pass has not run yet
        assert s.coord.preheat_plan.state == "planned"
        s.coord.set_target(19.5, SOURCE_USER)
        zone = await s.run()
        assert zone["preheat_active"] is False
        assert zone["effective_target"] == 19.5
        assert s.coord.preheat_plan.reason == "cancelled_by_manual_change"

    async def test_manual_change_before_start_does_not_cancel(self, timers):
        s = Scenario(EVENT_15)
        await s.run()
        s.coord.set_target(18.5, SOURCE_USER)
        s.at(PREHEAT_AT)
        zone = await s.run()
        assert zone["preheat_active"] is True

    async def test_manual_change_to_event_target_or_higher_needs_no_pre_heat(self, timers):
        s = Scenario(EVENT_15)
        s.at(PREHEAT_AT)
        await s.run()
        s.coord.set_target(21.0, SOURCE_USER)
        zone = await s.run()
        assert zone["preheat_active"] is False
        assert zone["effective_target"] == 21.0

    async def test_dial_change_during_suspension_with_started_plan_cancels(self, timers):
        s = Scenario(EVENT_15)
        s.coord.set_system_state(vacation=True, summer=False)
        s.at(PREHEAT_AT + timedelta(minutes=10))
        await s.run()
        s.coord._active_control = True
        state = lambda v: MagicMock(state="heat", attributes={"temperature": v})
        s.coord._handle_trv_setpoint_change(
            "climate.trv", state(12), state(25), s.coord.zone_cfg
        )
        s.coord._active_control = False
        s.coord.set_system_state(vacation=False, summer=False)
        zone = await s.run()
        assert zone["preheat_active"] is False
        assert s.coord.preheat_plan.reason == "cancelled_by_manual_change"

    async def test_event_target_change_is_not_manual(self, timers):
        s = Scenario(EVENT_15)
        s.at(PREHEAT_AT)
        await s.run()
        s.coord.set_target(19.0, "event")
        assert (await s.run())["preheat_active"] is True


class TestPlanLifecycle:
    async def test_stale_plan_dropped_when_events_change(self, timers):
        s = Scenario(EVENT_15)
        await s.run()
        s.coord.scheduler.set_events([])
        await s.run()
        assert s.coord.preheat_plan.reason == "no_event"
        assert not timers.preheat.live

    async def test_cleanup_cancels_timer(self, timers):
        s = Scenario(EVENT_15)
        await s.run()
        s.coord.cleanup_event_listeners()
        assert not timers.preheat.live

    async def test_status_flag_follows_activity(self, timers):
        s = Scenario(EVENT_15)
        zone = await s.run()
        assert zone["preheat_state"] == "planned"
        s.at(PREHEAT_AT)
        zone = await s.run()
        assert zone["preheat_state"] == "active"
