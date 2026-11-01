"""Tests for config flow validation helpers.

_NullableEntitySelector short-circuit paths (None / "") are also pure.
"""
from __future__ import annotations

import pytest

from custom_components.thermosmart.config_flow import _NullableEntitySelector
from homeassistant.helpers import selector


# ── _NullableEntitySelector ──────────────────────────────────────────────────

class TestNullableEntitySelector:
    """Test the None/empty short-circuit in _NullableEntitySelector.

    Only the None and empty-string paths are tested here since they bypass
    the HA entity_id validator — the valid entity path requires HA runtime state.
    """

    def _make_selector(self) -> _NullableEntitySelector:
        return _NullableEntitySelector(
            selector.EntitySelectorConfig(domain="weather")
        )

    def test_none_input_returns_none(self):
        sel = self._make_selector()
        assert sel(None) is None

    def test_empty_string_returns_none(self):
        sel = self._make_selector()
        assert sel("") is None

    def test_none_does_not_call_parent_validator(self):
        """Ensure None never reaches the HA entity_id validator (which would raise)."""
        sel = self._make_selector()
        # If super().__call__(None) were invoked it would raise TypeError/ValueError
        result = sel(None)
        assert result is None  # reached cleanly

    def test_empty_string_does_not_call_parent_validator(self):
        sel = self._make_selector()
        result = sel("")
        assert result is None
