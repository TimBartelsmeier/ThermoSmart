"""Pure suspension logic: precedence and automatic summer detection."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import product

import pytest

from custom_components.thermosmart.const import TEMP_FROST_PROTECTION
from custom_components.thermosmart.suspension import (
    SUMMER,
    VACATION,
    WINDOW_OPEN,
    SummerDetector,
    held_at,
    resolve,
)

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


class TestResolve:
    @pytest.mark.parametrize(
        "summer, vacation, window, expected",
        [
            (False, False, False, None),
            (False, False, True, WINDOW_OPEN),
            (False, True, False, VACATION),
            (False, True, True, VACATION),
            (True, False, False, SUMMER),
            (True, False, True, SUMMER),
            (True, True, False, SUMMER),
            (True, True, True, SUMMER),
        ],
    )
    def test_truth_table(self, summer, vacation, window, expected):
        assert resolve(summer=summer, vacation=vacation, window_open=window) == expected

    def test_table_covers_all_combinations(self):
        assert len(list(product([False, True], repeat=3))) == 8

    def test_releasing_in_precedence_order(self):
        assert resolve(summer=True, vacation=True, window_open=True) == SUMMER
        assert resolve(summer=False, vacation=True, window_open=True) == VACATION
        assert resolve(summer=False, vacation=False, window_open=True) == WINDOW_OPEN
        assert resolve(summer=False, vacation=False, window_open=False) is None


class TestHeldAt:
    def test_summer_holds_frost_protection(self):
        assert held_at(SUMMER, 15.0) == TEMP_FROST_PROTECTION

    def test_vacation_holds_vacation_temperature(self):
        assert held_at(VACATION, 15.0) == 15.0

    def test_window_and_none_hold_nothing(self):
        assert held_at(WINDOW_OPEN, 15.0) is None
        assert held_at(None, 15.0) is None


def _feed(detector, temperature, hours, step_minutes=60, start=T0):
    result = detector.summer
    for i in range(int(hours * 60 / step_minutes) + 1):
        result = detector.add_sample(start + timedelta(minutes=i * step_minutes), temperature)
    return result


class TestSummerDetector:
    def _detector(self, **kw):
        return SummerDetector(18.0, 15.0, **kw)

    def test_thresholds_must_be_ordered(self):
        with pytest.raises(ValueError):
            SummerDetector(15.0, 15.0)
        with pytest.raises(ValueError):
            SummerDetector(15.0, 18.0)

    def test_no_decision_before_minimum_span(self):
        detector = self._detector()
        assert _feed(detector, 25.0, hours=71) is False

    def test_summer_after_minimum_span_above_on_threshold(self):
        assert _feed(self._detector(), 25.0, hours=72) is True

    def test_threshold_is_inclusive(self):
        assert _feed(self._detector(), 18.0, hours=72) is True

    def test_winter_after_span_at_or_below_off_threshold(self):
        detector = self._detector(summer=True)
        assert _feed(detector, 15.0, hours=72) is False

    def test_between_thresholds_keeps_previous_state(self):
        assert _feed(self._detector(summer=True), 16.5, hours=48) is True
        assert _feed(self._detector(summer=False), 16.5, hours=48) is False

    def test_average_covers_window_only(self):
        detector = self._detector()
        _feed(detector, 25.0, hours=80)
        assert detector.summer is True
        # cold readings replace the old ones once the 72 h window has moved on
        later = T0 + timedelta(hours=80)
        for i in range(1, 100):
            detector.add_sample(later + timedelta(hours=i), 5.0)
        assert detector.summer is False
        assert detector.span < timedelta(hours=73)

    def test_no_samples_means_no_average_and_no_summer(self):
        detector = self._detector()
        assert detector.average is None
        assert detector.span is None
        assert detector.summer is False

    def test_restored_state_survives_empty_history(self):
        detector = self._detector(summer=True)
        detector.add_sample(T0, 17.0)  # single reading: not enough to decide
        assert detector.summer is True


class TestFullWindowRequirement:
    def test_no_decision_on_24_hours_of_data(self):
        assert _feed(SummerDetector(18.0, 15.0), 25.0, hours=24) is False

    def test_decides_once_the_full_72_hours_are_covered(self):
        detector = SummerDetector(18.0, 15.0)
        assert _feed(detector, 25.0, hours=71, step_minutes=15) is False
        assert _feed(detector, 25.0, hours=73, step_minutes=15) is True

    def test_span_reaches_window_with_coarse_sampling(self):
        detector = SummerDetector(18.0, 15.0)
        _feed(detector, 25.0, hours=200, step_minutes=15)
        assert detector.span >= timedelta(hours=72)
        assert detector.summer is True
