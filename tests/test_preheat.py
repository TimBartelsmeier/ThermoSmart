"""Pure pre-heat planner."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.thermosmart import preheat as ph

EVENT = datetime(2026, 5, 6, 15, 0, tzinfo=timezone.utc)
NOW = EVENT - timedelta(hours=5)


def _plan(**kw):
    args = dict(
        now=NOW, scheduling_active=True, event_at=EVENT, event_target=21.0,
        event_preheat=True, desired_target=18.0, previous_event_at=None,
        duration_min=60.0, duration_status="deterministic_baseline",
        cancelled=False, started_at=None,
    )
    args.update(kw)
    return ph.plan(**args)


class TestPlanned:
    def test_start_is_event_minus_duration(self):
        p = _plan()
        assert (p.state, p.start) == (ph.PLANNED, EVENT - timedelta(minutes=60))
        assert (p.event_at, p.event_target, p.duration_min) == (EVENT, 21.0, 60.0)

    def test_active_exactly_at_start(self):
        assert _plan(now=EVENT - timedelta(minutes=60)).state == ph.ACTIVE

    def test_planned_one_second_before_start(self):
        assert _plan(now=EVENT - timedelta(minutes=60, seconds=1)).state == ph.PLANNED

    def test_active_after_start(self):
        assert _plan(now=EVENT - timedelta(minutes=1)).state == ph.ACTIVE

    def test_start_never_before_previous_event(self):
        prev = EVENT - timedelta(minutes=30)
        p = _plan(previous_event_at=prev)
        assert p.start == prev

    def test_previous_event_earlier_than_start_does_not_matter(self):
        prev = EVENT - timedelta(hours=3)
        assert _plan(previous_event_at=prev).start == EVENT - timedelta(minutes=60)


class TestNoPlan:
    def test_reasons(self):
        cases = [
            (dict(event_at=None, event_target=None), ph.NO_EVENT),
            (dict(scheduling_active=False), ph.SCHEDULING_INACTIVE),
            (dict(event_preheat=False), ph.EVENT_NO_PREHEAT),
            (dict(cancelled=True), ph.CANCELLED),
            (dict(event_target=18.0), ph.TARGET_NOT_HIGHER),
            (dict(event_target=17.0), ph.TARGET_NOT_HIGHER),
            (dict(duration_min=0.0, duration_status="unavailable"), ph.NO_TEMPERATURE),
            (dict(duration_min=0.0, duration_status="target_reached"), ph.TARGET_REACHED),
        ]
        for kw, reason in cases:
            p = _plan(**kw)
            assert (p.state, p.reason, p.start) == (ph.NONE, reason, None), kw

    def test_no_plan_still_reports_event_for_display(self):
        p = _plan(event_preheat=False)
        assert p.event_at == EVENT and p.event_target == 21.0


class TestLatch:
    def test_started_pre_heat_stays_active_when_estimate_shrinks(self):
        started = EVENT - timedelta(minutes=60)
        p = _plan(now=EVENT - timedelta(minutes=10), duration_min=15.0, started_at=started)
        assert (p.state, p.start) == (ph.ACTIVE, started)

    def test_started_pre_heat_survives_zero_estimate(self):
        started = EVENT - timedelta(minutes=60)
        p = _plan(duration_min=0.0, duration_status="target_reached", started_at=started)
        assert p.state == ph.ACTIVE

    def test_cancel_beats_latch(self):
        started = EVENT - timedelta(minutes=60)
        assert _plan(cancelled=True, started_at=started).state == ph.NONE

    def test_latch_ends_when_target_no_longer_higher(self):
        started = EVENT - timedelta(minutes=60)
        assert _plan(desired_target=21.0, started_at=started).state == ph.NONE
