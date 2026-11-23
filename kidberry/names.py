"""Find every time the child's name is said, including the ways speech recognition mishears it.

The name and its mishearings come from data/settings.json (see settings.py):
    "name"  - the name itself or a known mishearing (child.name, child.heard_as), in the raw or the AI-corrected text
    "maybe" - sounds like the name (child.maybe_heard_as), or a mishearing that is also an everyday word
              (child.heard_as_common_word, e.g. בלי = "without") used the way people call a child: alone,
              before a comma/!/?, at the end of a line, or after "come", "enough", "no"...
              ("בלי להרביץ" = "without hitting" is not counted)
"""
import re

from .settings import S

_child = S["child"]
NAME = _child["name"]
NAME_EN = _child["name_latin"] or NAME

H = "א-ת"
_is_latin = lambda w: bool(re.fullmatch(r"[A-Za-z' -]+", w))
_words = [w for w in [NAME, _child["name_latin"], *_child["heard_as"]] if w]
_HE_NAME = [re.escape(w) for w in _words if not _is_latin(w)]
_LATIN_NAME = [re.escape(w) for w in _words if _is_latin(w)]
_HE_MAYBE = [re.escape(w) for w in _child["maybe_heard_as"] if w and not _is_latin(w)]
_LATIN_MAYBE = [re.escape(w) for w in _child["maybe_heard_as"] if w and _is_latin(w)]
_COMMON = [re.escape(w) for w in _child["heard_as_common_word"] if w]

_PREFIX = "(?:ו|ש|ה|ל)?"   # no כ: "כללי" (general) is not כ+ללי
NEVER = r"(?!x)x"          # a pattern that matches nothing, for empty lists


def _alt(he: list, latin: list) -> str:
    parts = []
    if he:
        parts.append(rf"(?<![{H}]){_PREFIX}(?:{'|'.join(he)})(?![{H}])")
    if latin:
        parts.append(rf"\b(?:{'|'.join(latin)})\b")
    return "|".join(parts) or NEVER


NAME_RE = re.compile(_alt(_HE_NAME, _LATIN_NAME), re.I)
MAYBE_RE = re.compile(_alt(_HE_MAYBE, _LATIN_MAYBE), re.I)
# An everyday-word mishearing that sounds like calling the child: the whole line, or followed by
# punctuation / end of line, or right after a word people say before a child's name.
_CALL_BEFORE = "בוא|בואי|די|לא|יא|אוי|היי|הי|נו|תבוא|רגע|שב|תשב|איפה|מה|יופי|כל הכבוד|אהלן|שלום|ביי|יאללה"
_C = "|".join(_COMMON)
_COMMON_PAT = (rf"(?<![{H}])(?:{_C})(?=\s*(?:[,!?.…]|$))|(?:{_CALL_BEFORE})[\s,]+(?:{_C})(?![{H}])"
               if _COMMON else NEVER)
BLI_RE = re.compile(rf"(?<![{H}])(?:{_C})(?=\s*(?:[,!?.…]|$))|(?:^|(?<=[\s,]))(?:{_CALL_BEFORE})[\s,]+(?:{_C})(?![{H}])"
                    if _COMMON else NEVER)


def mention(*texts: str | None) -> str | None:
    """'name', 'maybe' or None for one transcript line (pass the raw text and the AI correction)."""
    tier = None
    for t in texts:
        if not t:
            continue
        if NAME_RE.search(t):
            return "name"
        if MAYBE_RE.search(t) or BLI_RE.search(t.strip()):
            tier = "maybe"
    return tier


def highlight_pattern() -> str:
    """One regex (JavaScript-compatible) the page uses to highlight the name in a line."""
    return "|".join(p for p in (_alt(_HE_NAME + _HE_MAYBE, _LATIN_NAME + _LATIN_MAYBE), _COMMON_PAT) if p != NEVER) or NEVER


if __name__ == "__main__":
    import sys
    from datetime import datetime, timedelta
    from .db import connect
    sys.stdout.reconfigure(encoding="utf-8")
    with connect() as conn:
        rows = conn.execute("SELECT s.*, r.started_at, r.name AS rec FROM segments s JOIN recordings r ON r.id=s.recording_id"
                            " WHERE s.filtered IS NULL ORDER BY r.started_at, s.start").fetchall()
    for s in rows:
        tier = mention(s["text"], s["corrected"])
        if tier:
            clock = (datetime.fromisoformat(s["started_at"]) + timedelta(seconds=s["start"])).strftime("%Y-%m-%d %H:%M:%S") \
                if s["started_at"] else s["rec"]
            print(f"{clock}  {tier:5}  {s['corrected'] or s['text']}")
