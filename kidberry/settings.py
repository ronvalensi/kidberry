"""Per-family settings: the child's name, other children, staff languages, the recorder.

Kept in data/settings.json, next to the database. It is never uploaded (data/ is git-ignored), so
updates to the code never touch it. Missing keys fall back to DEFAULTS.

    child.name                  the child's name as it is written in the transcript language, e.g. "נועם"
    child.name_latin            the same name in Latin letters, e.g. "Noam"
    child.heard_as              how speech recognition writes the name; counted as the name
    child.maybe_heard_as        sounds like the name; shown as "maybe", listen to check
    child.heard_as_common_word  mishearings that are also everyday words (e.g. בלי = "without");
                                counted as "maybe" only when they sound like calling the child
    other_children              [{"name": "Yael", "native": "יעל", "heard_as": ["יאל"], "note": ""}]
                                so the AI does not turn other children into your child
    staff_languages.spanish     true when staff speak Spanish (lines are translated to English)
    recorder.path               the folder on the recorder that holds the recordings
"""
import copy
import json

from .config import DATA_DIR

SETTINGS_PATH = DATA_DIR / "settings.json"

DEFAULTS = {
    "child": {"name": "", "name_latin": "", "heard_as": [], "maybe_heard_as": [], "heard_as_common_word": []},
    "other_children": [],
    "country": "Israel",
    "staff_languages": {"spanish": False, "spanish_note": ""},
    "recorder": {"path": ""},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out


def load() -> dict:
    if SETTINGS_PATH.exists():
        return _merge(DEFAULTS, json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    return copy.deepcopy(DEFAULTS)


def save(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


S = load()
