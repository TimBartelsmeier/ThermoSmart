"""Control suspensions (summer, vacation, window open) and automatic summer detection.

A suspension is a situation in which the desired target is *not* applied to the TRVs.
The desired target keeps being updated meanwhile (events, user changes) and is applied
again as soon as the last suspension ends. Pure logic: no Home Assistant imports.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

from .const import TEMP_FROST_PROTECTION

SUMMER = "summer"
VACATION = "vacation"
WINDOW_OPEN = "window_open"


def resolve(*, summer: bool, vacation: bool, window_open: bool) -> str | None:
    """The suspension that decides what the TRVs do. Precedence: summer > vacation > window."""
    if summer:
        return SUMMER
    if vacation:
        return VACATION
    if window_open:
        return WINDOW_OPEN
    return None


def held_at(suspension: str | None, vacation_temp: float) -> float | None:
    """Temperature the TRVs are held at; None releases the setpoint (window) or no hold."""
    if suspension == SUMMER:
        return TEMP_FROST_PROTECTION
    if suspension == VACATION:
        return vacation_temp
    return None


class SummerDetector:
    """Detects summer from the average outdoor temperature over a rolling window.

    Hysteresis: summer starts at ``on_threshold`` or above and ends at ``off_threshold``
    or below; in between the previous state is kept. No decision is taken before the
    samples cover ``min_span`` (default: the full window), so a restart never flips the state on a few readings.
    """

    def __init__(
        self,
        on_threshold: float,
        off_threshold: float,
        *,
        window: timedelta = timedelta(hours=72),
        min_span: timedelta | None = None,
        summer: bool = False,
    ) -> None:
        if off_threshold >= on_threshold:
            raise ValueError("off_threshold must be below on_threshold")
        self.on_threshold = on_threshold
        self.off_threshold = off_threshold
        self.window = window
        self.min_span = window if min_span is None else min_span
        self.summer = summer
        self._samples: deque[tuple[datetime, float]] = deque()

    def add_sample(self, now: datetime, temperature: float) -> bool:
        """Record a reading and return the (possibly updated) summer state."""
        self._samples.append((now, temperature))
        self._prune(now)
        average, span = self.average, self.span
        if average is not None and span is not None and span >= self.min_span:
            if average >= self.on_threshold:
                self.summer = True
            elif average <= self.off_threshold:
                self.summer = False
        return self.summer

    def _prune(self, now: datetime) -> None:
        # Keep the newest sample at or before the window start, so the covered span
        # can actually reach the full window despite the sampling interval.
        while len(self._samples) >= 2 and now - self._samples[1][0] >= self.window:
            self._samples.popleft()

    @property
    def average(self) -> float | None:
        if not self._samples:
            return None
        return sum(t for _, t in self._samples) / len(self._samples)

    @property
    def span(self) -> timedelta | None:
        if len(self._samples) < 2:
            return None
        return self._samples[-1][0] - self._samples[0][0]

    @property
    def sample_count(self) -> int:
        return len(self._samples)
