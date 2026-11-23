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
    languages                   what is spoken in the kindergarten: "he", "en", "es", "ru". Only the speech models
                                these need are downloaded; Hebrew-only needs just the Hebrew model.
    language_notes              optional hints for the AI per language, e.g. {"es": "Rioplatense, e.g. \"ponelo\""}
    ui_language                 "en" or "he": the interface language, and what other languages are translated into
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
    "languages": ["he"],
    "language_notes": {},
    "ui_language": "en",
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

LANGUAGE_NAMES = {"he": "Hebrew", "en": "English", "es": "Spanish", "ru": "Russian"}


def languages() -> list[str]:
    """The kindergarten's languages, Hebrew first when present. Reads the old staff_languages.spanish too."""
    langs = [l for l in (S.get("languages") or []) if l in LANGUAGE_NAMES]
    if (S.get("staff_languages") or {}).get("spanish") and "es" not in langs:
        langs.append("es")
    langs = langs or ["he"]
    return sorted(dict.fromkeys(langs), key=lambda l: l != "he")


def language_note(lang: str) -> str:
    note = (S.get("language_notes") or {}).get(lang, "")
    if not note and lang == "es":
        note = (S.get("staff_languages") or {}).get("spanish_note", "")
    return note


def ui_language() -> str:
    return S.get("ui_language") if S.get("ui_language") in ("en", "he") else "en"
