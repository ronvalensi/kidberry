"""Translate non-Hebrew transcript lines into the interface language. Runs locally (free, no tokens).

Usage:
    python -m kidberry.translate            # translate lines that have no (up-to-date) translation
    python -m kidberry.translate --force    # re-translate everything

Which lines: those in the kindergarten's languages other than Hebrew and other than the interface
language (settings.json "ui_language"). With an English interface, Spanish and Russian lines get English;
with a Hebrew interface, English, Spanish and Russian lines get Hebrew. Hebrew lines are never translated.
Only the translation models these need are downloaded (~300 MB each).

Translates the AI-corrected text when there is one, otherwise what Whisper heard. If the AI correction
changes later, or the interface language changes, the line is translated again on the next run.
"""
import argparse
import re

from . import config, settings
from .db import connect

MODELS = {   # (from, to) -> Helsinki-NLP OPUS-MT model
    ("es", "en"): "Helsinki-NLP/opus-mt-es-en",
    ("ru", "en"): "Helsinki-NLP/opus-mt-ru-en",
    ("en", "he"): "Helsinki-NLP/opus-mt-en-he",
    ("es", "he"): "Helsinki-NLP/opus-mt-es-he",
    ("ru", "he"): "Helsinki-NLP/opus-mt-ru-he",
}
BATCH = 32


def pairs_needed(langs: list[str] | None = None, target: str | None = None) -> list[tuple[str, str]]:
    """The (from, to) translations a set of languages needs."""
    langs, target = langs or settings.languages(), target or settings.ui_language()
    return [(l, target) for l in langs if l not in ("he", target) and (l, target) in MODELS]


class Translator:
    def __init__(self, model: str):
        import torch
        from transformers import MarianMTModel, MarianTokenizer
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() and config.DEVICE == "cuda" else "cpu"
        self.tok = MarianTokenizer.from_pretrained(model)
        self.model = MarianMTModel.from_pretrained(model).to(self.device).eval()

    def __call__(self, texts: list[str]) -> list[str]:
        out = []
        for i in range(0, len(texts), BATCH):
            batch = self.tok(texts[i: i + BATCH], return_tensors="pt", padding=True, truncation=True,
                             max_length=400).to(self.device)
            with self.torch.no_grad():
                gen = self.model.generate(**batch, max_new_tokens=300, num_beams=4)
            out += [t.strip() for t in self.tok.batch_decode(gen, skip_special_tokens=True)]
        return out


def _protect_name(text: str) -> str:
    """Translation models drop or invent names written in Hebrew letters inside a Spanish/Russian line
    ("y Dana, נועם" -> "and Dana, ."), so the child's name is translated as its Latin spelling."""
    c = settings.S["child"]
    if c["name"] and c["name_latin"]:
        text = re.sub(rf"(?<![א-ת]){re.escape(c['name'])}(?![א-ת])", c["name_latin"], text)
    return text


def _restore_name(text: str, target: str) -> str:
    c = settings.S["child"]
    if target == "he" and c["name"] and c["name_latin"]:
        text = re.sub(rf"\b{re.escape(c['name_latin'])}\b", c["name"], text)
    return text


def translate_all(conn, force: bool = False) -> dict:
    """Translate what is missing or stale. Returns {"es->en": n, ...} for the pairs that had work."""
    done = {}
    for src_lang, target in pairs_needed():
        rows = conn.execute(
            "SELECT id, COALESCE(corrected, text) AS src, translated_from, translation_lang FROM segments"
            " WHERE language=? AND filtered IS NULL", (src_lang,)).fetchall()
        todo = [r for r in rows if force or r["translated_from"] != r["src"] or r["translation_lang"] != target]
        if not todo:
            continue
        translator = Translator(MODELS[(src_lang, target)])   # one model in memory at a time
        result = [_restore_name(t, target) for t in translator([_protect_name(r["src"]) for r in todo])]
        with conn:
            conn.executemany("UPDATE segments SET translation=?, translated_from=?, translation_lang=? WHERE id=?",
                             [(t, r["src"], target, r["id"]) for r, t in zip(todo, result)])
        done[f"{src_lang}->{target}"] = len(todo)
        del translator
    return done


def summary(done: dict) -> str:
    names = settings.LANGUAGE_NAMES
    return ", ".join(f"{n} {names[k[:2]]} lines to {names[k[-2:]]}" for k, n in done.items())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="re-translate everything")
    args = ap.parse_args()
    if not pairs_needed():
        print("Nothing to translate for these languages.")
        return
    done = translate_all(connect(), args.force)
    print(f"Translated {summary(done)}." if done else "All translations are up to date.")


if __name__ == "__main__":
    main()
