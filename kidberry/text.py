"""Text the server writes into flags (keyword and sound flag reasons), in the interface language.

These reasons are rebuilt from local data on every run, so after the interface language changes,
relabel() rewrites them for all recordings in a few seconds (no AI, nothing leaves the PC).
"""
from . import settings

T = {
    "en": {
        "warning": "Warning phrase: {words}",
        "crying": "Crying on and off for {span} ({crying} of crying in {bouts} bouts, confidence up to {peak}). ",
        "resp_yes": "An adult responded (AI, from the transcript).",
        "resp_no": "No adult heard responding (AI, from the transcript).",
        "resp_unclear": "Unclear whether an adult responded (AI).",
        "resp_none": "Listen for whether an adult responds.",
        "sound": "{label} for ~{span} (confidence {peak}). Listen to hear who and why.",
        "min": "{n:.1f} min", "s": "{n:.0f} s",
        "screaming": "Screaming", "yelling": "Yelling / shouting", "impact": "Slap / impact sound", "crying_label": "Crying",
        "ai_lang": "English",
    },
    "he": {
        "warning": "ביטוי מדאיג: {words}",
        "crying": "בכי לסירוגין במשך {span} ({crying} של בכי ב-{bouts} התפרצויות, ודאות עד {peak}). ",
        "resp_yes": "מבוגר הגיב (AI, לפי התמלול).",
        "resp_no": "לא נשמע מבוגר שמגיב (AI, לפי התמלול).",
        "resp_unclear": "לא ברור אם מבוגר הגיב (AI).",
        "resp_none": "האזינו אם מבוגר מגיב.",
        "sound": "{label} במשך כ-{span} (ודאות {peak}). האזינו כדי לשמוע מי ולמה.",
        "min": "{n:.1f} דקות", "s": "{n:.0f} שניות",
        "screaming": "צרחות", "yelling": "צעקות", "impact": "סטירה / קול חבטה", "crying_label": "בכי",
        "ai_lang": "Hebrew",
    },
}


def t(key: str, **kw) -> str:
    lang = settings.ui_language()
    return T.get(lang, T["en"]).get(key, T["en"][key]).format(**kw)


def span(seconds: float) -> str:
    return t("min", n=seconds / 60) if seconds >= 60 else t("s", n=seconds)


def relabel() -> int:
    """Rewrite keyword and sound flag reasons for every recording in the current language. Verdicts are kept."""
    from .analyze import keyword_flags
    from .db import connect
    from .sounds import sound_flags
    n = 0
    with connect() as conn:
        for rec in conn.execute("SELECT * FROM recordings WHERE status='done'").fetchall():
            keyword_flags(conn, rec["id"])
            sound_flags(conn, rec)
            n += 1
    return n
