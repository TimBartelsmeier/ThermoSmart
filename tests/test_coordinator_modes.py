"""Unit tests for ThermoSmart Coordinator mode logic (Welle 3).

Tests _effective_mode, _control_reason, target selection, set_mode,
set_vacation_override, set_summer_override, and _is_person_home.

Pure Python + MagicMock – runs on Windows without Docker.
No HA fixtures needed (uses MagicMock hass).
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from custom_components.thermosmart.climate import ThermoSmartClimate
from custom_components.thermosmart.const import (
    HEATING_MODE_AUTO,
    HEATING_MODE_AWAY,
    HEATING_MODE_COMFORT,
    HEATING_MODE_ECO,
    HEATING_MODE_NIGHT,
    HEATING_MODE_VACATION,
    HEATING_MODES,
)
from custom_components.thermosmart.schedule import parse_events

from tests.helpers import make_coordinator, make_state


# ── TestEffectiveMode ─────────────────────────────────────────────────────────


class TestEffectiveMode:
    def _presence(self, vacation=False, all_away=False):
        return {"vacation": vacation, "all_away": all_away, "persons_home": []}

    def test_vacation_presence_returns_vacation_mode(self):
        coord = make_coordinator()
        result = coord._effective_mode(self._presence(vacation=True))
        assert result == HEATING_MODE_VACATION

    def test_vacation_overrides_manual_mode(self):
        """Vacation presence flag beats any manually set mode."""
        coord = make_coordinator()
        coord._mode = HEATING_MODE_COMFORT
        result = coord._effective_mode(self._presence(vacation=True))
        assert result == HEATING_MODE_VACATION

    def test_manual_comfort_mode_returned(self):
        coord = make_coordinator()
        coord._mode = HEATING_MODE_COMFORT
        result = coord._effective_mode(self._presence())
        assert result == HEATING_MODE_COMFORT

    def test_manual_night_mode_returned(self):
        coord = make_coordinator()
        coord._mode = HEATING_MODE_NIGHT
        result = coord._effective_mode(self._presence())
        assert result == HEATING_MODE_NIGHT

    def test_manual_mode_beats_all_away(self):
        """Explicit mode (non-AUTO) takes priority over presence-away."""
        coord = make_coordinator()
        coord._mode = HEATING_MODE_COMFORT
        result = coord._effective_mode(self._presence(all_away=True))
        assert result == HEATING_MODE_COMFORT

    def test_legacy_auto_does_not_trigger_presence_away(self):
        coord = make_coordinator()
        coord._mode = HEATING_MODE_AUTO
        result = coord._effective_mode(self._presence(all_away=True))
        assert result == HEATING_MODE_AUTO

    def test_auto_no_presence_returns_auto(self):
        coord = make_coordinator()
        coord._mode = HEATING_MODE_AUTO
        result = coord._effective_mode(self._presence())
        assert result == HEATING_MODE_AUTO


# ── TestControlReason ─────────────────────────────────────────────────────────


class TestControlReason:
    def _rec(self, **kwargs) -> dict:
        defaults = {
            "window_open": False,
            "override_active": False,
            "mode": HEATING_MODE_AUTO,
            "is_summer": False,
        }
        return {**defaults, **kwargs}

    def test_window_open_highest_priority(self):
        rec = self._rec(window_open=True, override_active=True)
        assert make_coordinator()._control_reason(rec) == "window_open"

    def test_override_active_second_priority(self):
        rec = self._rec(mode=HEATING_MODE_VACATION)
        assert make_coordinator()._control_reason(rec) == "vacation"

    def test_vacation_mode_third_priority(self):
        rec = self._rec(mode=HEATING_MODE_VACATION)
        assert make_coordinator()._control_reason(rec) == "vacation"

    def test_summer_mode_fourth_priority(self):
        rec = self._rec(is_summer=True)
        assert make_coordinator()._control_reason(rec) == "summer_mode"

    def test_away_mode_fifth_priority(self):
        rec = self._rec(mode=HEATING_MODE_AWAY)
        assert make_coordinator()._control_reason(rec) == "presence"

    def test_default_is_schedule(self):
        rec = self._rec()
        assert make_coordinator()._control_reason(rec) == "schedule"


# ── TestSetOverride ───────────────────────────────────────────────────────────


class TestTargetSelection:
    def test_coordinator_starts_without_active_preset(self):
        coord = make_coordinator()
        assert coord._preset_mode is None

    def test_value_sets_current_target(self):
        coord = make_coordinator()
        coord.set_target_temperature(22.0)
        assert coord._target_temperature == 22.0

    def test_value_at_threshold_sets_current_target(self):
        coord = make_coordinator()
        coord.set_target_temperature(5.0)
        assert coord._target_temperature == 5.0

    def test_value_below_threshold_is_ignored(self):
        coord = make_coordinator()
        coord._target_temperature = 20.0
        coord.set_target_temperature(4.9)
        assert coord._target_temperature == 20.0

    def test_zero_value_is_ignored(self):
        coord = make_coordinator()
        coord._target_temperature = 20.0
        coord.set_target_temperature(0.0)
        assert coord._target_temperature == 20.0


# ── TestClimatePresetState ────────────────────────────────────────────────────


class TestClimatePresetState:
    def test_climate_reports_no_preset_for_manual_target(self):
        coord = make_coordinator()
        entry = MagicMock()
        entry.entry_id = "test_zone_001"
        entry.data = {"name": "Test Zone"}
        climate = ThermoSmartClimate(coord, entry)

        coord.set_mode(HEATING_MODE_COMFORT)
        coord.set_target_temperature(20.0)

        assert climate.preset_mode is None


# ── TestSetMode ───────────────────────────────────────────────────────────────


class TestSetMode:
    def test_mode_change_replaces_current_target(self):
        coord = make_coordinator()
        coord._mode = HEATING_MODE_AUTO
        coord.set_target_temperature(22.0)
        coord.set_mode(HEATING_MODE_COMFORT)
        assert coord._target_temperature == 21.0

    def test_same_mode_preserves_current_target(self):
        coord = make_coordinator()
        coord._mode = HEATING_MODE_COMFORT
        coord.set_target_temperature(22.0)
        coord.set_mode(HEATING_MODE_COMFORT)
        assert coord._target_temperature == 21.0

    def test_set_mode_updates_internal_mode(self):
        coord = make_coordinator()
        coord.set_mode(HEATING_MODE_ECO)
        assert coord._mode == HEATING_MODE_ECO
        assert coord._preset_mode == HEATING_MODE_ECO

    def test_manual_temperature_clears_preset(self):
        coord = make_coordinator()
        coord.set_mode(HEATING_MODE_ECO)
        coord.set_target_temperature(21.5)
        assert coord._preset_mode is None

    def test_numeric_schedule_target_clears_preset(self):
        coord = make_coordinator()
        coord.set_mode(HEATING_MODE_COMFORT)
        events = parse_events([{
            "days": ["mon"],
            "time": "06:00",
            "target": 20,
        }])

        coord._apply_schedule_event(
            {"schedule_events": events},
            now=datetime(2026, 10, 19, 7, 0),
        )

        assert coord._preset_mode is None


# ── TestSetVacationOverride ───────────────────────────────────────────────────


class TestSetVacationOverride:
    def test_activate_sets_vacation_mode(self):
        coord = make_coordinator()
        coord.set_vacation_override(True)
        assert coord._mode == HEATING_MODE_VACATION

    def test_activate_saves_previous_mode_as_pre_vacation(self):
        coord = make_coordinator()
        coord._mode = HEATING_MODE_COMFORT
        coord.set_vacation_override(True)
        assert coord._pre_vacation_mode == HEATING_MODE_COMFORT
        assert coord._target_temperature == 12.0

    def test_deactivate_restores_pre_vacation_mode(self):
        coord = make_coordinator()
        coord._mode = HEATING_MODE_NIGHT
        coord._schedule_target = 18.0
        coord.set_vacation_override(True)
        coord.set_vacation_override(False)
        assert coord._mode == HEATING_MODE_NIGHT
        assert coord._schedule_target == 18.0

    def test_deactivate_without_pre_vacation_mode_restores_comfort_preset(self):
        coord = make_coordinator()
        coord._pre_vacation_mode = None
        coord._mode = HEATING_MODE_VACATION
        coord.set_vacation_override(False)
        assert coord._mode == HEATING_MODE_COMFORT

    def test_auto_is_not_a_preset_temperature(self):
        assert HEATING_MODE_AUTO not in HEATING_MODES


class TestScheduleSetpoints:
    def test_event_applies_numeric_target_once(self):
        coord = make_coordinator()
        coord.set_target_temperature(18.0)
        events = parse_events([{
            "days": ["mon"],
            "time": "06:00",
            "target": 20,
        }])

        coord._apply_schedule_event(
            {"schedule_events": events},
            now=datetime(2026, 10, 19, 7, 0),
        )

        assert coord._target_temperature == 20.0
        assert coord._schedule_event_key == events[0].key

    def test_schedule_event_replaces_user_selected_target(self):
        coord = make_coordinator()
        coord.set_target_temperature(18.0)
        events = parse_events([{
            "days": ["mon"],
            "time": "06:00",
            "target": 21,
        }])

        coord._apply_schedule_event(
            {"schedule_events": events},
            now=datetime(2026, 10, 19, 7, 0),
        )

        assert coord._target_temperature == 21.0

    def test_manual_preset_remains_until_next_event(self):
        coord = make_coordinator()
        events = parse_events([{
            "days": ["mon"],
            "time": "06:00",
            "target": 20,
        }])
        now = datetime(2026, 10, 19, 7, 0)
        coord._apply_schedule_event({"schedule_events": events}, now=now)
        coord.set_mode(HEATING_MODE_NIGHT)

        coord._apply_schedule_event({"schedule_events": events}, now=now)

        assert coord._target_temperature == 18.0

    def test_named_event_highlights_its_preset(self):
        coord = make_coordinator()
        events = parse_events([{
            "days": ["mon"],
            "time": "06:00",
            "target": "night",
        }])

        coord._apply_schedule_event(
            {"schedule_events": events},
            now=datetime(2026, 10, 19, 7, 0),
        )

        assert coord._preset_mode == HEATING_MODE_NIGHT

    def test_numeric_event_has_no_preset_highlight(self):
        coord = make_coordinator()
        events = parse_events([{
            "days": ["mon"],
            "time": "06:00",
            "target": 20,
        }])

        coord._apply_schedule_event(
            {"schedule_events": events},
            now=datetime(2026, 10, 19, 7, 0),
        )

        assert coord._preset_mode is None

    def test_disabled_schedule_does_not_apply_event(self):
        coord = make_coordinator()
        coord._scheduling_enabled = False
        events = parse_events([{
            "days": ["mon"],
            "time": "06:00",
            "target": "night",
        }])

        coord._apply_schedule_event(
            {"schedule_events": events},
            now=datetime(2026, 10, 19, 7, 0),
        )

        assert coord._target_temperature is None
        assert coord._schedule_event_key is None

    def test_activate_replaces_current_target_with_vacation_target(self):
        coord = make_coordinator()
        coord.set_target_temperature(22.0)
        coord.set_vacation_override(True)
        assert coord._target_temperature == 12.0

    def test_double_activate_does_not_overwrite_pre_vacation_mode(self):
        """Second activate keeps the original pre_vacation_mode (not VACATION)."""
        coord = make_coordinator()
        coord._mode = HEATING_MODE_NIGHT
        coord.set_vacation_override(True)   # saves NIGHT
        coord.set_vacation_override(True)   # mode is already VACATION → not saved again
        assert coord._pre_vacation_mode == HEATING_MODE_NIGHT


# ── TestSetSummerOverride ─────────────────────────────────────────────────────


class TestSetSummerOverride:
    def test_true_forces_is_summer(self):
        coord = make_coordinator()
        coord._is_summer = False
        coord.set_summer_override(True)
        assert coord._is_summer is True

    def test_false_forces_is_summer_off(self):
        coord = make_coordinator()
        coord._is_summer = True
        coord.set_summer_override(False)
        assert coord._is_summer is False

    def test_none_leaves_is_summer_unchanged(self):
        coord = make_coordinator()
        coord._is_summer = True
        coord.set_summer_override(None)
        assert coord._is_summer is True

    def test_true_keeps_current_target_state_separate_from_summer_hold(self):
        coord = make_coordinator()
        coord.set_target_temperature(22.0)
        coord.set_summer_override(True)
        assert coord._target_temperature == 22.0

    def test_sets_summer_override_attribute(self):
        coord = make_coordinator()
        coord.set_summer_override(True)
        assert coord._summer_override is True
        coord.set_summer_override(None)
        assert coord._summer_override is None


# ── TestIsPersonHome ──────────────────────────────────────────────────────────


class TestIsPersonHome:
    def test_home_state_with_default_zone_returns_true(self):
        coord = make_coordinator()
        assert coord._is_person_home("home", "zone.home") is True

    def test_away_state_with_default_zone_returns_false(self):
        coord = make_coordinator()
        assert coord._is_person_home("work", "zone.home") is False

    def test_person_state_matches_zone_slug(self):
        """person_state == slug of zone_entity (without 'zone.' prefix) → home."""
        coord = make_coordinator()
        assert coord._is_person_home("office", "zone.office") is True

    def test_empty_zone_entity_treats_home_as_home(self):
        coord = make_coordinator()
        assert coord._is_person_home("home", "") is True

    def test_person_away_non_matching_zone(self):
        coord = make_coordinator()
        assert coord._is_person_home("work", "zone.office") is False

    def test_friendly_name_match(self):
        """person_state matching zone friendly_name → home."""
        coord = make_coordinator()
        coord.hass.states.get.side_effect = lambda eid: (
            make_state("20", attributes={"friendly_name": "Büro"})
            if eid == "zone.office" else None
        )
        assert coord._is_person_home("büro", "zone.office") is True
