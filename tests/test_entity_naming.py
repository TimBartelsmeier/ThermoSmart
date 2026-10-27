"""Entity names and unique ids stay stable and match the documented vocabulary."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from custom_components.thermosmart.select import ThermoSmartModeSelect
from custom_components.thermosmart.sensor import ThermoSmartTRVSetpointSensor

_STRINGS = Path(__file__).parent.parent / "custom_components" / "thermosmart" / "strings.json"


def test_trv_setpoint_sensor_is_named_tpi_boosted():
    sensors = json.loads(_STRINGS.read_text("utf-8"))["entity"]["sensor"]
    assert sensors["trv_setpoint"]["name"] == "TPI boosted TRV setpoint"


def test_unique_ids():
    entry = MagicMock(entry_id="abc", data={"name": "Z"})
    assert ThermoSmartModeSelect(MagicMock(), entry)._attr_unique_id == "abc_preset"
    assert ThermoSmartTRVSetpointSensor(MagicMock(), entry)._attr_unique_id == "abc_trv_setpoint"


def test_climate_range_matches_target_model():
    from custom_components.thermosmart.climate import ThermoSmartClimate
    from custom_components.thermosmart.target import TARGET_MAX_C, TARGET_MIN_C

    climate = ThermoSmartClimate(MagicMock(), MagicMock(entry_id="abc", data={"name": "Z"}))
    assert climate.min_temp == TARGET_MIN_C
    assert climate.max_temp == TARGET_MAX_C
