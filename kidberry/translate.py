"""English translation of Spanish transcript lines. Runs locally on the GPU (free, no tokens).

Usage:
    python -m kidberry.translate            # translate Spanish lines that have no (up-to-date) translation
    python -m kidberry.translate --force    # re-translate everything

Translates the AI-corrected text when there is one, otherwise what Whisper heard. If the AI correction
changes later, the line is translated again on the next run.
"""
import argparse

from . import config
from .db import connect

MODEL = "Helsinki-NLP/opus-mt-es-en"
BATCH = 32


class Translator:
    def __init__(self):
        import torch
        from transformers import MarianMTModel, MarianTokenizer
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() and config.DEVICE == "cuda" else "cpu"
        self.tok = MarianTokenizer.from_pretrained(MODEL)
        self.model = MarianMTModel.from_pretrained(MODEL).to(self.device).eval()

    def __call__(self, texts: list[str]) -> list[str]:
        out = []
        for i in range(0, len(texts), BATCH):
            batch = self.tok(texts[i: i + BATCH], return_tensors="pt", padding=True, truncation=True,
                             max_length=400).to(self.device)
            with self.torch.no_grad():
                gen = self.model.generate(**batch, max_new_tokens=300, num_beams=4)
            out += [t.strip() for t in self.tok.batch_decode(gen, skip_special_tokens=True)]
        return out


def translate_all(conn, force: bool = False, translator: Translator | None = None) -> int:
    rows = conn.execute(
        "SELECT id, COALESCE(corrected, text) AS src, translated_from FROM segments"
        " WHERE language='es' AND filtered IS NULL").fetchall()
    todo = [r for r in rows if force or r["translated_from"] != r["src"]]
    if not todo:
        return 0
    translator = translator or Translator()
    result = translator([r["src"] for r in todo])
    with conn:
        conn.executemany("UPDATE segments SET translation=?, translated_from=? WHERE id=?",
                         [(t, r["src"], r["id"]) for r, t in zip(todo, result)])
    return len(todo)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="re-translate everything")
    args = ap.parse_args()
    n = translate_all(connect(), args.force)
    print(f"Translated {n} Spanish lines to English.")


if __name__ == "__main__":
    main()
