"""Integration tests for ThermoSmart _async_update_data (Welle 3).

Tests the indoor safety bypass (v1.1.1 fix) and the complete update loop
using a testable coordinator subclass that stubs out all TRV/maintenance
side-effects while keeping the coordinator's own logic intact.

Pure Python + MagicMock – runs on Windows without Docker.
"""
from __future__ import annotations

import asyncio

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.thermosmart.coordinator import ThermoSmartCoordinator

from tests.helpers import make_coordinator, make_state, set_hass_states


class _TestableCoordinator(ThermoSmartCoordinator):
    """Subclass with TRV/maintenance side-effects removed for integration testing.

    Only overrides external I/O methods; all coordinator logic (mode evaluation,
    recommendation pipeline, indoor safety bypass, etc.) runs unchanged.
    """


    async def _watchdog_hvac(self, cfg: dict, recommendation: dict) -> None:
        pass

    async def _async_calibrate_trvs(self, cfg: dict, recommendation: dict) -> None:
        pass

    async def _apply_temperature(self, cfg: dict, recommendation: dict) -> None:
        pass

    async def _async_set_valve_percent(self, cfg: dict, duty: float) -> None:
        pass

    async def _apply_frost_protection(self, cfg: dict) -> None:
        pass

    async def _async_valve_maintenance(self, cfg: dict, recommendation: dict) -> None:
        pass

    async def _async_write_external_temp(self, cfg: dict, recommendation: dict) -> None:
        pass

    async def _async_observe_trv_setpoints(
        self, cfg: dict, recommendation: dict, weather: dict
    ) -> None:
        pass

    def _check_boost_outcome(self, cfg: dict) -> None:
        pass

    def _check_heating_failure(self, recommendation: dict) -> None:
        pass


def _make_testable(zone_cfg_overrides: dict | None = None) -> _TestableCoordinator:
    """Create a _TestableCoordinator with mocked dependencies."""
    from unittest.mock import patch as _patch

    base = make_coordinator(zone_cfg_overrides)
    with _patch("homeassistant.helpers.frame.report_usage"):
        coord = _TestableCoordinator(
            base.hass, base.entry, base.outdoor, base.learning_engine
        )
    return coord


# ── TestIndoorSafetyBypass ────────────────────────────────────────────────────


class TestSummerNoIndoorBypass:
    async def test_summer_stays_summer_when_indoor_is_cold(self):
        """The former indoor safety bypass is removed: summer is never lifted
        automatically, regardless of indoor temperature."""
        coord = _make_testable()
        coord._is_summer = True
        set_hass_states(coord, {"sensor.test_temp": make_state("10.0")})

        result = await coord._async_update_data()
        assert result["zone"]["is_summer"] is True
        assert not hasattr(coord, "_indoor_safety_active")

    async def test_not_summer_stays_winter(self):
        coord = _make_testable()
        coord._is_summer = False
        set_hass_states(coord, {"sensor.test_temp": make_state("15.0")})

        result = await coord._async_update_data()
        assert result["zone"]["is_summer"] is False


# ── TestUpdateDataStructure ───────────────────────────────────────────────────


class TestNoOutdoorData:
    @pytest.mark.parametrize("learning", [True, False])
    async def test_cycles_work_without_any_outdoor_data(self, learning):
        coord = _make_testable({"learning_enabled": learning})
        coord.outdoor.read.return_value = {
            k: None for k in ("temperature", "humidity", "wind_speed",
                              "solar_radiation", "rain", "condition")
        }
        set_hass_states(coord, {"sensor.test_temp": make_state("19.0")})

        for _ in range(3):
            result = await coord._async_update_data()
            assert result["zone"]["effective_target"] is not None
            assert "error" not in result["zone"]


class TestUpdateDataStructure:
    async def test_update_data_returns_required_keys(self):
        coord = _make_testable()
        set_hass_states(coord, {"sensor.test_temp": make_state("20.0")})
        result = await coord._async_update_data()
        assert "weather" in result
        assert "zone" in result
        assert "active_control" in result
        assert "presence" not in result

    async def test_zone_contains_target_keys(self):
        coord = _make_testable()
        result = await coord._async_update_data()
        zone = result["zone"]
        for key in ("desired_target", "effective_target", "vacation_mode", "is_summer"):
            assert key in zone
        assert "adjusted_target" not in zone
        assert "mode" not in zone

    async def test_is_summer_field_present_in_zone(self):
        coord = _make_testable()
        result = await coord._async_update_data()
        assert "is_summer" in result["zone"]


# ── TestMaintenanceCancel ─────────────────────────────────────────────────────
# P1 fix regression: async_cancel_maintenance() must reliably stop an in-flight
# valve-maintenance task on unload/reload, never leak a task, and never raise.


class TestMaintenanceCancel:
    @staticmethod
    async def _start_fake_maintenance(coord) -> asyncio.Event:
        """Assign a real, cancellable asyncio Task to coord._maintenance_task.

        make_coordinator() uses a MagicMock hass, so hass.async_create_task()
        would return a MagicMock rather than a real Task — this bypasses that
        and exercises the actual cancel/await semantics of
        async_cancel_maintenance() directly, mirroring how maintenance.py's
        real _run_maintenance() sets/clears _maintenance_running in a finally.
        """
        started = asyncio.Event()

        async def _long_running() -> None:
            coord._maintenance_running = True
            try:
                started.set()
                await asyncio.sleep(10)
            finally:
                coord._maintenance_running = False

        coord._maintenance_task = asyncio.ensure_future(_long_running())
        await started.wait()
        return started

    async def test_cancel_stops_running_task_and_clears_flag(self):
        coord = make_coordinator()
        await self._start_fake_maintenance(coord)

        await coord.async_cancel_maintenance()

        assert coord._maintenance_task.done()
        assert coord._maintenance_task.cancelled()
        assert coord._maintenance_running is False

    async def test_cancel_is_noop_when_no_task(self):
        coord = make_coordinator()
        assert coord._maintenance_task is None

        await coord.async_cancel_maintenance()  # must not raise

    async def test_cancel_is_noop_when_task_already_done(self):
        coord = make_coordinator()

        async def _quick() -> None:
            return None

        coord._maintenance_task = asyncio.ensure_future(_quick())
        await coord._maintenance_task

        await coord.async_cancel_maintenance()  # must not raise; already done()

    async def test_repeated_cancel_call_is_noop_and_does_not_raise(self):
        coord = make_coordinator()
        await self._start_fake_maintenance(coord)

        await coord.async_cancel_maintenance()
        await coord.async_cancel_maintenance()  # second call: task already done

        assert coord._maintenance_task.cancelled()
        assert coord._maintenance_running is False


class TestObservationModeWritesNothing:
    async def test_external_temperature_not_written_in_observation_mode(self):
        coord = _make_testable()
        calls = []

        async def record(cfg, recommendation):
            calls.append(1)

        coord._async_write_external_temp = record
        coord._active_control = False
        set_hass_states(coord, {"sensor.test_temp": make_state("19.0")})
        await coord._async_update_data()
        assert calls == []
