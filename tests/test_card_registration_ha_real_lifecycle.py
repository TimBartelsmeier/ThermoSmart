"""Exercise automatic card registration with HA's actual bus and collections."""
from unittest.mock import AsyncMock, patch
from aiohttp.resolver import ThreadedResolver

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.components.lovelace.dashboard import LovelaceStorage
from homeassistant.components.lovelace.resources import ResourceStorageCollection, ResourceYAMLCollection
from homeassistant.const import (
    EVENT_COMPONENT_LOADED,
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.thermosmart.card_registration import CardRegistration
from custom_components.thermosmart.const import CARD_ASSET_VERSION, CARD_URL_PATH, DOMAIN

_URL = f"{CARD_URL_PATH}?v={CARD_ASSET_VERSION}"


async def test_real_storage_collection_persists_resource_and_preserves_others(hass, hass_storage):
    hass_storage["lovelace_resources"] = {
        "version": 1,
        "key": "lovelace_resources",
        "data": {"items": [
            {"id": "thermosmart", "url": f"{CARD_URL_PATH}?v=old", "type": "js"},
            {"id": "other", "url": "/local/other.js", "type": "module"},
        ]},
    }
    resources = ResourceStorageCollection(hass, LovelaceStorage(hass, None))
    hass.data["lovelace"] = {"resources": resources}
    hass.config.components.add("frontend")
    registration = CardRegistration(hass)
    with patch.object(registration.hass, "http", create=True) as http:
        http.async_register_static_paths = AsyncMock()
        await registration.async_setup()
        await registration.async_setup()
        http.async_register_static_paths.assert_awaited_once()
    assert registration.registered
    assert resources.async_items() == [
        {"id": "thermosmart", "url": _URL, "type": "module"},
        {"id": "other", "url": "/local/other.js", "type": "module"},
    ]
    # HA flushes pending collection writes at shutdown.
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert hass_storage["lovelace_resources"]["data"]["items"] == resources.async_items()
    restored = ResourceStorageCollection(hass, LovelaceStorage(hass, None))
    await restored.async_get_info()
    assert restored.async_items() == resources.async_items()
    assert DATA_EXTRA_MODULE_URL not in hass.data


async def test_real_bus_readiness_and_yaml_reload_keep_automatic_module(hass):
    hass.data["lovelace"] = {"resources": ResourceYAMLCollection([])}
    hass.data[DATA_EXTRA_MODULE_URL] = set()
    registration = CardRegistration(hass)
    with patch.object(registration.hass, "http", create=True) as http:
        http.async_register_static_paths = AsyncMock()
        await registration.async_setup()
        assert not registration.registered
        hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "climate"})
        await hass.async_block_till_done()
        assert hass.data[DATA_EXTRA_MODULE_URL] == set()
        hass.config.components.add("frontend")
        hass.bus.async_fire(EVENT_COMPONENT_LOADED, {"component": "frontend"})
        await hass.async_block_till_done()
        assert registration.registered
        assert hass.data[DATA_EXTRA_MODULE_URL] == {_URL}
        # YAML resource reload replaces the collection, not extra-module URLs.
        hass.data["lovelace"]["resources"] = ResourceYAMLCollection([
            {"url": "/local/other.js", "type": "module"}
        ])
        await registration.async_setup()
        assert hass.data[DATA_EXTRA_MODULE_URL] == {_URL}
        http.async_register_static_paths.assert_awaited_once()
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()


async def test_real_repairs_failure_and_recovery(hass):
    hass.data["lovelace"] = {"resources": ResourceYAMLCollection([])}
    hass.data[DATA_EXTRA_MODULE_URL] = set()
    hass.config.components.add("frontend")
    registration = CardRegistration(hass)
    with patch.object(registration.hass, "http", create=True) as http:
        http.async_register_static_paths = AsyncMock(side_effect=[RuntimeError("test failure"), None])
        await registration.async_setup()
        registry = ir.async_get(hass)
        issue = registry.async_get_issue(DOMAIN, "card_registration_failed")
        assert issue is not None
        assert issue.is_persistent
        assert issue.translation_placeholders["error"] == "test failure"
        await registration.async_setup()
        assert registry.async_get_issue(DOMAIN, "card_registration_failed") is None
        assert registration.registered


async def test_real_shutdown_before_readiness_removes_listeners(hass):
    registration = CardRegistration(hass)
    with patch.object(registration.hass, "http", create=True) as http:
        http.async_register_static_paths = AsyncMock()
        await registration.async_setup()
        assert registration._cancel_ready is not None
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert registration._cancel_ready is None
        assert registration._cancel_stop is None


async def test_real_http_serves_bundle_at_versioned_url(hass, hass_client_no_auth):
    # Avoid pycares' independent shutdown thread in this local HTTP-only test.
    with patch("aiohttp.connector.DefaultResolver", ThreadedResolver):
        assert await async_setup_component(hass, "http", {})
        registration = CardRegistration(hass)
        await registration.async_setup()
        client = await hass_client_no_auth()
        response = await client.get(_URL)
        assert response.status == 200
        assert "javascript" in response.headers["Content-Type"]
        assert "Cache-Control" not in response.headers
        assert '"thermosmart-zone-card"' in await response.text()
        registration._stop_listening()
