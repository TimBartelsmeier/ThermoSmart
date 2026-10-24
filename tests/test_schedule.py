"""Schedule parsing, next-occurrence maths, timer runner and YAML management."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.thermosmart import schedule as sched
from custom_components.thermosmart.schedule import (
    ScheduleError,
    ScheduleEvent,
    ScheduleRunner,
    next_occurrence,
    parse_events,
    zone_key,
)

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


def _ev(days, time, target, *, preheat=False):
    return {"days": days, "time": time, "target": target, "preheat": preheat}


def _events(*raw):
    return parse_events(list(raw))


# ── Parsing ───────────────────────────────────────────────────────────────────

class TestParse:
    def test_valid_numeric_and_preset(self):
        events = _events(
            _ev(["mon", "tue"], "06:30", 21),
            _ev("sat", "08:00", "eco", preheat=True),
        )
        assert events[0].days == (0, 1) and events[0].minute == 390
        assert events[0].target == 21.0 and events[0].preheat is False
        assert events[1].days == (5,) and events[1].target == "eco"
        assert events[1].preheat is True

    def test_weekday_numbers_and_case_and_dedup(self):
        (event,) = _events(_ev([6, "MON", 0, " sun "], "00:00", 20))
        assert event.days == (0, 6)

    def test_sleep_is_alias_of_night(self):
        (event,) = _events(_ev(["mon"], "22:00", "Sleep"))
        assert event.target == "night"

    @pytest.mark.parametrize("time", ["24:00", "12:60", "abc", "", None, "7"])
    def test_invalid_time(self, time):
        with pytest.raises(ScheduleError, match="HH:MM"):
            _events(_ev(["mon"], time, 20))

    @pytest.mark.parametrize("days", [[], None, ["funday"], [7], [True], 5.5])
    def test_invalid_days(self, days):
        with pytest.raises(ScheduleError):
            _events(_ev(days, "06:00", 20))

    @pytest.mark.parametrize("target", [4.9, 30.1, True, "toasty", None, [20]])
    def test_invalid_target(self, target):
        with pytest.raises(ScheduleError):
            _events(_ev(["mon"], "06:00", target))

    def test_target_range_matches_desired_target_range(self):
        _events(_ev(["mon"], "06:00", 5), _ev(["mon"], "07:00", 30))

    def test_error_names_the_event(self):
        with pytest.raises(ScheduleError, match="event 2"):
            _events(_ev(["mon"], "06:00", 20), _ev(["mon"], "99:00", 20))

    def test_not_a_list_or_mapping(self):
        with pytest.raises(ScheduleError):
            parse_events({"a": 1})
        with pytest.raises(ScheduleError, match="mapping"):
            parse_events(["x"])

    def test_empty_list(self):
        assert parse_events([]) == []


class TestZoneKey:
    @pytest.mark.parametrize("name,key", [
        ("Wohnzimmer", "wohnzimmer"),
        ("Küche & Bad", "kuche_bad"),
        ("  Living  Room 2 ", "living_room_2"),
        ("日本", "zone"),
    ])
    def test_zone_key(self, name, key):
        assert zone_key(name) == key


# ── next_occurrence ───────────────────────────────────────────────────────────

class TestNextOccurrence:
    def test_later_same_day(self):
        events = _events(_ev(["wed"], "18:00", 21))
        now = datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN)  # Wednesday
        occ = next_occurrence(events, now)
        assert occ.at == datetime(2026, 5, 6, 18, 0, tzinfo=BERLIN)
        assert occ.minutes_until == 360

    def test_exact_time_is_not_next(self):
        events = _events(_ev(["wed"], "12:00", 21))
        now = datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN)
        occ = next_occurrence(events, now)
        assert occ.at == datetime(2026, 5, 13, 12, 0, tzinfo=BERLIN)

    def test_week_rollover(self):
        events = _events(_ev(["mon"], "06:00", 21))
        now = datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN)  # Wed
        assert next_occurrence(events, now).at == datetime(2026, 5, 11, 6, 0, tzinfo=BERLIN)

    def test_year_rollover(self):
        events = _events(_ev(["fri"], "06:00", 21))
        now = datetime(2026, 12, 31, 12, 0, tzinfo=BERLIN)  # Thursday
        assert next_occurrence(events, now).at == datetime(2027, 1, 1, 6, 0, tzinfo=BERLIN)

    def test_picks_earliest_of_several(self):
        events = _events(
            _ev(["mon"], "06:00", 21), _ev(["wed"], "20:00", 17), _ev(["wed"], "15:00", 19),
        )
        now = datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN)
        assert next_occurrence(events, now).event.target == 19.0

    def test_duplicate_timestamp_last_listed_wins(self):
        events = _events(_ev(["wed"], "15:00", 19), _ev(["wed"], "15:00", 23))
        now = datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN)
        assert next_occurrence(events, now).event.target == 23.0

    def test_no_events(self):
        assert next_occurrence([], datetime(2026, 5, 6, tzinfo=BERLIN)) is None

    def test_naive_now_rejected(self):
        with pytest.raises(ValueError):
            next_occurrence(_events(_ev(["mon"], "06:00", 21)), datetime(2026, 5, 6))

    def test_minutes_until_rounds_up_and_is_at_least_one(self):
        events = _events(_ev(["wed"], "12:01", 21))
        now = datetime(2026, 5, 6, 12, 0, 30, tzinfo=BERLIN)
        assert next_occurrence(events, now).minutes_until == 1

    def test_spring_forward_gap_resolves_to_equivalent_instant(self):
        # 2026-03-29: 02:00 → 03:00 in Europe/Berlin; 02:30 does not exist.
        events = _events(_ev(["sun"], "02:30", 21))
        now = datetime(2026, 3, 28, 23, 0, tzinfo=BERLIN)
        occ = next_occurrence(events, now)
        assert occ.at.astimezone(UTC) == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
        assert occ.minutes_until == 210

    def test_event_after_the_jump_keeps_wall_clock_time(self):
        events = _events(_ev(["sun"], "06:00", 21))
        now = datetime(2026, 3, 28, 23, 0, tzinfo=BERLIN)
        occ = next_occurrence(events, now)
        assert occ.at.hour == 6 and occ.at.utcoffset() == timedelta(hours=2)
        # real elapsed time is 6 h (not 7 h) because one hour was skipped
        assert occ.minutes_until == 6 * 60

    def test_fall_back_fires_once_at_first_occurrence(self):
        # 2026-10-25: 03:00 → 02:00; 02:30 happens twice.
        events = _events(_ev(["sun"], "02:30", 21))
        now = datetime(2026, 10, 25, 0, 0, tzinfo=BERLIN)
        first = next_occurrence(events, now)
        assert first.at.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
        after = next_occurrence(events, first.at)
        assert after.at.date() == datetime(2026, 11, 1).date()

    def test_other_timezone_is_respected(self):
        events = _events(_ev(["wed"], "08:00", 21))
        tz = ZoneInfo("America/New_York")
        now = datetime(2026, 5, 6, 7, 0, tzinfo=tz)
        assert next_occurrence(events, now).at == datetime(2026, 5, 6, 8, 0, tzinfo=tz)


# ── Runner ────────────────────────────────────────────────────────────────────

class _Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self):
        return self.now.astimezone(BERLIN)


class _Timers:
    """Stand-in for async_track_point_in_utc_time."""

    def __init__(self):
        self.armed: list[dict] = []

    def __call__(self, hass, action, when):
        entry = {"action": action, "when": when, "cancelled": False}
        self.armed.append(entry)

        def cancel():
            entry["cancelled"] = True

        return cancel

    @property
    def live(self):
        return [t for t in self.armed if not t["cancelled"]]


@pytest.fixture
def timers():
    t = _Timers()
    with patch.object(sched, "async_track_point_in_utc_time", t):
        yield t


def _runner(clock, *, active=True, events=None):
    fired: list = []
    is_active = {"value": active}
    runner = ScheduleRunner(
        MagicMock(), now=clock, is_active=lambda now: is_active["value"],
        on_event=fired.append,
    )
    if events is not None:
        runner.set_events(events)
    return runner, fired, is_active


def _fire(timers, clock):
    """Advance the clock to the armed time and run the timer callback."""
    (timer,) = timers.live
    timer["cancelled"] = True  # a fired timer is gone, like in HA
    clock.now = timer["when"]
    timer["action"](timer["when"])


class TestRunner:
    def test_arms_exact_next_time(self, timers):
        clock = _Clock(datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN))
        runner, _, _ = _runner(clock, events=_events(_ev(["wed"], "15:00", 20)))
        runner.start()
        assert [t["when"] for t in timers.live] == [datetime(2026, 5, 6, 13, 0, tzinfo=UTC)]

    def test_fires_and_rearms_for_next_week(self, timers):
        clock = _Clock(datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN))
        runner, fired, _ = _runner(clock, events=_events(_ev(["wed"], "15:00", 20)))
        runner.start()
        _fire(timers, clock)
        assert len(fired) == 1
        assert runner.next_event.at == datetime(2026, 5, 13, 15, 0, tzinfo=BERLIN)
        assert len(timers.live) == 1

    def test_early_callback_does_not_fire_twice(self, timers):
        clock = _Clock(datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN))
        runner, fired, _ = _runner(clock, events=_events(_ev(["wed"], "15:00", 20)))
        runner.start()
        (timer,) = timers.live
        clock.now = timer["when"] - timedelta(milliseconds=50)  # fires a hair early
        timer["action"](timer["when"])
        assert len(fired) == 1
        assert runner.next_event.at == datetime(2026, 5, 13, 15, 0, tzinfo=BERLIN)

    def test_chain_of_events_across_week(self, timers):
        clock = _Clock(datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN))
        runner, fired, _ = _runner(clock, events=_events(
            _ev(["wed"], "15:00", 20), _ev(["thu"], "06:00", 22), _ev(["mon"], "06:00", 18),
        ))
        runner.start()
        for _ in range(3):
            _fire(timers, clock)
        assert [o.event.target for o in fired] == [20.0, 22.0, 18.0]

    def test_inactive_scheduling_skips_but_keeps_chain(self, timers):
        clock = _Clock(datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN))
        runner, fired, active = _runner(
            clock, events=_events(_ev(["wed"], "15:00", 20), _ev(["wed"], "16:00", 22)),
        )
        runner.start()
        active["value"] = False
        _fire(timers, clock)
        assert fired == []
        active["value"] = True  # re-enabling does not apply anything retroactively
        assert len(timers.live) == 1
        _fire(timers, clock)
        assert [o.event.target for o in fired] == [22.0]

    def test_no_catch_up_on_start(self, timers):
        # 18:00 is already past at start: nothing fires, next one is next week.
        clock = _Clock(datetime(2026, 5, 6, 19, 0, tzinfo=BERLIN))
        runner, fired, _ = _runner(clock, events=_events(_ev(["wed"], "18:00", 20)))
        runner.start()
        assert fired == []
        assert runner.next_event.at == datetime(2026, 5, 13, 18, 0, tzinfo=BERLIN)

    def test_set_events_while_running_rearms_and_cancels_old(self, timers):
        clock = _Clock(datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN))
        runner, _, _ = _runner(clock, events=_events(_ev(["wed"], "15:00", 20)))
        runner.start()
        runner.set_events(_events(_ev(["wed"], "13:00", 20)))
        assert len(timers.live) == 1
        assert timers.live[0]["when"] == datetime(2026, 5, 6, 11, 0, tzinfo=UTC)
        assert timers.armed[0]["cancelled"] is True

    def test_no_events_arms_nothing(self, timers):
        clock = _Clock(datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN))
        runner, _, _ = _runner(clock, events=[])
        runner.start()
        assert timers.live == [] and runner.next_event is None

    def test_stop_cancels_timer(self, timers):
        clock = _Clock(datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN))
        runner, _, _ = _runner(clock, events=_events(_ev(["wed"], "15:00", 20)))
        runner.start()
        runner.stop()
        assert timers.live == [] and runner.next_event is None
        runner.set_events(_events(_ev(["wed"], "16:00", 20)))  # not running: stays idle
        assert timers.live == []

    def test_handler_exception_keeps_the_chain_alive(self, timers):
        clock = _Clock(datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN))
        runner = ScheduleRunner(
            MagicMock(), now=clock, is_active=lambda now: True,
            on_event=MagicMock(side_effect=RuntimeError("boom")),
        )
        runner.set_events(_events(_ev(["wed"], "15:00", 20)))
        runner.start()
        _fire(timers, clock)
        assert len(timers.live) == 1

    def test_dst_day_fires_at_correct_instant(self, timers):
        clock = _Clock(datetime(2026, 3, 28, 23, 0, tzinfo=BERLIN))
        runner, fired, _ = _runner(clock, events=_events(_ev(["sun"], "06:00", 20)))
        runner.start()
        assert timers.live[0]["when"] == datetime(2026, 3, 29, 4, 0, tzinfo=UTC)
        _fire(timers, clock)
        assert len(fired) == 1

    def test_on_change_called_when_rearmed(self, timers):
        clock = _Clock(datetime(2026, 5, 6, 12, 0, tzinfo=BERLIN))
        changes = []
        runner = ScheduleRunner(
            MagicMock(), now=clock, is_active=lambda now: True,
            on_event=lambda occ: None, on_change=lambda: changes.append(1),
        )
        runner.set_events(_events(_ev(["wed"], "15:00", 20)))
        runner.start()
        _fire(timers, clock)
        assert len(changes) == 2


# ── YAML file management ──────────────────────────────────────────────────────

def _entry(entry_id, name, **extra):
    return SimpleNamespace(
        entry_id=entry_id, data={"name": name, **extra}, options={},
    )


def _hass(tmp_path):
    async def run_in_executor(func, *args):
        return func(*args)

    hass = SimpleNamespace(
        data={},
        config=SimpleNamespace(path=lambda name: str(tmp_path / name)),
        async_add_executor_job=run_in_executor,
    )
    return hass


def _file(tmp_path):
    return tmp_path / sched.SCHEDULE_FILENAME


class TestYamlManagement:
    async def test_creates_file_with_empty_zone_blocks_sorted(self, tmp_path):
        import yaml

        hass = _hass(tmp_path)
        entries = [_entry("2", "Wohnzimmer"), _entry("1", "Bad"),
                   _entry("sys", "ignored", entry_type="system")]
        await sched.async_manage_schedule_yaml(hass, entries)
        data = yaml.safe_load(_file(tmp_path).read_text())
        assert list(data["zones"]) == ["bad", "wohnzimmer"]
        assert data["zones"]["bad"] == {"events": []}

    async def test_never_modifies_or_deletes_existing_blocks(self, tmp_path):
        import yaml

        _file(tmp_path).write_text(yaml.safe_dump({"zones": {
            "wohnzimmer": {"events": [{"days": ["mon"], "time": "06:00", "target": 21}]},
            "old_room": {"events": [{"days": ["tue"], "time": "07:00", "target": "eco"}]},
        }}))
        hass = _hass(tmp_path)
        await sched.async_manage_schedule_yaml(hass, [_entry("1", "Wohnzimmer"), _entry("2", "Bad")])
        data = yaml.safe_load(_file(tmp_path).read_text())
        assert list(data["zones"]) == ["bad", "old_room", "wohnzimmer"]
        assert data["zones"]["wohnzimmer"]["events"][0]["target"] == 21
        assert data["zones"]["old_room"]["events"][0]["target"] == "eco"

    async def test_untouched_file_is_not_rewritten(self, tmp_path):
        hass = _hass(tmp_path)
        entries = [_entry("1", "Bad")]
        await sched.async_manage_schedule_yaml(hass, entries)
        before = _file(tmp_path).stat().st_mtime_ns
        await sched.async_manage_schedule_yaml(hass, entries)
        assert _file(tmp_path).stat().st_mtime_ns == before

    async def test_invalid_yaml_is_reported_not_overwritten(self, tmp_path):
        _file(tmp_path).write_text("zones: [unclosed")
        hass = _hass(tmp_path)
        with pytest.raises(ScheduleError, match="cannot parse"):
            await sched.async_manage_schedule_yaml(hass, [_entry("1", "Bad")])
        assert _file(tmp_path).read_text() == "zones: [unclosed"

    async def test_orphans_are_reported(self, tmp_path):
        _file(tmp_path).write_text("zones:\n  bad: {events: []}\n  gone: {events: []}\n")
        orphans = await sched.async_orphaned_zone_keys(_hass(tmp_path), [_entry("1", "Bad")])
        assert orphans == ("gone",)

    async def test_load_schedule_for_zone(self, tmp_path):
        _file(tmp_path).write_text(
            "zones:\n  bad:\n    events:\n"
            "      - {days: [mon], time: '06:00', target: 21, preheat: true}\n"
        )
        events = await sched.async_load_schedule(_hass(tmp_path), _entry("1", "Bad"))
        assert events == [ScheduleEvent((0,), 360, 21.0, True, 0)]

    async def test_load_missing_file_or_zone_is_empty(self, tmp_path):
        hass = _hass(tmp_path)
        assert await sched.async_load_schedule(hass, _entry("1", "Bad")) == []
        _file(tmp_path).write_text("zones: {other: {events: []}}")
        assert await sched.async_load_schedule(hass, _entry("1", "Bad")) == []

    async def test_load_invalid_event_names_zone_and_event(self, tmp_path):
        _file(tmp_path).write_text(
            "zones:\n  bad:\n    events:\n      - {days: [mon], time: '25:00', target: 21}\n"
        )
        with pytest.raises(ScheduleError, match="zone 'bad', event 1"):
            await sched.async_load_schedule(_hass(tmp_path), _entry("1", "Bad"))

    async def test_load_uses_renamed_zone_from_options(self, tmp_path):
        _file(tmp_path).write_text(
            "zones:\n  kuche:\n    events:\n      - {days: [mon], time: '06:00', target: 21}\n"
        )
        entry = _entry("1", "Bad")
        entry.options = {"name": "Küche"}
        assert len(await sched.async_load_schedule(_hass(tmp_path), entry)) == 1

    async def test_duplicate_and_existing_warnings(self, tmp_path):
        _file(tmp_path).write_text("zones: {bad: {events: []}}")
        hass = _hass(tmp_path)
        entries = [_entry("1", "Bad")]
        assert await sched.async_schedule_zone_warnings(hass, entries, "Bad") == (
            "duplicate:bad:Bad",)
        assert await sched.async_schedule_zone_warnings(
            hass, entries, "Bad", current_entry_id="1") == ()
        assert await sched.async_schedule_zone_warnings(hass, [], "Bad") == ("existing:bad",)
        assert await sched.async_schedule_zone_warnings(hass, [], "New") == ()

    def test_file_lives_in_config_dir(self, tmp_path):
        assert os.path.basename(sched._path(_hass(tmp_path))) == "thermosmart_schedules.yaml"
