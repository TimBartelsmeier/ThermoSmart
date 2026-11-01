"""Learn / Adapt switch split (review H1).

"Learn room behaviour" controls observation and model updates;
"Adapt control to learned behaviour" controls whether learned parameters
drive control. The two are independent.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.thermosmart import switch as switch_mod
from custom_components.thermosmart.coordinator import ControlAdaptationMode
from tests.helpers import make_coordinator
from tests.helpers_ha_runtime import (
    FakeStore, attach_shadow, make_recording_coordinator,
)


async def _coord(*, learn: bool, adapt: bool, active: bool = True):
    from custom_components.thermosmart.learning.runtime.lifecycle import LearningRuntimeMode
    coord = make_recording_coordinator({})
    coord.set_active_control(active)
    coord.set_learning_switches(learn=learn, adapt=adapt)
    shadow = attach_shadow(coord, mode=LearningRuntimeMode.CONTROL, store=FakeStore())
    await shadow.async_setup()
    return coord, shadow


async def _cycle(coord) -> dict:
    return ((await coord._async_update_data()) or {}).get("zone", {})


class TestDerive:

    def test_adapt_defaults_to_learn(self):
        assert ControlAdaptationMode.derive(True, True) == ControlAdaptationMode.ADAPTIVE
        assert ControlAdaptationMode.derive(False, True) == ControlAdaptationMode.DETERMINISTIC

    @pytest.mark.parametrize("learn,active,adapt,expected", [
        (False, True, True, ControlAdaptationMode.ADAPTIVE),
        (True, True, False, ControlAdaptationMode.DETERMINISTIC),
        (True, False, False, ControlAdaptationMode.SHADOW_ONLY),
        (False, False, True, ControlAdaptationMode.INACTIVE),
    ])
    def test_three_flags(self, learn, active, adapt, expected):
        assert ControlAdaptationMode.derive(learn, active, adapt) == expected


class TestCoordinatorFlags:

    def test_defaults_on(self):
        coord = make_coordinator()
        assert coord.learn_enabled is True
        assert coord.adapt_enabled is True

    def test_setter_is_partial(self):
        coord = make_coordinator()
        coord.set_learning_switches(adapt=False)
        assert coord.learn_enabled is True
        assert coord.adapt_enabled is False
        coord.set_learning_switches(learn=False)
        assert coord.learn_enabled is False
        assert coord.adapt_enabled is False


class TestRuntimeEffects:

    @pytest.mark.asyncio
    async def test_learn_off_adapt_on_stops_observation_keeps_learned_tpi(self):
        coord, shadow = await _coord(learn=False, adapt=True)
        with patch.object(shadow, "observe_safe", wraps=shadow.observe_safe) as spy:
            zone = await _cycle(coord)
        assert not spy.called
        assert zone.get("tpi_coef_source") != "deterministic_baseline"
        assert zone["learn_enabled"] is False
        assert zone["adapt_enabled"] is True
        assert zone["adaptation_mode"] == ControlAdaptationMode.ADAPTIVE

    @pytest.mark.asyncio
    async def test_learn_on_adapt_off_observes_with_baseline_tpi(self):
        coord, shadow = await _coord(learn=True, adapt=False)
        with patch.object(shadow, "observe_safe", wraps=shadow.observe_safe) as spy:
            zone = await _cycle(coord)
        assert spy.called
        assert zone.get("tpi_coef_source") == "deterministic_baseline"
        assert zone["adaptation_mode"] == ControlAdaptationMode.DETERMINISTIC


def _switch_with_restore(cls, *, own_last, legacy_state):
    coord = MagicMock()
    entry = SimpleNamespace(entry_id="e1", title="Zone", data={})
    with patch.object(switch_mod, "_device_info", return_value=None):
        sw = cls(coord, entry)
    sw.hass = MagicMock()

    async def _last():
        return own_last

    sw.async_get_last_state = _last
    registry = MagicMock()
    registry.async_get_entity_id.return_value = (
        "switch.zone_learning" if legacy_state is not None else None)
    restore = SimpleNamespace(last_states={} if legacy_state is None else {
        "switch.zone_learning": SimpleNamespace(state=SimpleNamespace(state=legacy_state)),
    })
    return sw, coord, registry, restore


class TestAdaptSwitchMigration:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("own_last,legacy,expected", [
        (None, "off", False),
        (None, "on", True),
        (None, None, True),
        (SimpleNamespace(state="on"), "off", True),
        (SimpleNamespace(state="off"), "on", False),
    ])
    async def test_initial_state(self, own_last, legacy, expected):
        sw, coord, registry, restore = _switch_with_restore(
            switch_mod.ThermoSmartAdaptSwitch, own_last=own_last, legacy_state=legacy)
        with patch.object(switch_mod.er, "async_get", return_value=registry), \
             patch.object(switch_mod, "async_get_restore_data", return_value=restore), \
             patch.object(switch_mod.RestoreEntity, "async_added_to_hass", return_value=None):
            sw.async_on_remove = MagicMock()
            await sw.async_added_to_hass()
        coord.set_learning_switches.assert_called_once_with(adapt=expected)

    @pytest.mark.asyncio
    async def test_learn_switch_defaults_on_without_history(self):
        sw, coord, registry, restore = _switch_with_restore(
            switch_mod.ThermoSmartLearningSwitch, own_last=None, legacy_state=None)
        with patch.object(switch_mod.RestoreEntity, "async_added_to_hass", return_value=None):
            sw.async_on_remove = MagicMock()
            await sw.async_added_to_hass()
        coord.set_learning_switches.assert_called_once_with(learn=True)

    def test_unique_ids(self):
        coord = MagicMock()
        entry = SimpleNamespace(entry_id="e1", title="Zone", data={})
        with patch.object(switch_mod, "_device_info", return_value=None):
            assert switch_mod.ThermoSmartLearningSwitch(coord, entry).unique_id == "e1_learning"
            assert switch_mod.ThermoSmartAdaptSwitch(coord, entry).unique_id == "e1_adapt_control"
