"""Guard: removed legacy concepts must not creep back into the control code."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "thermosmart"

_FORBIDDEN = [
    r"HEATING_MODE_",
    r"\badjusted_target\b",
    r"\bbase_target\b",
    r"\boverride_active\b",
    r"\b_override\b",
    r"CONF_PRESENCE_",
    r"CONF_HOME_ZONE",
    r"CONF_SCHEDULE_ENABLED",
    r"\bpresence\b",
    r"forecast_suppression",
    r"weather_engine",
]

# The learning package keeps its own historical vocabulary (e.g. stored schemas).
_SKIP_PARTS = {"learning", "__pycache__", "translations"}


def _source_files():
    for path in ROOT.rglob("*.py"):
        if not _SKIP_PARTS.intersection(path.relative_to(ROOT).parts):
            yield path


def test_no_legacy_identifiers_in_control_code():
    hits = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for pattern in _FORBIDDEN:
            for m in re.finditer(pattern, text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{path.name}:{line}: {pattern}")
    assert not hits, "legacy identifiers found:\n" + "\n".join(hits)
