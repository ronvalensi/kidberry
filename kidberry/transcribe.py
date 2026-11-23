"""Transcribe kindergarten recordings into the Kidberry database.

Usage:
    python -m kidberry.transcribe <folder-or-wav> [more ...] [--force]

Pipeline per file:
  1. enhance  - ffmpeg high-pass + dynamic loudness normalization -> recordings/processed/<name>.wav
  2. window   - split into ~30s windows, cutting at the quietest moment to avoid splitting words
  3. language - detect Hebrew vs English per window (other languages are not allowed)
  4. transcribe with the matching model (Hebrew-tuned or general), word timestamps on
  5. filter   - drop known Whisper hallucinations ("thanks for watching", repetition loops, ...)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from . import config
from .db import connect

SR = 16000
WINDOW_S = 30.0
CUT_SEARCH_S = 6.0   # look for the quietest cut point in the last N seconds of each window
FRAME_S = 0.1


def _load_cuda_dlls() -> None:
    """Make the pip-installed CUDA libraries (cuBLAS, cuDNN) visible on Windows."""
    try:
        import nvidia  # namespace package from the nvidia-*-cu12 wheels
    except ImportError:
        return
    for base in nvidia.__path__:
        for sub in ("cublas", "cudnn"):
            bin_dir = Path(base) / sub / "bin"
            if bin_dir.is_dir():
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]


# ---------------------------------------------------------------- files / db

def find_wavs(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            files.extend(sorted((f for f in p.rglob("*") if f.suffix.lower() == ".wav"), key=lambda f: f.name))
        elif p.is_file():
            files.append(p)
        else:
            print(f"[skip] not found: {p}")
    return [f.resolve() for f in files]


def started_at_from_name(name: str) -> str | None:
    """The recorder names files YYYYMMDDhhmmss.WAV (recording start time)."""
    m = re.search(r"(\d{14})", name)
    if not m:
        return None
    try:
        return datetime.strptime(m[1], "%Y%m%d%H%M%S").isoformat()
    except ValueError:
        return None


def register(conn, path: Path) -> int:
    st = path.stat()
    conn.execute(
        "INSERT OR IGNORE INTO recordings (path, name, size_bytes, file_mtime, started_at) VALUES (?,?,?,?,?)",
        (str(path), path.name, st.st_size, st.st_mtime, started_at_from_name(path.name)),
    )
    conn.commit()
    return conn.execute("SELECT id FROM recordings WHERE path=?", (str(path),)).fetchone()["id"]


def fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ---------------------------------------------------------------- audio

def enhance(src: Path) -> Path:
    config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    dst = config.PROCESSED_DIR / (src.stem + ".wav")
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    tmp = dst.with_suffix(".tmp.wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-af", config.ENHANCE_FILTER,
                    "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(tmp)], check=True)
    tmp.replace(dst)
    return dst


def load_audio(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                          "-ar", str(SR), "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def windows(audio: np.ndarray):
    """Yield (start_s, end_s) windows of <=30s, cut at the quietest 100ms frame near the end."""
    frame = int(FRAME_S * SR)
    n_frames = len(audio) // frame
    energy = np.sqrt(np.mean(audio[: n_frames * frame].reshape(n_frames, frame) ** 2, axis=1))
    total = len(audio) / SR
    pos = 0.0
    while pos < total - 0.5:
        end = pos + WINDOW_S
        if end >= total:
            yield pos, total
            return
        lo, hi = int((end - CUT_SEARCH_S) / FRAME_S), int(end / FRAME_S)
        cut = (lo + int(np.argmin(energy[lo:hi]))) * FRAME_S + FRAME_S / 2
        yield pos, cut
        pos = cut


# ---------------------------------------------------------------- hallucination filter

_HALLUCINATION = re.compile(
    r"thank(s| you) for watching|subscribe|like (and|&) share|see you (in the )?next|"
    r"terima kasih|takk for|ご視聴|おやすみ|amara\.org|subtitles? by|translated by|"
    r"תודה שצפיתם|תודה על הצפייה|כתוביות|הירשמו לערוץ|"
    r"gracias por ver|suscr[ií]bete|subt[ií]tulos|"
    # The Hebrew model was trained partly on Knesset sessions and falls back to them on noise.
    r"היושב[- ]?ראש|הכנסת|חברי חברי|חברות וחברי|רבותי|הצבעה|אדוני",
    re.IGNORECASE,
)
# Short phrases Whisper emits on noise. A segment made only of these carries no information and
# is usually invented, so it is filtered ("תודה רבה.", "Oh, oh, oh.", "Hi!").
_FILLERS = re.compile(r"\b(thank you( very much)?|thanks|bye( bye)?|you|okay|ok|oh|hi|hey|hello|"
                      r"(muchas )?gracias|adi[oó]s|hola|chao)\b|"
                      r"(ו?תודה( רבה)?( לך| לכם)?|ביי( ביי)?|שלום|היי|אה+)", re.IGNORECASE)
_BIDI = re.compile("[‎‏‪-‮⁦-⁩]")


def clean(text: str) -> str:
    return _BIDI.sub("", text).strip()


def filter_reason(text: str, avg_logprob: float, no_speech_prob: float, compression_ratio: float | None) -> str | None:
    """Why a segment is probably a Whisper hallucination, or None if it looks like real speech."""
    if not text:
        return "empty"
    if _HALLUCINATION.search(text):
        return "known hallucination"
    if (compression_ratio or 0) > 2.4 or re.search(r"(.)\1{5,}", text):
        return "repetition"
    bare = re.sub(r"[^\w\s]", " ", text)
    words = bare.split()
    if len(words) >= 4 and len(set(words)) / len(words) <= 0.5:      # "היי יפה, היי יפה, היי יפה"
        return "repetition"
    if avg_logprob < -1.3 or (no_speech_prob > 0.8 and avg_logprob < -0.6):
        return "low confidence"
    fillers = _FILLERS.findall(bare)
    rest = _FILLERS.sub(" ", bare).split()
    if fillers and not rest:
        return "filler only"
    if len(fillers) >= 3 and len(rest) <= 4:                          # "בסדר, תודה, תודה, תודה, בבקשה"
        return "filler repetition"
    return None


# ---------------------------------------------------------------- transcription

class Transcriber:
    def __init__(self):
        _load_cuda_dlls()
        from faster_whisper import WhisperModel
        print(f"Loading models: {config.MODEL_GENERAL} + {config.MODEL_HEBREW} on {config.DEVICE}...")
        self.general = WhisperModel(config.MODEL_GENERAL, device=config.DEVICE, compute_type=config.COMPUTE_TYPE)
        if config.MODEL_HEBREW == config.MODEL_GENERAL:
            self.hebrew = self.general
        else:
            self.hebrew = WhisperModel(config.MODEL_HEBREW, device=config.DEVICE, compute_type=config.COMPUTE_TYPE)

    def language(self, chunk: np.ndarray) -> str:
        """Hebrew by default; English or Spanish only when the detector is clearly confident.

        On muffled audio Whisper's detector often guesses other languages (es/jw/nn) with low
        confidence, and forcing a wrong language makes Whisper *translate* or invent ("Gracias.").
        So English/Spanish must be earned. (The Hebrew-tuned model always answers "he".)
        Spanish matters where staff speak Spanish to each other.
        """
        _, _, probs = self.general.detect_language(audio=chunk)
        p = dict(probs)
        p_he = p.get("he", 0.0)
        for lang, min_p in (("en", config.ENGLISH_MIN_PROB), ("es", config.SPANISH_MIN_PROB)):
            other = p.get("es" if lang == "en" else "en", 0.0)
            if p.get(lang, 0.0) >= min_p and p[lang] > 2 * p_he and p[lang] > other:
                return lang
        return "he"

    def transcribe_chunk(self, chunk: np.ndarray, lang: str):
        model = self.hebrew if lang == "he" else self.general
        segs, _ = model.transcribe(
            chunk, language=lang, vad_filter=False, word_timestamps=True, beam_size=5,
            condition_on_previous_text=False,
        )
        return list(segs)

    def run(self, conn, rec_id: int, path: Path) -> None:
        conn.execute("UPDATE recordings SET status='processing', error=NULL WHERE id=?", (rec_id,))
        conn.commit()
        t0 = time.time()

        print("  enhancing audio...", end="", flush=True)
        processed = enhance(path)
        conn.execute("UPDATE recordings SET processed_path=? WHERE id=?", (str(processed), rec_id))
        conn.commit()
        audio = load_audio(processed)
        duration = len(audio) / SR
        print(f" done ({fmt(duration)})")

        rows, dropped, langs = [], 0, {"he": 0, "en": 0, "es": 0}
        for w_start, w_end in windows(audio):
            chunk = audio[int(w_start * SR): int(w_end * SR)]
            lang = self.language(chunk)
            langs[lang] += 1
            for seg in self.transcribe_chunk(chunk, lang):
                text = clean(seg.text)
                reason = filter_reason(text, seg.avg_logprob, seg.no_speech_prob, seg.compression_ratio)
                dropped += reason is not None
                words = [{"w": clean(w.word), "s": round(w_start + w.start, 2), "e": round(w_start + w.end, 2),
                          "p": round(w.probability, 3)} for w in (seg.words or [])]
                start, end = w_start + seg.start, min(w_start + seg.end, w_end)
                rows.append((rec_id, start, end, text, lang, seg.avg_logprob, seg.no_speech_prob,
                             seg.compression_ratio, reason, json.dumps(words, ensure_ascii=False)))
            elapsed = time.time() - t0
            print(f"\r  {w_end / duration:6.1%}  [{fmt(w_end)}]  kept {len(rows) - dropped}, filtered {dropped}"
                  f"  ({w_end / max(elapsed, 1):.0f}x realtime)   ", end="", flush=True)
        print()

        main_lang = max(langs, key=langs.get)
        with conn:
            conn.execute("DELETE FROM segments WHERE recording_id=?", (rec_id,))
            conn.executemany(
                "INSERT INTO segments (recording_id, start, end, text, language, avg_logprob, no_speech_prob,"
                " compression_ratio, filtered, words_json) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            conn.execute(
                "UPDATE recordings SET status='done', duration=?, language=?, processed_at=? WHERE id=?",
                (duration, main_lang, datetime.now().isoformat(timespec="seconds"), rec_id))
        print(f"  done in {(time.time() - t0) / 60:.1f} min: {len(rows) - dropped} segments kept, {dropped} filtered"
              f" as likely hallucinations; windows he={langs['he']} en={langs['en']} es={langs['es']}")


def refilter() -> None:
    """Re-apply filter_reason() to every stored segment (after tuning the filter; no re-transcription)."""
    conn = connect()
    rows = conn.execute("SELECT id, text, avg_logprob, no_speech_prob, compression_ratio FROM segments").fetchall()
    updates = [(filter_reason(r["text"], r["avg_logprob"], r["no_speech_prob"], r["compression_ratio"]), r["id"])
               for r in rows]
    with conn:
        conn.executemany("UPDATE segments SET filtered=? WHERE id=?", updates)
    n = sum(u[0] is not None for u in updates)
    print(f"{len(rows)} segments: {len(rows) - n} kept, {n} filtered")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="*", help="WAV files or folders (searched recursively)")
    ap.add_argument("--force", action="store_true", help="re-transcribe files that are already done")
    ap.add_argument("--refilter", action="store_true", help="only re-apply the hallucination filter to stored segments")
    args = ap.parse_args()
    if args.refilter:
        refilter()
        return
    if not args.inputs:
        ap.error("give WAV files or folders, or --refilter")

    conn = connect()
    todo = []
    for path in find_wavs(args.inputs):
        rec_id = register(conn, path)
        status = conn.execute("SELECT status FROM recordings WHERE id=?", (rec_id,)).fetchone()["status"]
        if status == "done" and not args.force:
            print(f"[skip] already transcribed: {path.name}")
        else:
            todo.append((rec_id, path))
    if not todo:
        print("Nothing to do.")
        return

    tr = Transcriber()
    for i, (rec_id, path) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {path.name}")
        try:
            tr.run(conn, rec_id, path)
        except KeyboardInterrupt:
            conn.execute("UPDATE recordings SET status='pending' WHERE id=?", (rec_id,))
            conn.commit()
            sys.exit("\nInterrupted; this file will be retried next run.")
        except Exception as e:  # keep going with the remaining files
            print(f"\n  ERROR: {e}")
            conn.execute("UPDATE recordings SET status='error', error=? WHERE id=?", (str(e), rec_id))
            conn.commit()


if __name__ == "__main__":
    main()
