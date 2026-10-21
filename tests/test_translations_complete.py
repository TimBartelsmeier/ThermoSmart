"""Every translation file must mirror strings.json (keys and placeholders)."""
import json
import re
from pathlib import Path

import pytest

BASE = Path(__file__).parent.parent / "custom_components" / "thermosmart"
TRANSLATIONS = sorted((BASE / "translations").glob("*.json"))


def _flat(d, prefix=""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _flat(v, f"{prefix}{k}.")
        else:
            yield f"{prefix}{k}", v


def _placeholders(s):
    return sorted(re.findall(r"\{\w+\}", s))


def test_en_matches_strings_json():
    assert json.loads((BASE / "strings.json").read_text("utf-8")) == json.loads(
        (BASE / "translations" / "en.json").read_text("utf-8")
    )


@pytest.mark.parametrize("path", TRANSLATIONS, ids=lambda p: p.stem)
def test_translation_complete(path):
    en = dict(_flat(json.loads((BASE / "strings.json").read_text("utf-8"))))
    tr = dict(_flat(json.loads(path.read_text("utf-8"))))
    assert set(en) - set(tr) == set(), "missing keys"
    assert set(tr) - set(en) == set(), "obsolete keys"
    for key, value in tr.items():
        assert _placeholders(value) == _placeholders(en[key]), key
