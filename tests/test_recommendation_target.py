"""Recommendation: desired vs. effective target, suspensions and manual-change flag."""
from __future__ import annotations

from custom_components.thermosmart.target import SOURCE_EVENT, SOURCE_USER
from tests.helpers import make_state, set_hass_states
from tests.test_coordinator_integration import _make_testable


async def _zone(coord) -> dict:
    set_hass_states(coord, {"sensor.test_temp": make_state("20.0")})
    result = await coord._async_update_data()
    return result["zone"]


class TestEffectiveTarget:
    async def test_effective_equals_desired_by_default(self):
        coord = _make_testable()
        coord.set_target(22.0, SOURCE_USER)
        zone = await _zone(coord)
        assert zone["desired_target"] == 22.0
        assert zone["effective_target"] == 22.0
        assert zone["desired_target_source"] == SOURCE_USER

    async def test_vacation_holds_vacation_temp_but_keeps_desired(self):
        coord = _make_testable()
        coord.set_target(22.0, SOURCE_USER)
        coord.set_vacation_override(True)
        zone = await _zone(coord)
        assert zone["vacation_mode"] is True
        assert zone["desired_target"] == 22.0
        assert zone["effective_target"] == coord.zone_cfg.get("vacation_temp", 12.0)

    async def test_event_during_vacation_updates_desired_not_effective(self):
        coord = _make_testable()
        coord.set_vacation_override(True)
        coord.set_target(19.0, SOURCE_EVENT)
        zone = await _zone(coord)
        assert zone["desired_target"] == 19.0
        assert zone["effective_target"] != 19.0

    async def test_vacation_end_restores_latest_desired_target(self):
        coord = _make_testable()
        coord.set_vacation_override(True)
        coord.set_target(19.0, SOURCE_EVENT)
        await _zone(coord)
        coord.set_vacation_override(False)
        zone = await _zone(coord)
        assert zone["effective_target"] == 19.0

    async def test_summer_flag_and_target_still_tracked(self):
        coord = _make_testable()
        coord._is_summer = True
        coord.set_target(20.0, SOURCE_USER)
        zone = await _zone(coord)
        assert zone["is_summer"] is True
        assert zone["desired_target"] == 20.0

    async def test_removed_keys_are_gone(self):
        zone = await _zone(_make_testable())
        for key in ("adjusted_target", "base_target", "weather_offset", "mode",
                    "override_active", "forecast_suppression"):
            assert key not in zone


class TestManualChangeFlag:
    async def test_manual_change_is_one_shot(self):
        coord = _make_testable()
        coord.set_target(22.0, SOURCE_USER)
        first = await _zone(coord)
        second = await _zone(coord)
        assert first["manual_change"] is True
        assert second["manual_change"] is False

    async def test_event_change_is_not_manual(self):
        coord = _make_testable()
        coord._manual_change_pending = False
        coord.set_target(22.0, SOURCE_EVENT)
        zone = await _zone(coord)
        assert zone["manual_change"] is False
