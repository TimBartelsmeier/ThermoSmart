"""Desired-target state: last writer wins, presets, restore, validation."""
from __future__ import annotations

import math

import pytest

from custom_components.thermosmart.const import (
    PRESET_AWAY,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NIGHT,
    PRESET_VACATION,
)
from custom_components.thermosmart.target import (
    MANUAL_SOURCES,
    SOURCE_DEFAULT,
    SOURCE_DIAL,
    SOURCE_EVENT,
    SOURCE_PRESET,
    SOURCE_RESTORE,
    SOURCE_USER,
    TargetState,
    preset_temperature,
)
from tests.helpers import make_coordinator


class TestTargetState:
    def test_initial_value_is_comfort_default(self):
        t = TargetState()
        assert t.value == 21.0
        assert t.current.source == SOURCE_DEFAULT
        assert t.preset == PRESET_COMFORT

    @pytest.mark.parametrize(
        "first,second",
        [
            (SOURCE_USER, SOURCE_EVENT),
            (SOURCE_EVENT, SOURCE_USER),
            (SOURCE_DIAL, SOURCE_EVENT),
            (SOURCE_EVENT, SOURCE_DIAL),
            (SOURCE_PRESET, SOURCE_DIAL),
            (SOURCE_DIAL, SOURCE_PRESET),
        ],
    )
    def test_last_writer_wins(self, first, second):
        t = TargetState()
        t.set(19.0, first)
        t.set(23.0, second)
        assert t.value == 23.0
        assert t.current.source == second

    def test_unchanged_write_is_noop_and_silent(self):
        t = TargetState()
        calls = []
        t.add_listener(lambda new, prev: calls.append((new, prev)))
        assert t.set(20.0, SOURCE_USER) is True
        assert t.set(20.0, SOURCE_EVENT) is False
        assert len(calls) == 1
        assert t.current.source == SOURCE_USER

    def test_listener_gets_new_and_previous(self):
        t = TargetState()
        calls = []
        t.add_listener(lambda new, prev: calls.append((new.value, prev.value)))
        t.set(18.0, SOURCE_USER)
        assert calls == [(18.0, 21.0)]

    def test_unsubscribe(self):
        t = TargetState()
        calls = []
        unsub = t.add_listener(lambda new, prev: calls.append(1))
        unsub()
        unsub()
        t.set(18.0, SOURCE_USER)
        assert calls == []

    def test_non_preset_write_clears_preset(self):
        t = TargetState()
        t.set(18.0, SOURCE_PRESET, preset=PRESET_ECO)
        assert t.preset == PRESET_ECO
        t.set(19.5, SOURCE_USER)
        assert t.preset is None

    def test_same_value_different_preset_is_a_change(self):
        t = TargetState()
        t.set(18.0, SOURCE_USER)
        assert t.set(18.0, SOURCE_PRESET, preset=PRESET_ECO) is True
        assert t.preset == PRESET_ECO

    @pytest.mark.parametrize("bad", [4.9, 30.1, math.nan, math.inf, "abc", None])
    def test_invalid_values_rejected_and_state_kept(self, bad):
        t = TargetState()
        with pytest.raises(ValueError):
            t.set(bad, SOURCE_USER)
        assert t.value == 21.0

    @pytest.mark.parametrize("edge", [5.0, 30.0])
    def test_range_edges_accepted(self, edge):
        t = TargetState()
        t.set(edge, SOURCE_USER)
        assert t.value == edge

    def test_restore_is_silent(self):
        t = TargetState()
        calls = []
        t.add_listener(lambda new, prev: calls.append(1))
        t.restore(19.0, PRESET_NIGHT)
        assert calls == []
        assert t.value == 19.0
        assert t.preset == PRESET_NIGHT
        assert t.current.source == SOURCE_RESTORE

    def test_manual_sources(self):
        assert MANUAL_SOURCES == {SOURCE_USER, SOURCE_PRESET, SOURCE_DIAL}


class TestPresetTemperature:
    def test_reads_zone_config(self):
        cfg = {"comfort_temp": 22, "night_temp": 17, "away_temp": 16,
               "eco_temp": 18.5, "vacation_temp": 10}
        assert preset_temperature(cfg, PRESET_COMFORT) == 22
        assert preset_temperature(cfg, PRESET_NIGHT) == 17
        assert preset_temperature(cfg, PRESET_AWAY) == 16
        assert preset_temperature(cfg, PRESET_ECO) == 18.5
        assert preset_temperature(cfg, PRESET_VACATION) == 10

    def test_falls_back_to_defaults(self):
        assert preset_temperature({}, PRESET_COMFORT) == 21.0


class TestCoordinatorTarget:
    def test_set_preset_uses_config_and_marks_manual(self):
        coord = make_coordinator()
        coord.set_preset(PRESET_NIGHT)
        assert coord.target.value == coord.zone_cfg.get("night_temp", 18.0)
        assert coord.target.preset == PRESET_NIGHT
        assert coord._manual_change_pending is True

    def test_event_source_is_not_manual(self):
        coord = make_coordinator()
        coord._manual_change_pending = False
        coord.set_target(19.0, SOURCE_EVENT)
        assert coord._manual_change_pending is False

    def test_out_of_range_raises_and_keeps_target(self):
        coord = make_coordinator()
        before = coord.target.value
        with pytest.raises(ValueError):
            coord.set_target(40.0, SOURCE_USER)
        assert coord.target.value == before

    def test_suspended_by_summer_or_vacation(self):
        coord = make_coordinator()
        coord._is_summer = False
        coord._vacation_mode = False
        assert coord.suspended is False
        coord._vacation_mode = True
        assert coord.suspended is True
        coord._vacation_mode = False
        coord._is_summer = True
        assert coord.suspended is True
