"""Desired target temperature of a zone – the single source of truth.

A zone has exactly one desired target. The user, a preset, a physical TRV dial
or (later) a schedule event may change it; the last writer wins. Everything else
(suspensions, pre-heating, TRV boost) derives what the TRVs are driven to from
it and never rewrites it.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from .const import (
    CONF_ECO_TEMP,
    CONF_VACATION_TEMP,
    PRESET_AWAY,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NIGHT,
    PRESET_VACATION,
    TEMP_AWAY,
    TEMP_COMFORT,
    TEMP_ECO,
    TEMP_FROST_PROTECTION,
    TEMP_NIGHT,
)

SOURCE_USER = "user"
SOURCE_PRESET = "preset"
SOURCE_DIAL = "dial"
SOURCE_EVENT = "event"
SOURCE_RESTORE = "restore"
SOURCE_DEFAULT = "default"

# Sources that count as a manual intervention (they cancel pre-heating and feed
# the learning "manual override" counters).
MANUAL_SOURCES = frozenset({SOURCE_USER, SOURCE_PRESET, SOURCE_DIAL})

TARGET_MIN_C = 5.0
TARGET_MAX_C = 30.0

_PRESET_CONFIG: dict[str, tuple[str, float]] = {
    PRESET_COMFORT: ("comfort_temp", TEMP_COMFORT),
    PRESET_ECO: (CONF_ECO_TEMP, TEMP_ECO),
    PRESET_NIGHT: ("night_temp", TEMP_NIGHT),
    PRESET_AWAY: ("away_temp", TEMP_AWAY),
    PRESET_VACATION: (CONF_VACATION_TEMP, TEMP_FROST_PROTECTION),
}


def preset_temperature(cfg: Mapping, preset: str) -> float:
    """Temperature configured for a preset in the zone config."""
    key, default = _PRESET_CONFIG[preset]
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class DesiredTarget:
    value: float
    source: str
    preset: str | None = None
    changed_at: datetime | None = None


TargetListener = Callable[[DesiredTarget, DesiredTarget], None]


class TargetState:
    """Holds the desired target and notifies listeners about every change.

    Listeners receive ``(new, previous)``. A write that does not change the
    value or the preset is a no-op and does not notify.
    """

    def __init__(self, initial: float = TEMP_COMFORT, preset: str | None = PRESET_COMFORT) -> None:
        self._current = DesiredTarget(self._validate(initial), SOURCE_DEFAULT, preset)
        self._listeners: list[TargetListener] = []

    @staticmethod
    def _validate(value: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as err:
            raise ValueError(f"invalid target temperature: {value!r}") from err
        if not math.isfinite(number) or not TARGET_MIN_C <= number <= TARGET_MAX_C:
            raise ValueError(
                f"target temperature {value!r} outside {TARGET_MIN_C}–{TARGET_MAX_C} °C"
            )
        return round(number, 2)

    @property
    def current(self) -> DesiredTarget:
        return self._current

    @property
    def value(self) -> float:
        return self._current.value

    @property
    def preset(self) -> str | None:
        return self._current.preset

    def add_listener(self, listener: TargetListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def set(
        self,
        value: float,
        source: str,
        *,
        preset: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Write the desired target. Returns True if value or preset changed.

        Raises ValueError for values outside the supported range.
        """
        number = self._validate(value)
        previous = self._current
        if abs(number - previous.value) < 1e-9 and preset == previous.preset:
            return False
        self._current = DesiredTarget(number, source, preset, now)
        for listener in list(self._listeners):
            listener(self._current, previous)
        return True

    def restore(self, value: float, preset: str | None) -> None:
        """Set the target after a restart without notifying listeners."""
        self._current = DesiredTarget(self._validate(value), SOURCE_RESTORE, preset)
