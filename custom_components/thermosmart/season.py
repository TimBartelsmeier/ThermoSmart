"""Frost protection while summer mode holds the TRVs – ThermoSmart SeasonMixin."""
from __future__ import annotations

import asyncio
import logging

from .const import TEMP_FROST_PROTECTION
from .temperature_units import from_internal_temperature_c, to_internal_temperature_c

_LOGGER = logging.getLogger(__name__)


class SeasonMixin:
    """Frostschutz im Sommer (Sommer-Erkennung liegt im System-Entry, siehe system.py)."""

    async def _apply_frost_protection(self, cfg: dict) -> None:
        """Im Sommer: TRVs auf Frostschutztemperatur (12°C) setzen.

        Fehler eines einzelnen TRVs (z. B. ZigBee-Dropout nach dem
        Verfügbarkeits-Check) brechen den Frostschutz der übrigen TRVs
        nicht ab. Jede Exception wird mit der zugehörigen Entity-ID
        geloggt; der Coordinator bleibt weiterhin funktionsfähig.
        """
        tasks: list = []
        task_ids: list[str] = []

        for entity_id in cfg.get("climate_entities", []):
            await self._async_enforce_device_state_settings(entity_id, cfg)
            state = self.hass.states.get(entity_id)
            if not state or state.state in ("unavailable", "unknown"):
                continue
            current_setpoint = to_internal_temperature_c(self.hass, state.attributes.get("temperature"))
            if current_setpoint is not None and abs(current_setpoint - TEMP_FROST_PROTECTION) < 0.5:
                continue
            self._last_written_setpoints[entity_id] = TEMP_FROST_PROTECTION
            self._mark_trv_write(entity_id, cfg)
            tasks.append(self.hass.services.async_call(
                "climate", "set_temperature",
                {"entity_id": entity_id,
                 "temperature": from_internal_temperature_c(self.hass, TEMP_FROST_PROTECTION)},
                blocking=True,
            ))
            task_ids.append(entity_id)

        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for entity_id, result in zip(task_ids, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                _LOGGER.warning(
                    "ThermoSmart '%s': Frostschutz für %s fehlgeschlagen: %s",
                    self.zone_name, entity_id, result,
                )
