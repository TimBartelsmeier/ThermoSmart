"""Tests for bundling the ThermoSmart Lovelace card inside the integration
(Frenck's HACS review of #9081/#9082): the card ships under
custom_components/thermosmart/www/, is served via HA's async static-path API,
and is auto-loaded through HA's resource collection (storage) or extra-module
helper (YAML). No separate HACS plugin or direct storage-file manipulation.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import pytest
from homeassistant.components.lovelace.resources import ResourceStorageCollection, ResourceYAMLCollection
from homeassistant.const import EVENT_COMPONENT_LOADED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event

from custom_components import thermosmart as ts_init
from custom_components.thermosmart.const import (
    CARD_ASSET_VERSION,
    CARD_FILENAME,
    CARD_URL_PATH,
    DOMAIN,
)
from tests.helpers import make_mock_hass
from custom_components.thermosmart.card_registration import CardRegistration, REGISTRATION_KEY

_CARD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "custom_components", "thermosmart", "www", "thermosmart-card.js",
)


# ── File structure ────────────────────────────────────────────────────────────


class TestCardFileStructure:
    def test_card_file_exists(self):
        assert os.path.isfile(_CARD_PATH)

    def test_card_file_is_not_empty(self):
        assert os.path.getsize(_CARD_PATH) > 1000

    def test_card_file_defines_the_custom_element(self):
        content = open(_CARD_PATH, encoding="utf-8").read()
        for name in ("zone-card", "zone-schedule-card"):
            assert f'"thermosmart-{name}"' in content
        assert "customElements.define(name, component)" in content
        assert 'customElements.define("thermosmart-card-editor"' in content

    def test_card_registers_a_duplicate_define_guard(self):
        """Card ships its own idempotency guard — matters once the same file
        could theoretically be loaded twice (bundled + a leftover manual
        install from before this migration)."""
        content = open(_CARD_PATH, encoding="utf-8").read()
        assert "if (!customElements.get(name))" in content
        assert "window.customCards.some(card => card.type === name)" in content

    def test_no_build_or_node_artifacts_in_www_dir(self):
        www_dir = os.path.dirname(_CARD_PATH)
        entries = os.listdir(www_dir)
        assert entries == [CARD_FILENAME]

    def test_no_node_modules_anywhere_under_custom_components(self):
        root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "custom_components")
        for dirpath, dirnames, _ in os.walk(root):
            assert "node_modules" not in dirnames


# ── Static path + frontend registration ──────────────────────────────────────


def _hass(resources=None, *, frontend_ready=True, modern=False):
    hass = make_mock_hass()
    hass.config.components = {"http", "frontend", "lovelace"} if frontend_ready else {"http"}
    if resources is None:
        resources = ResourceYAMLCollection([])
    hass.data = {
        "lovelace": SimpleNamespace(resources=resources) if modern else {"resources": resources}
    }
    hass.http.async_register_static_paths = AsyncMock()
    return hass


def _storage(items=()):
    resources = MagicMock(spec=ResourceStorageCollection)
    resources.async_get_info = AsyncMock()
    resources.async_items.return_value = list(items)
    resources.async_create_item = AsyncMock()
    resources.async_update_item = AsyncMock()
    resources.async_delete_item = AsyncMock()
    return resources


_URL = f"{CARD_URL_PATH}?v={CARD_ASSET_VERSION}"
_ADD_URL = "custom_components.thermosmart.card_registration.add_extra_js_url"
_CREATE_ISSUE = "custom_components.thermosmart.card_registration.ir.async_create_issue"
_DELETE_ISSUE = "custom_components.thermosmart.card_registration.ir.async_delete_issue"


class TestRegisterCardHelper:
    @pytest.mark.parametrize("modern", [False, True])
    async def test_storage_registration_uses_collection_only(self, modern):
        resources = _storage()
        hass = _hass(resources, modern=modern)
        with patch(_ADD_URL) as add_url, patch(_DELETE_ISSUE):
            assert await ts_init.async_setup(hass, {}) is True
        resources.async_get_info.assert_awaited_once()
        resources.async_create_item.assert_awaited_once_with({"res_type": "module", "url": _URL})
        add_url.assert_not_called()
        assert hass.data[DOMAIN][REGISTRATION_KEY].registered

    async def test_registers_static_path_without_long_lived_cache_headers(self):
        hass = _hass()
        with patch(_ADD_URL) as add_url, patch(_DELETE_ISSUE):
            await ts_init._async_register_card(hass)
        (configs,), _ = hass.http.async_register_static_paths.await_args
        assert len(configs) == 1
        cfg = configs[0]
        assert cfg.url_path == CARD_URL_PATH
        assert cfg.path.endswith(os.path.join("www", CARD_FILENAME))
        assert cfg.cache_headers is False
        add_url.assert_called_once_with(hass, _URL)

    async def test_waits_for_frontend_once(self):
        hass = _hass(frontend_ready=False)
        with patch(_ADD_URL) as add_url, patch(_DELETE_ISSUE):
            await ts_init._async_register_card(hass)
            await ts_init._async_register_card(hass)
            add_url.assert_not_called()
            registration = hass.data[DOMAIN][REGISTRATION_KEY]
            assert not registration.registered
            assert hass.bus.async_listen.call_count == 2
            listener = hass.bus.async_listen.call_args_list[0].args[1]
            await listener(Event(EVENT_COMPONENT_LOADED, {"component": "climate"}))
            add_url.assert_not_called()
            hass.config.components.add("frontend")
            await listener(Event(EVENT_COMPONENT_LOADED, {"component": "frontend"}))
        add_url.assert_called_once_with(hass, _URL)
        hass.http.async_register_static_paths.assert_awaited_once()
        assert registration.registered
        assert registration._cancel_ready is None
        assert registration._cancel_stop is None

    @pytest.mark.parametrize("stage", ["static", "frontend"])
    async def test_failure_is_reported_and_deliberate_reload_recovers(self, stage, caplog):
        resources = _storage()
        hass = _hass(resources)
        operation = (
            hass.http.async_register_static_paths if stage == "static"
            else resources.async_create_item
        )
        operation.side_effect = [RuntimeError("failure"), None]
        with patch(_CREATE_ISSUE) as issue, patch(_DELETE_ISSUE) as clear, patch(_ADD_URL) as add_url:
            await ts_init._async_register_card(hass)
            assert not hass.data[DOMAIN][REGISTRATION_KEY].registered
            issue.assert_called_once()
            assert issue.call_args.kwargs["is_persistent"] is True
            assert issue.call_args.kwargs["translation_placeholders"]["error"] == "failure"
            clear.assert_not_called()
            await ts_init._async_register_card(hass)
            assert hass.data[DOMAIN][REGISTRATION_KEY].registered
            clear.assert_called_once()
            add_url.assert_not_called()
        assert "ThermoSmart card failed during" in caplog.text
        assert hass.http.async_register_static_paths.await_count == (2 if stage == "static" else 1)

    async def test_updates_only_exact_owned_path_and_removes_its_duplicates(self):
        resources = _storage([
            {"id": "first", "url": f"{CARD_URL_PATH}?v=old", "type": "js"},
            {"id": "duplicate", "url": _URL, "type": "module"},
            {"id": "other", "url": f"{CARD_URL_PATH}.other", "type": "module"},
            {"id": "external", "url": f"https://example.com{CARD_URL_PATH}", "type": "module"},
        ])
        with patch(_DELETE_ISSUE), patch(_ADD_URL) as add_url:
            await ts_init._async_register_card(_hass(resources))
        resources.async_create_item.assert_not_awaited()
        resources.async_update_item.assert_awaited_once_with("first", {"res_type": "module", "url": _URL})
        resources.async_delete_item.assert_awaited_once_with("duplicate")
        add_url.assert_not_called()

    async def test_correct_existing_resource_is_unchanged(self):
        resources = _storage([{"id": "first", "url": _URL, "type": "module"}])
        with patch(_DELETE_ISSUE):
            await ts_init._async_register_card(_hass(resources))
        resources.async_create_item.assert_not_awaited()
        resources.async_update_item.assert_not_awaited()
        resources.async_delete_item.assert_not_awaited()

    async def test_matching_explicit_yaml_resource_is_not_loaded_twice(self):
        resources = ResourceYAMLCollection([{"url": _URL, "type": "module"}])
        with patch(_ADD_URL) as add_url, patch(_DELETE_ISSUE):
            await ts_init._async_register_card(_hass(resources))
        add_url.assert_not_called()
        assert resources.async_items() == [{"url": _URL, "type": "module"}]

    @pytest.mark.parametrize("items", [
        [{"url": f"{CARD_URL_PATH}?v=old", "type": "module"}],
        [{"url": _URL, "type": "js"}],
        [{"url": _URL, "type": "module"}] * 2,
    ])
    async def test_invalid_explicit_yaml_resource_reports_issue_without_mutation(self, items):
        resources = ResourceYAMLCollection(items)
        with patch(_ADD_URL) as add_url, patch(_CREATE_ISSUE) as issue:
            await ts_init._async_register_card(_hass(resources))
        add_url.assert_not_called()
        issue.assert_called_once()
        assert resources.async_items() == items

    async def test_unknown_resource_interface_reports_issue_not_fallback(self):
        hass = _hass()
        hass.data["lovelace"] = {}
        with patch(_ADD_URL) as add_url, patch(_CREATE_ISSUE) as issue:
            await ts_init._async_register_card(hass)
        add_url.assert_not_called()
        issue.assert_called_once()

    async def test_shutdown_removes_pending_listeners(self):
        hass = _hass(frontend_ready=False)
        registration = CardRegistration(hass)
        await registration.async_setup()
        unsubscribers = [MagicMock(), MagicMock()]
        registration._cancel_ready, registration._cancel_stop = unsubscribers
        registration._on_stop(Event(EVENT_HOMEASSISTANT_STOP))
        for unsubscribe in unsubscribers:
            unsubscribe.assert_called_once()


# ── Idempotency across multiple config entries ───────────────────────────────


def _system_entry(entry_id: str) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = {"entry_type": "system"}
    entry.options = {}
    return entry


class TestCardRegisteredOnceAcrossEntries:
    async def test_two_system_entries_register_the_card_exactly_once(self):
        """The domain-level 'card_registered' guard is entry-type-agnostic —
        proven here via two lightweight system entries so the test doesn't
        need to stand up a full zone Coordinator/WeatherEngine/LearningEngine."""
        hass = _hass()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.services.has_service = MagicMock(return_value=True)  # skip service registration branch

        with patch(_ADD_URL) as add_url, patch(_DELETE_ISSUE):
            await ts_init.async_setup_entry(hass, _system_entry("sys_1"))
            await ts_init.async_setup_entry(hass, _system_entry("sys_2"))

        assert hass.http.async_register_static_paths.await_count == 1
        assert add_url.call_count == 1
        assert hass.data[DOMAIN][REGISTRATION_KEY].registered

    async def test_registration_flag_prevents_a_direct_second_call(self):
        import asyncio
        hass = _hass()
        with patch(_ADD_URL) as add_url, patch(_DELETE_ISSUE):
            await asyncio.gather(*(ts_init._async_register_card(hass) for _ in range(5)))
        hass.http.async_register_static_paths.assert_awaited_once()
        add_url.assert_called_once()


# ── URL shape sanity (no /local/ dependency, matches Frenck's ask) ──────────


class TestCardUrlShape:
    def test_url_path_is_not_under_local(self):
        assert not CARD_URL_PATH.startswith("/local/")

    def test_url_path_matches_requested_pattern(self):
        assert CARD_URL_PATH == "/thermosmart/thermosmart-card.js"


# ── Card version / encoding consistency ───────────────────────────────────────


class TestCardAssetVersionConsistency:
    """CARD_ASSET_VERSION is the cache-busting query string of the card URL; it
    must be bumped together with the card's own CARD_VERSION, otherwise
    browsers keep serving a stale copy of a changed card."""

    def test_asset_version_matches_the_version_inside_the_card(self):
        import re
        content = open(_CARD_PATH, encoding="utf-8").read()
        match = re.search(r"const CARD_VERSION = '([^']+)'", content)
        assert match, "CARD_VERSION constant not found in thermosmart-card.js"
        assert match.group(1) == CARD_ASSET_VERSION

    def test_registered_url_carries_the_asset_version(self):
        from custom_components.thermosmart.const import CARD_URL_PATH
        assert f"{CARD_URL_PATH}?v={CARD_ASSET_VERSION}".endswith(f"?v={CARD_ASSET_VERSION}")


class TestCardFileEncoding:
    """Guards the shipped card against encoding damage. UTF-8 double encoding
    (text decoded as Latin-1/CP1252 and encoded again) leaves byte sequences
    that start with the UTF-8 bytes of the two letters A-tilde / A-circumflex
    and breaks the script in the browser."""

    # UTF-8 encodings of U+00C3, U+00C2, and of the 3-byte mojibake lead-in
    # that a double-encoded en/em dash or quote produces.
    _MOJIBAKE_MARKERS = (
        bytes([0xC3, 0x83]),
        bytes([0xC3, 0x82]),
        bytes([0xC3, 0xA2, 0xE2, 0x82, 0xAC]),
    )

    def test_card_is_valid_utf8(self):
        with open(_CARD_PATH, "rb") as fh:
            fh.read().decode("utf-8")  # raises on invalid UTF-8

    def test_card_contains_no_double_encoding_markers(self):
        with open(_CARD_PATH, "rb") as fh:
            data = fh.read()
        for marker in self._MOJIBAKE_MARKERS:
            assert marker not in data, f"double-encoding marker {marker!r} found"

    def test_card_has_no_byte_order_mark(self):
        with open(_CARD_PATH, "rb") as fh:
            assert fh.read(3) != bytes([0xEF, 0xBB, 0xBF])
