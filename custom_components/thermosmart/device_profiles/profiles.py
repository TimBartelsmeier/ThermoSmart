"""ThermoSmart device profile definitions.

All known device profiles are defined here as frozen DeviceProfile instances.
Adding support for a new TRV model means adding an entry here and a
corresponding match rule in registry.py — no other files need to change.
"""
from __future__ import annotations

from ..const import AUTO_QUIRK_PATTERNS
from .capabilities import (
    DevicePreset,
    DeviceProfile,
    SETPOINT_CLIMATE,
    SETPOINT_HVAC_FIRST,
    VALVE_READ_ONLY,
    VALVE_MAX_LIMIT,
)

# ── Generic fallback ─────────────────────────────────────────────────────────

GENERIC = DeviceProfile(
    identifier="generic",
    display_name="Generic TRV",
)

# Generic platform-class fallbacks — not in the match table; available for
# future lookup when the detection layer can determine protocol/platform.

GENERIC_ZIGBEE_TRV = DeviceProfile(
    identifier="generic_zigbee_trv",
    display_name="Generic Zigbee TRV",
    burst_guard_seconds=8.0,
)

GENERIC_HOMEKIT_TRV = DeviceProfile(
    identifier="generic_homekit_trv",
    display_name="Generic HomeKit TRV",
    valve_semantics=VALVE_READ_ONLY,
    allow_external_temp_input=False,
    calibration_sync=False,
)

# ── Named profiles ───────────────────────────────────────────────────────────

SONOFF_TRVZB = DeviceProfile(
    identifier="sonoff_trvzb",
    display_name="SONOFF TRVZB",
    valve_semantics=VALVE_MAX_LIMIT,
    quirk_entities=("external_temperature_input",),
)

TUYA_TS0601 = DeviceProfile(
    identifier="tuya_ts0601",
    display_name="Tuya TS0601",
)

MOES_TV02 = DeviceProfile(
    identifier="moes_tv02",
    display_name="Moes TV02 (Zigbee)",
)

EVE_THERMO = DeviceProfile(
    identifier="eve_thermo",
    display_name="Eve Thermo (HomeKit / SEA80x)",
    valve_semantics=VALVE_READ_ONLY,
    valve_bump_on_close=False,
    allow_external_temp_input=False,
    calibration_sync=False,
    minimum_setpoint=7.0,
    # Valve position is exposed via HomeKit as a read-only sensor.
    # ThermoSmart reads it for learning; direct writes are not possible.
    # minimum_setpoint matches the HomeKit-reported min_temp for Eve Thermo devices.
    # Guards against protocol stacks (e.g. some Matter bridges) that omit min_temp.
)

# Alias — same profile, both names accepted in code.
EVE_HOMEKIT = EVE_THERMO

AQARA_TRV = DeviceProfile(
    identifier="aqara_trv",
    display_name="Aqara TRV",
    quirk_entities=("child_lock",),
)

EUROTRONIC_SPIRIT = DeviceProfile(
    identifier="eurotronic_spirit",
    display_name="Eurotronic Spirit",
    setpoint_method=SETPOINT_HVAC_FIRST,
    hvac_mode_before_write="heat",
    setpoint_delay_seconds=0.5,
)

EUROTRONIC_SPZB0001 = DeviceProfile(
    identifier="eurotronic_spzb0001",
    display_name="Eurotronic SPZB0001",
    setpoint_method=SETPOINT_HVAC_FIRST,
    hvac_mode_before_write="heat",
    setpoint_delay_seconds=0.5,
)

DANFOSS_ALLY = DeviceProfile(
    identifier="danfoss_ally",
    display_name="Danfoss Ally",
    quirk_entities=("load_balancing_enable",),
)

BOSCH_BTH = DeviceProfile(
    identifier="bosch_bth",
    display_name="Bosch BTH-RA",
)

INNR_COZB0001 = DeviceProfile(
    identifier="innr_cozb0001",
    display_name="Innr COZB001",
)

AVATTO_ME167 = DeviceProfile(
    identifier="avatto_me167",
    display_name="Avatto ME167",
    calibration_inverted=True,
)

BECA_BHT_002 = DeviceProfile(
    identifier="beca_bht_002",
    display_name="Beca BHT-002 (Zigbee)",
    # Some firmware versions report calibration offsets with incorrect sign or
    # implausibly large magnitude. ThermoSmart's standard plausibility checks
    # apply; no active workaround is implemented at this stage.
    valve_control_uncertain=True,
    calibration_plausibility_limit=3.0,
)

TADO = DeviceProfile(
    identifier="tado",
    display_name="tado°",
    is_active=False,
    hvac_watchdog=False,
    allow_external_temp_input=False,
    calibration_sync=False,
    warning=(
        "tado° devices rely on the tado° cloud API and cannot be controlled "
        "directly via a local climate entity. ThermoSmart can observe tado° "
        "entities in read-only mode but will not write setpoints. Use the "
        "official tado° integration for scheduling and control."
    ),
)

DAIKIN_CLIMATE = DeviceProfile(
    identifier="daikin_climate",
    display_name="Daikin Climate",
    is_active=False,
    hvac_watchdog=False,
    allow_external_temp_input=False,
    calibration_sync=False,
    warning=(
        "Daikin climate devices are heat pumps or multi-split systems, not "
        "radiator TRVs. ThermoSmart's TPI control and heating-rate learning "
        "target single-zone radiator heating and are not designed for this "
        "device class. The device can be observed but active setpoint control "
        "by ThermoSmart is not recommended."
    ),
)

NETATMO = DeviceProfile(
    identifier="netatmo",
    display_name="Netatmo Smart Radiator Valve",
    is_active=False,
    hvac_watchdog=False,
    allow_external_temp_input=False,
    calibration_sync=False,
    warning=(
        "Netatmo devices rely on cloud connectivity for their primary control "
        "path. Local Home Assistant entity availability varies by setup. "
        "ThermoSmart can observe Netatmo entities but setpoint writes may not "
        "be reliable. Verify that local control is available and stable before "
        "enabling Active Control."
    ),
)

# User-facing recommendations. The profile remains the runtime capability
# record; these presets only seed editable zone configuration.
DEVICE_PRESETS: dict[str, DevicePreset] = {
    "generic": DevicePreset(
        "generic", "Generic TRV", info_summary="Generic device; available inputs are auto-detected."
    ),
    "sonoff_trvzb": DevicePreset(
        "sonoff_trvzb", "SONOFF TRVZB", recommended_mode="tpi_boost",
        ext_temp_entity_pattern="external_temperature",
        feed_external_temp=True,
        calibration_sync=False,
        quirk_entity_patterns=AUTO_QUIRK_PATTERNS,
        default_state_settings=[{
            "entity_pattern": "temperature_sensor",
            "value": "external",
        }],
        info_summary="Preset loaded: external room temperature enabled and the temperature source forced to external (so no calibration offset is written); the TRV's own window detection, child lock, frost protection and schedule are switched off; valve opening degree is treated as a limit.",
    ),
    "eurotronic_spirit": DevicePreset(
        "eurotronic_spirit", "Eurotronic Spirit", recommended_mode="direct_valve",
        valve_entity_pattern="valve_position", hvac_mode_before_write="heat",
        setpoint_delay_seconds=0.5,
        quirk_entity_patterns=AUTO_QUIRK_PATTERNS,
        info_summary="Preset loaded: direct valve control via valve position; heat mode is selected before setpoint writes.",
    ),
    "eurotronic_spzb0001": DevicePreset(
        "eurotronic_spzb0001", "Eurotronic SPZB0001", recommended_mode="direct_valve",
        valve_entity_pattern="valve_position", hvac_mode_before_write="heat",
        setpoint_delay_seconds=0.5,
        quirk_entity_patterns=AUTO_QUIRK_PATTERNS,
        info_summary="Preset loaded: direct valve control via valve position; heat mode is selected before setpoint writes.",
    ),
    "avatto_me167": DevicePreset(
        "avatto_me167", "Avatto ME167", recommended_mode="tpi_boost",
        calibration_inverted=True,
        quirk_entity_patterns=AUTO_QUIRK_PATTERNS,
        info_summary="Preset loaded: calibration offset inversion enabled.",
    ),
    "tuya_ts0601": DevicePreset(
        "tuya_ts0601", "Tuya TS0601", recommended_mode="tpi_boost",
        valve_entity_pattern="heating_demand",
        quirk_entity_patterns=AUTO_QUIRK_PATTERNS,
        info_summary="Preset loaded: TPI setpoint modulation; detected heating-demand input can be configured for direct control.",
    ),
    "eve_thermo": DevicePreset(
        "eve_thermo", "Eve Thermo", recommended_mode="tpi_boost",
        calibration_sync=False,
        info_summary="Preset loaded: TPI setpoint modulation; external temperature input is not assumed.",
    ),
}

# ── Registry of all profiles ─────────────────────────────────────────────────

ALL_PROFILES: tuple[DeviceProfile, ...] = (
    GENERIC,
    GENERIC_ZIGBEE_TRV,
    GENERIC_HOMEKIT_TRV,
    SONOFF_TRVZB,
    TUYA_TS0601,
    MOES_TV02,
    EVE_THERMO,
    AQARA_TRV,
    EUROTRONIC_SPIRIT,
    EUROTRONIC_SPZB0001,
    DANFOSS_ALLY,
    BOSCH_BTH,
    INNR_COZB0001,
    AVATTO_ME167,
    BECA_BHT_002,
    TADO,
    DAIKIN_CLIMATE,
    NETATMO,
)
