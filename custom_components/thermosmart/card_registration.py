"""Session-wide serving and automatic loading of the bundled dashboard card."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import logging
from pathlib import Path
from urllib.parse import urlsplit

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.resources import (
    ResourceStorageCollection,
    ResourceYAMLCollection,
)
from homeassistant.const import EVENT_COMPONENT_LOADED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import CARD_ASSET_VERSION, CARD_FILENAME, CARD_URL_PATH, DOMAIN

_LOGGER = logging.getLogger(__name__)
REGISTRATION_KEY = "card_registration"
_ISSUE_ID = "card_registration_failed"


def _owns_url(url: str) -> bool:
    parsed = urlsplit(url)
    return not parsed.scheme and not parsed.netloc and parsed.path == CARD_URL_PATH


def _resources(hass: HomeAssistant) -> ResourceStorageCollection | ResourceYAMLCollection:
    # HA 2025.2 replaced the Lovelace data dictionary with a dataclass.
    data = hass.data.get("lovelace")
    resources = data.get("resources") if isinstance(data, Mapping) else getattr(data, "resources", None)
    if not isinstance(resources, (ResourceStorageCollection, ResourceYAMLCollection)):
        raise RuntimeError("Home Assistant's Lovelace resource collection is unavailable or unsupported")
    return resources


class CardRegistration:
    """Register once after frontend readiness, independently of zone lifetimes."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.url = f"{CARD_URL_PATH}?v={CARD_ASSET_VERSION}"
        self.static_registered = False
        self.registered = False
        self._lock = asyncio.Lock()
        self._cancel_ready: Callable[[], None] | None = None
        self._cancel_stop: Callable[[], None] | None = None

    async def async_setup(self) -> None:
        """Also allow deliberate entry reloads to recover a reported failure."""
        async with self._lock:
            if self.registered:
                return
            stage = "static file serving"
            try:
                if not self.static_registered:
                    path = Path(__file__).parent / "www" / CARD_FILENAME
                    await self.hass.http.async_register_static_paths([
                        StaticPathConfig(CARD_URL_PATH, str(path), False)
                    ])
                    self.static_registered = True

                # Listen before checking readiness so no transition can be missed.
                if self._cancel_ready is None:
                    self._cancel_ready = self.hass.bus.async_listen(
                        EVENT_COMPONENT_LOADED, self._async_component_loaded
                    )
                    self._cancel_stop = self.hass.bus.async_listen(
                        EVENT_HOMEASSISTANT_STOP, self._on_stop
                    )
                if "frontend" not in self.hass.config.components:
                    return
                self._stop_listening()
                stage = "frontend resource registration"
                await self._async_register_frontend()
            except Exception as err:
                # This optional UI boundary must not take heating offline.
                _LOGGER.exception("ThermoSmart card failed during %s", stage)
                ir.async_create_issue(
                    self.hass, DOMAIN, _ISSUE_ID,
                    is_fixable=False,
                    is_persistent=True,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=_ISSUE_ID,
                    translation_placeholders={"stage": stage, "error": str(err)},
                )
                return
            self.registered = True
            ir.async_delete_issue(self.hass, DOMAIN, _ISSUE_ID)

    async def _async_component_loaded(self, event: Event) -> None:
        if event.data["component"] == "frontend":
            await self.async_setup()

    @callback
    def _on_stop(self, _event: Event) -> None:
        self._stop_listening()

    @callback
    def _stop_listening(self) -> None:
        if self._cancel_ready is not None:
            self._cancel_ready()
            self._cancel_ready = None
        if self._cancel_stop is not None:
            self._cancel_stop()
            self._cancel_stop = None

    async def _async_register_frontend(self) -> None:
        from .dashboard import async_register_dashboard_commands

        async_register_dashboard_commands(self.hass)
        resources = _resources(self.hass)
        # async_get_info ensures storage is loaded, including on older HA versions.
        await resources.async_get_info()
        matches = [item for item in resources.async_items() if _owns_url(item["url"])]
        if isinstance(resources, ResourceStorageCollection):
            if not matches:
                await resources.async_create_item({"res_type": "module", "url": self.url})
                _LOGGER.info("Registered ThermoSmart dashboard resource: %s", self.url)
                return
            first, *duplicates = matches
            if first["url"] != self.url or first["type"] != "module":
                await resources.async_update_item(
                    first["id"], {"res_type": "module", "url": self.url}
                )
                _LOGGER.info("Updated ThermoSmart dashboard resource: %s", self.url)
            for duplicate in duplicates:
                await resources.async_delete_item(duplicate["id"])
                _LOGGER.warning("Removed duplicate ThermoSmart dashboard resource: %s", duplicate["url"])
            return

        if matches:
            if len(matches) != 1 or matches[0]["type"] != "module" or matches[0]["url"] != self.url:
                raise RuntimeError(
                    f"An explicit ThermoSmart YAML resource must be a single module at {self.url}. "
                    "Remove the explicit entry to use automatic loading, then restart Home Assistant."
                )
            _LOGGER.info("Using explicitly configured ThermoSmart YAML resource: %s", self.url)
            return
        add_extra_js_url(self.hass, self.url)
        _LOGGER.info("Registered ThermoSmart extra frontend module for YAML-managed resources: %s", self.url)


async def async_register_card(hass: HomeAssistant) -> None:
    data = hass.data.setdefault(DOMAIN, {})
    if REGISTRATION_KEY not in data:
        data[REGISTRATION_KEY] = CardRegistration(hass)
    await data[REGISTRATION_KEY].async_setup()
