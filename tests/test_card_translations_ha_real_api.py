"""HA's native translation API serves card strings with English fallback."""
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.components.frontend import websocket_get_translations
from homeassistant.helpers import translation

from custom_components.thermosmart.const import DOMAIN

_PREFIX = f"component.{DOMAIN}.card."


@pytest.mark.parametrize(
    ("language", "expected"), [("en", "Heating"), ("de", "Heizen"), ("fr", "Heating")]
)
async def test_frontend_translation_api(hass, enable_custom_integrations, language, expected):
    connection = MagicMock()
    await websocket_get_translations.__wrapped__(hass, connection, {
        "id": 1, "language": language, "category": "card", "integration": [DOMAIN],
    })
    message = connection.send_message.call_args.args[0]
    assert message["success"]
    resources = message["result"]["resources"]
    assert resources[f"{_PREFIX}status.heating"] == expected
    assert resources[f"{_PREFIX}schedule.next_event"] == (
        "Nächstes Ereignis" if language == "de" else "Next event"
    )
    assert "{time}" in resources[f"{_PREFIX}schedule.suspend_until"]


async def test_missing_german_key_falls_back_to_english(
    hass, enable_custom_integrations, disable_translations_once
):
    load = translation._load_translations_files_by_language

    def incomplete_german(files):
        loaded = load(files)
        del loaded["de"][DOMAIN]["card"]["preset"]["sleep"]
        return loaded

    with patch.object(translation, "_load_translations_files_by_language", incomplete_german):
        cache = translation._TranslationCache(hass)
        resources = await cache.async_fetch("de", "card", {DOMAIN})
    assert resources[f"{_PREFIX}preset.sleep"] == "Night"
    assert resources[f"{_PREFIX}preset.comfort"] == "Komfort"
