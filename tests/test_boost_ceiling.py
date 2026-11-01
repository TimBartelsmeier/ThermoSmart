"""Zone boost ceiling (review M8)."""
from __future__ import annotations

import pytest

from custom_components.thermosmart.const import CONF_BOOST_CEILING
from custom_components.thermosmart.target import SOURCE_USER
from custom_components.thermosmart.tpi import cap_boost
from tests.helpers import make_state, set_hass_states
from tests.helpers_ha_runtime import (
    FakeStore, attach_shadow, inject_eligible_boost_model, make_recording_coordinator,
)


def _trv(max_temp=35.0):
    return make_state("heat", {
        "temperature": 15.0, "min_temp": 5.0, "max_temp": max_temp,
        "target_temp_step": 0.5,
    })


async def _cycle(cfg, target, *, indoor="15.0", max_temp=35.0):
    coord = make_recording_coordinator(cfg, indoor=indoor)
    coord.set_active_control(True)
    set_hass_states(coord, {"sensor.test_temp": make_state(indoor),
                            "climate.test_trv": _trv(max_temp)})
    coord.set_target(target, SOURCE_USER)
    zone = (await coord._async_update_data())["zone"]
    return coord, zone


def test_cap_boost():
    assert cap_boost(30.0, 21.0, 28.0) == 28.0
    assert cap_boost(25.0, 21.0, 28.0) == 25.0
    assert cap_boost(29.0, 29.0, 28.0) == 29.0


@pytest.mark.asyncio
async def test_default_ceiling_limits_boost():
    _, zone = await _cycle({}, 25.0)
    assert zone["trv_setpoint"] == 28.0


@pytest.mark.asyncio
async def test_custom_ceiling_limits_boost():
    _, zone = await _cycle({CONF_BOOST_CEILING: 24.0}, 21.0)
    assert zone["trv_setpoint"] == 24.0


@pytest.mark.asyncio
async def test_target_above_ceiling_written_unboosted():
    _, zone = await _cycle({}, 29.0)
    assert zone["effective_target"] == 29.0
    assert zone["trv_setpoint"] == 29.0


@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling,expected", [(22.0, 22.0), (35.0, None)])
async def test_learned_boost_respects_ceiling(ceiling, expected):
    from custom_components.thermosmart.learning.runtime.lifecycle import LearningRuntimeMode
    coord = make_recording_coordinator({CONF_BOOST_CEILING: ceiling}, indoor="20.5")
    coord.set_active_control(True)
    shadow = attach_shadow(coord, mode=LearningRuntimeMode.CONTROL, store=FakeStore())
    await shadow.async_setup()

    def _apply_learned_boost(rec, **_kw):
        base = float(rec["trv_setpoint"])
        rec["tpi_baseline_setpoint"] = base
        rec["trv_setpoint"] = base + 3.0
        rec["_boost_applied_c"] = 3.0
        rec["learning_boost_adjusted"] = True

    shadow.resolve_adaptive_boost_control = _apply_learned_boost
    set_hass_states(coord, {"sensor.test_temp": make_state("20.5"),
                            "climate.test_trv": _trv()})
    coord.set_target(21.0, SOURCE_USER)
    zone = (await coord._async_update_data())["zone"]
    assert zone["learning_boost_adjusted"] is True
    if expected is None:
        assert zone["trv_setpoint"] == pytest.approx(zone["tpi_baseline_setpoint"] + 3.0)
    else:
        assert zone["trv_setpoint"] == expected
        assert coord.control_calls[0][1] == expected
