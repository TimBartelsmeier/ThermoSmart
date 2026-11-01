"""Regression coverage for MaintenanceMixin's Active Control gate.

Real Heating Plausibility Audit finding: _async_valve_maintenance() checked
valve_likely_idle = is_summer or in_vacation without ever checking
active_control, so the weekly valve-exercise routine could dispatch a real
climate.set_temperature call even with Active Control switched off, as long
as the zone was in summer or vacation mode. Fixed by adding a hard
active_control gate alongside the existing early-exit checks.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from unittest.mock import patch

from custom_components.thermosmart import maintenance
from custom_components.thermosmart.learning.clock import FakeClock
from custom_components.thermosmart.triggers import TRIGGER_MAINTENANCE
from tests.helpers import make_coordinator, make_state, set_hass_states

_SUNDAY_3AM = datetime(2025, 1, 12, 3, 0, tzinfo=timezone.utc)  # matches VALVE_MAINTENANCE_WEEKDAY/HOUR


async def _run_maintenance(coord, cfg: dict, rec: dict, *, fail=(),
                           during_sleep=None) -> list[dict]:
    """Run _async_valve_maintenance() and collect any dispatched service calls."""
    calls: list[dict] = []
    coord.passes = []
    coord.request_pass = coord.passes.append

    async def _svc(domain, service, data, *, blocking=True, **kw):
        if data["entity_id"] in fail:
            raise RuntimeError("device rejected")
        calls.append(data)

    async def _sleep(_seconds):
        if during_sleep is not None:
            during_sleep(coord)

    coord.hass.services.async_call = _svc
    coord.hass.async_create_task = lambda coro: asyncio.ensure_future(coro)
    set_hass_states(coord, {
        "climate.trv": make_state("heat", {
            "temperature": 19.0, "min_temp": 5.0, "max_temp": 35.0, "target_temp_step": 0.5,
        }),
    })
    coord._device_profiles = {}
    coord._maintenance_running = False
    coord._last_maintenance = None
    coord._clock = FakeClock(_SUNDAY_3AM)

    with patch.object(maintenance.asyncio, "sleep", _sleep):
        await coord._async_valve_maintenance(cfg, rec)
        task = coord._maintenance_task
        if task is not None:
            await task
    return calls


def _cfg() -> dict:
    return {
        "climate_entities": ["climate.trv"],
        "valve_maintenance": True,
        "vacation_temp": 12.0,
        "comfort_temp": 21.0,
    }


class TestValveMaintenanceActiveControlGate:
    async def test_active_control_off_in_summer_dispatches_nothing(self):
        coord = make_coordinator()
        coord._is_summer = True
        coord._active_control = False
        calls = await _run_maintenance(coord, _cfg(), {"effective_target": 21.0})
        assert calls == []
        assert coord._maintenance_task is None

    async def test_active_control_off_in_vacation_dispatches_nothing(self):
        coord = make_coordinator()
        coord._is_summer = False
        coord._active_control = False
        rec = {"vacation_mode": True, "effective_target": 12.0}
        calls = await _run_maintenance(coord, _cfg(), rec)
        assert calls == []
        assert coord._maintenance_task is None

    async def test_active_control_on_in_summer_still_dispatches(self):
        coord = make_coordinator()
        coord._is_summer = True
        coord._active_control = True
        calls = await _run_maintenance(coord, _cfg(), {"effective_target": 21.0})
        assert len(calls) == 1  # exercise only; the return is a control pass
        assert calls[0]["entity_id"] == "climate.trv"
        assert coord.passes == [TRIGGER_MAINTENANCE]

    async def test_active_control_on_in_vacation_still_dispatches(self):
        coord = make_coordinator()
        coord._is_summer = False
        coord._active_control = True
        rec = {"vacation_mode": True, "effective_target": 12.0}
        calls = await _run_maintenance(coord, _cfg(), rec)
        assert len(calls) == 1
        assert coord.passes == [TRIGGER_MAINTENANCE]

    async def test_neither_summer_nor_vacation_dispatches_nothing_regardless_of_active_control(self):
        coord = make_coordinator()
        coord._is_summer = False
        coord._active_control = True
        rec = {"effective_target": 21.0}
        calls = await _run_maintenance(coord, _cfg(), rec)
        assert calls == []
        assert coord._maintenance_task is None


def _two_trv_cfg() -> dict:
    return {**_cfg(), "climate_entities": ["climate.trv", "climate.trv2"]}


def _summer_coord():
    coord = make_coordinator()
    coord._is_summer = True
    coord._active_control = True
    return coord


class TestValveMaintenanceLifecycle:
    """Review H4: the return is a regular control pass, never a captured target."""

    async def test_exercise_written_then_pass_requested(self):
        coord = _summer_coord()
        calls = await _run_maintenance(coord, _cfg(), {"effective_target": 21.0})
        assert [c["temperature"] for c in calls] == [28.0]
        assert coord.passes == [TRIGGER_MAINTENANCE]
        assert coord._last_maintenance == _SUNDAY_3AM
        assert coord._maintenance_running is False

    async def test_active_control_switched_off_during_exercise_no_write_no_pass(self, caplog):
        coord = _summer_coord()

        def _switch_off(c):
            c._active_control = False

        calls = await _run_maintenance(
            coord, _cfg(), {"effective_target": 21.0}, during_sleep=_switch_off)
        assert len(calls) == 1
        assert coord.passes == []
        assert "switched off during the valve exercise" in caplog.text

    async def test_all_writes_failed_not_marked_done(self, caplog):
        coord = _summer_coord()
        calls = await _run_maintenance(
            coord, _cfg(), {"effective_target": 21.0}, fail={"climate.trv"})
        assert calls == []
        assert coord._last_maintenance is None
        assert "valve exercise write failed for climate.trv" in caplog.text
        assert "failed for all TRVs" in caplog.text
        assert "finished" not in caplog.text
        # a pass is still requested in case the device partially applied it
        assert coord.passes == [TRIGGER_MAINTENANCE]

    async def test_partial_failure_marked_done_and_reported(self, caplog):
        coord = _summer_coord()
        set_states = {
            "climate.trv2": make_state("heat", {
                "temperature": 19.0, "min_temp": 5.0, "max_temp": 35.0,
                "target_temp_step": 0.5}),
        }
        orig = set_hass_states

        def _set(c, states):
            orig(c, {**states, **set_states})

        with patch(f"{__name__}.set_hass_states", _set):
            calls = await _run_maintenance(
                coord, _two_trv_cfg(), {"effective_target": 21.0}, fail={"climate.trv2"})
        assert [c["entity_id"] for c in calls] == ["climate.trv"]
        assert coord._last_maintenance == _SUNDAY_3AM
        assert "write failed for climate.trv2" in caplog.text
        assert "finished (1/2 TRVs)" in caplog.text

    async def test_no_eligible_trv_marks_week_done_without_writes(self):
        coord = _summer_coord()
        cfg = {**_cfg(), "device_settings": {"climate.trv": {"valve_maintenance": False}}}
        calls = await _run_maintenance(coord, cfg, {"effective_target": 21.0})
        assert calls == []
        assert coord._last_maintenance == _SUNDAY_3AM
        assert coord._maintenance_task is None
        assert coord.passes == []

    async def test_cancel_during_exercise_logs_and_resets(self, caplog):
        coord = _summer_coord()
        coord.passes = []
        coord.request_pass = coord.passes.append
        coord.hass.async_create_task = lambda coro: asyncio.ensure_future(coro)
        coord.hass.services.async_call = _noop_service
        set_hass_states(coord, {"climate.trv": make_state("heat", {
            "temperature": 19.0, "min_temp": 5.0, "max_temp": 35.0, "target_temp_step": 0.5})})
        coord._device_profiles = {}
        coord._maintenance_running = False
        coord._last_maintenance = None
        coord._clock = FakeClock(_SUNDAY_3AM)
        await coord._async_valve_maintenance(_cfg(), {"effective_target": 21.0})
        await asyncio.sleep(0)  # let the exercise write run into the real sleep
        await coord.async_cancel_maintenance()
        assert coord._maintenance_running is False
        assert coord.passes == []
        assert "valve exercise cancelled" in caplog.text


async def _noop_service(*_a, **_kw):
    return None
