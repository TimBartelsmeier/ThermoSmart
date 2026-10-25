"""Constants for the ThermoSmart integration."""

DOMAIN = "thermosmart"
VERSION = "1.3.1"

# Bundled Lovelace card (custom_components/thermosmart/www/thermosmart-card.js).
# Registered once per HA session as a static path + frontend module URL —
# see async_setup_entry(). No separate HACS plugin install needed anymore.
CARD_FILENAME = "thermosmart-card.js"
CARD_URL_PATH = f"/{DOMAIN}/{CARD_FILENAME}"

# Config entry keys
CONF_CLIMATE_ENTITIES = "climate_entities"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_OUTDOOR_TEMP_SENSOR = "outdoor_temp_sensor"
CONF_OUTDOOR_HUMIDITY_SENSOR = "outdoor_humidity_sensor"
CONF_OUTDOOR_WIND_SENSOR = "outdoor_wind_sensor"
CONF_OUTDOOR_SOLAR_SENSOR = "outdoor_solar_sensor"
CONF_OUTDOOR_RAIN_SENSOR = "outdoor_rain_sensor"
CONF_LEARNING_ENABLED = "learning_enabled"
CONF_ZONE_CONTROL_MODE = "zone_control_mode"
CONF_CONTROL_INTERVAL = "control_interval"
CONF_TEMP_TOLERANCE = "temp_tolerance"
CONF_EXT_TEMP_THRESHOLD = "ext_temp_threshold"
CONF_TRV_SETPOINT_DEBOUNCE = "trv_setpoint_debounce"
CONF_TRV_SETPOINT_SETTLING = "trv_setpoint_settling"
CONF_DEVICE_SETTINGS = "device_settings"
CONF_DEV_CONTROL_MODE = "control_mode"
CONF_DEV_VALVE_ENTITY = "valve_entity"
CONF_DEV_FEED_EXT_TEMP = "feed_external_temp"
CONF_DEV_EXT_TEMP_ENTITY = "ext_temp_entity"
CONF_DEV_CALIBRATION_ENTITY = "calibration_entity"
CONF_DEV_CALIBRATION_SYNC = "calibration_sync"
CONF_DEV_CALIBRATION_INVERT = "calibration_invert"
CONF_DEV_VALVE_MAINTENANCE = "valve_maintenance"
CONF_DEV_WINDOW_OFF = "window_off"
CONF_DEV_ACCEPT_SETPOINT_CHANGES = "accept_setpoint_changes"
CONF_DEV_STATE_SETTINGS = "state_settings"
CONF_SCHEDULE_EVENTS = "schedule_events"
CONTROL_MODE_INHERIT = "inherit"
CONTROL_MODE_SETPOINT = "setpoint"
CONTROL_MODE_TPI_BOOST = "tpi_boost"
CONTROL_MODE_DIRECT_VALVE = "direct_valve"
CONTROL_MODES = (
    CONTROL_MODE_INHERIT,
    CONTROL_MODE_SETPOINT,
    CONTROL_MODE_TPI_BOOST,
    CONTROL_MODE_DIRECT_VALVE,
)

# Bekannte Switch-Muster die bei TRVs automatisch deaktiviert werden sollen
AUTO_QUIRK_PATTERNS = [
    "window_detection",   # Sonoff TRVZB, Danfoss – eigene Fenstererkennung
    "child_lock",         # Sperrt externe Befehle → ThermoSmart kann nichts setzen
    "frost_protection",   # Interner Frostschutz – kollidiert mit ThermoSmart
    "schedule",           # Internes Wochenprogramm – überschreibt externe Setpoints
]

# Muster für automatisch erkannte Kalibrierungs-Entities
# Als Liste damit mehrere Protokoll-Konventionen erkannt werden
AUTO_CALIBRATION_PATTERNS = [
    "local_temperature_calibration",   # Zigbee2MQTT (TRVZB, Danfoss, Tuya, ...)
    "temperature_offset",              # Homematic IP (hahomematic), some ZHA devices
    "temperature_calibration",         # some ZHA device quirks
]

# Muster für automatisch erkannte External-Temperature-Input-Entities (TRVZB)
AUTO_EXT_TEMP_PATTERNS = ["external_temperature_input", "external_temperature"]

# Patterns for direct valve position control (written with TPI duty-cycle 0–100%).
# valve_opening_degree is intentionally excluded: on SONOFF TRVZB (and similar devices)
# it configures the maximum opening limit, not the live valve position. Writing the TPI
# duty-cycle to it caps heating capacity instead of modulating it. Those devices are
# controlled via climate.set_temperature (setpoint boost) instead.
AUTO_VALVE_PATTERNS = [
    "valve_position",         # Eurotronic Spirit (SPZB0001) — direct motor position
    "pi_heating_demand",      # Tuya TS0601_3/5, some Danfoss — live heating demand
    "heating_demand",         # alternative name for pi_heating_demand
    "level",                  # Homematic IP via hahomematic (0.0–1.0 scaled to %)
]

# TPI-Regler Konstanten
TPI_COEF_INT_DEFAULT  = 0.6    # Duty-Cycle-Anteil pro °C Raumfehler (0→100%)
TPI_COEF_EXT_DEFAULT  = 0.01   # Duty-Cycle-Anteil pro °C Außentemperatur-Delta
# Central unit string for all Learning rate predictions (°C/h).
# Gate 4 in read_tpi_coefficients_safe() and all unit assertions use this constant.
UNIT_C_PER_H = "C/h"
TPI_MAX_BOOST_CELSIUS = 8.0    # max. °C über Zieltemp für Setpoint-Konvertierung
TPI_VALVE_BUMP_PCT    = 10     # % extra beim Schließen (TRVZB motor-workaround)
TPI_VALVE_BUMP_DELAY  = 5.0    # Sekunden zwischen Bump-Open und Zielwert

# Zeitplan-Konfiguration (HH:MM Strings)
CONF_VACATION_TEMP     = "vacation_temp"       # Urlaubstemperatur (Frostschutz)
CONF_WINDOW_OPEN_TEMP  = "window_open_temp"    # TRV-Setpoint bei geöffnetem Fenster (pro Zone)
CONF_ECO_TEMP          = "eco_temp"            # Eco-Preset Temperatur

VALVE_MAINTENANCE_HOUR = 3     # 03:00 Uhr nachts
VALVE_MAINTENANCE_WEEKDAY = 6  # Sonntag (0=Mo … 6=So)
VALVE_MAINTENANCE_BOOST_TEMP = 28.0  # °C für Vollöffnung
VALVE_MAINTENANCE_DURATION_SEC = 30        # Sekunden offen (Winter/Beobachtung)
VALVE_MAINTENANCE_DURATION_SUMMER_SEC = 8  # Sekunden offen (Sommer – kurze Übung)

DOMAIN_GLOBAL_SUMMER   = f"{DOMAIN}_global_summer"
DOMAIN_GLOBAL_VACATION = f"{DOMAIN}_global_vacation"

# Default values
DEFAULT_WEATHER_ENTITY = "weather.home"
DEFAULT_LEARNING_ENABLED = True
DEFAULT_SCAN_INTERVAL = 300  # seconds

# Temperatur-Standardwerte (für Fallbacks)
TEMP_COMFORT = 21.0
TEMP_NIGHT = 18.0
TEMP_AWAY = 17.0
TEMP_FROST_PROTECTION = 12.0
TEMP_ECO = 19.0
WINDOW_OPEN_SETPOINT = 5.0  # TRV-Setpoint bei geöffnetem Fenster


# Learning algorithm settings
LEARNING_MIN_SAMPLES = 5        # Mindest-Beobachtungen bevor Lernen aktiv wird
PREHEAT_MAX_MINUTES = 180       # maximale Vorheizzeit
PREHEAT_MIN_DELTA = 2.0         # °C Differenz ab der Vorheizung ausgelöst wird

# Prognose-Feedback-Loop
FORECAST_EVAL_HOURS = 5          # Stunden bis Prognose-Entscheidung ausgewertet wird
FORECAST_BIAS_MIN = 0.3          # Mindest-Vertrauen in Prognose (nie komplett ignorieren)
FORECAST_BIAS_MAX = 1.0          # Vollständiges Vertrauen (Standard / Cold-Start)
FORECAST_BIAS_LEARNING_RATE = 0.06  # Anpassungsrate pro Auswertung

# Solar-Gain-Erkennung (SunIntensitySensor)
SOLAR_GAIN_THRESHOLD_W = 300.0   # W/m² – unterhalb kein relevanter Solar-Wärmeeintrag
SOLAR_GAIN_MIN_OUTDOOR_C = 3.0   # °C – bei Kälte trotz Sonne keinen Abzug
SOLAR_GAIN_MAX_REDUCTION = 0.30  # max. 30 % Reduktion des Heizbedarfs durch Sonne

# Sommer/Winter-Erkennung
SUMMER_THRESHOLD = 18.0   # °C Außentemperatur über X Stunden → Sommer
WINTER_THRESHOLD = 15.0   # °C Außentemperatur unter X Stunden → Heizung an
SEASON_HOURS = 72         # Stunden die der Schwellwert überschritten sein muss
SEASON_SAMPLE_MINUTES = 15  # Abtastintervall der System-Außentemperatur
CONF_SUMMER_ON_THRESHOLD = "summer_on_threshold"    # System-Entry: 72 h-Ø ab hier → Sommer
CONF_SUMMER_OFF_THRESHOLD = "summer_off_threshold"  # System-Entry: 72 h-Ø bis hier → Winter

# Sensor-Noise-Filter
NOISE_FILTER_SPIKE_THRESHOLD = 4.0  # °C – Abweichung > Wert vom EMA → Messung ignorieren
NOISE_FILTER_EMA_ALPHA = 0.2        # EMA-Glättungsfaktor (kleiner = träger/stabiler)

# Presets: plain named temperatures (configured per zone)
PRESET_COMFORT  = "comfort"
PRESET_ECO      = "eco"
PRESET_NIGHT    = "night"
PRESET_AWAY     = "away"
PRESET_VACATION = "vacation"
PRESETS = [
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NIGHT,
    PRESET_AWAY,
    PRESET_VACATION,
]

# EMA-Zeitkonstante für 1h-Trend-Sensor (α ≈ 1-e^(-5min/60min))
EMA_1H_ALPHA = 0.08

# Slope-basierte Fenstererkennung (ohne Sensor)
# Aktiviert wenn KEINE window_sensors konfiguriert sind
WINDOW_SLOPE_THRESHOLD = 0.06   # °C/min – Abfallrate die auf offenes Fenster hindeutet (= 3.6°C/h)
WINDOW_SLOPE_EMA_ALPHA = 0.5    # EMA-Glättung der Slope (träger = weniger Fehlalarme)
WINDOW_SLOPE_MIN_POINTS = 2     # Mindest-Messpunkte unter Schwellwert bevor Alarm

# Heizungsausfall-Erkennung
HEATING_FAILURE_DELAY_MIN = 35      # Minuten fallender Temp trotz Heizbefehl bis zur Warnung
HEATING_FAILURE_SLOPE_THRESH = -0.01  # °C/min – negativer Trend trotz aktivem Heizbefehl
HEATING_FAILURE_CMD_DELTA = 2.0     # °C – Mindest-Delta Setpoint > Ist für "Heizbefehl aktiv"

# Dispatch-Fehler-Erkennung (Command-Fehler-Zähler)
DISPATCH_FAILURE_SUPPORT_EVENT_THRESHOLD = 5  # aufeinanderfolgende Fehlschläge → Support-Event


# Storage keys
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_learning_data"

# Platforms
PLATFORMS = ["climate", "sensor", "switch", "select", "datetime"]

# Icons
ICON_ZONE = "mdi:home-thermometer"
ICON_LEARNING = "mdi:brain"
ICON_PREHEAT = "mdi:timer-play"
