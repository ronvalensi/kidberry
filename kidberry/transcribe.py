"""Transcribe kindergarten recordings into the Kidberry database.

Usage:
    python -m kidberry.transcribe <folder-or-wav> [more ...] [--force]

Pipeline per file:
  1. enhance  - ffmpeg high-pass + dynamic loudness normalization -> recordings/processed/<name>.wav
  2. window   - split into ~30s windows, cutting at the quietest moment to avoid splitting words
  3. language - pick the window's language among the kindergarten's languages (settings.json);
                skipped when there is only one
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

from . import config, hardware, settings
from .db import connect, covers, process_range

SR = 16000
WINDOW_S = 30.0
CUT_SEARCH_S = 6.0   # look for the quietest cut point in the last N seconds of each window
FRAME_S = 0.1
CHECKPOINT_S = 300.0  # save progress every ~5 min of audio, so an interrupted file resumes from there


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


def _pcm16_mono_16k(path: Path) -> tuple[int, int] | None:
    """(byte offset, samples) of the audio data if this is a 16 kHz mono 16-bit PCM WAV, else None."""
    with open(path, "rb") as f:
        if f.read(4) != b"RIFF":
            return None
        f.read(4)
        if f.read(4) != b"WAVE":
            return None
        ok = False
        while True:
            head = f.read(8)
            if len(head) < 8:
                return None
            cid, size = head[:4], int.from_bytes(head[4:], "little")
            if cid == b"fmt ":
                fmt_ = f.read(size)
                fmt_code, channels = int.from_bytes(fmt_[0:2], "little"), int.from_bytes(fmt_[2:4], "little")
                rate, bits = int.from_bytes(fmt_[4:8], "little"), int.from_bytes(fmt_[14:16], "little")
                ok = fmt_code == 1 and channels == 1 and rate == SR and bits == 16
                if size % 2:
                    f.read(1)
            elif cid == b"data":
                return (f.tell(), size // 2) if ok else None
            else:
                f.seek(size + size % 2, 1)


class AudioFile:
    """A recording read on demand: slicing returns float32 samples in [-1, 1].

    The processed files (16 kHz mono 16-bit WAV) are read from disk one slice at a time, so a 5-hour
    file costs almost no RAM instead of ~1.7 GB. Other formats are decoded fully with ffmpeg."""

    def __init__(self, path: Path):
        info = _pcm16_mono_16k(Path(path))
        self._arr = None if info else load_audio(path)
        if info:
            self._offset, self._n = info
            self._f = open(path, "rb")

    def __len__(self) -> int:
        return len(self._arr) if self._arr is not None else self._n

    def __getitem__(self, sl: slice) -> np.ndarray:
        if self._arr is not None:
            return self._arr[sl]
        start, stop, step = sl.indices(self._n)
        if step != 1:
            raise ValueError("AudioFile only supports contiguous slices")
        count = max(0, stop - start)
        self._f.seek(self._offset + 2 * start)
        return np.fromfile(self._f, np.int16, count).astype(np.float32) / 32768.0

    def close(self) -> None:
        if self._arr is None:
            self._f.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def load_audio(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                          "-ar", str(SR), "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def windows(audio):
    """Yield (start_s, end_s) windows of <=30s, cut at the quietest 100ms frame near the end."""
    frame = int(FRAME_S * SR)
    n_frames = len(audio) // frame
    energy = np.empty(n_frames, np.float32)
    block = 6000                                   # frames per block (10 min), so memory stays small
    for i in range(0, n_frames, block):
        j = min(i + block, n_frames)
        x = audio[i * frame: j * frame].reshape(j - i, frame)
        energy[i:j] = np.sqrt(np.mean(x ** 2, axis=1))
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
    r"субтитр|редактор субтитров|продолжение следует|спасибо за (просмотр|внимание)|подписывайтесь|"
    r"ставьте лайк|dimatorzok|"
    # The Hebrew model was trained partly on Knesset sessions and falls back to them on noise.
    r"היושב[- ]?ראש|הכנסת|חברי חברי|חברות וחברי|רבותי|הצבעה|אדוני",
    re.IGNORECASE,
)
# Short phrases Whisper emits on noise. A segment made only of these carries no information and
# is usually invented, so it is filtered ("תודה רבה.", "Oh, oh, oh.", "Hi!").
_FILLERS = re.compile(r"\b(thank you( very much)?|thanks|bye( bye)?|you|okay|ok|oh|hi|hey|hello|"
                      r"(muchas )?gracias|adi[oó]s|hola|chao|спасибо|пока|привет|ага)\b|"
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

def models_needed(langs: list[str]) -> list[str]:
    """The speech models a set of languages needs (what will be downloaded and loaded)."""
    general = hardware.profile().model_general
    need = []
    if "he" in langs:
        need.append(config.MODEL_HEBREW)
    if any(l != "he" for l in langs) and general not in need:
        need.append(general)
    return need


class Transcriber:
    def __init__(self, langs: list[str] | None = None):
        hw = hardware.profile()
        if hw.device == "cuda":
            _load_cuda_dlls()
        from faster_whisper import WhisperModel
        self.langs = langs or settings.languages()
        names = ", ".join(settings.LANGUAGE_NAMES[l] for l in self.langs)
        need = models_needed(self.langs)
        print(f"Languages: {names}. Loading {' + '.join(need)} on {hw.device} ({hw.compute_type})...")

        def load(m):
            return WhisperModel(m, device=hw.device, compute_type=hw.compute_type, cpu_threads=hw.cpu_threads)
        self.general = load(hw.model_general) if hw.model_general in need else None
        if "he" not in self.langs:
            self.hebrew = None
        elif config.MODEL_HEBREW == hw.model_general:
            self.hebrew = self.general
        else:
            self.hebrew = load(config.MODEL_HEBREW)

    def language(self, chunk: np.ndarray) -> str:
        """The window's language, chosen only among the kindergarten's languages.

        Hebrew is the default when it is spoken; another language must be clearly detected. On muffled
        audio Whisper's detector often guesses other languages with low confidence, and forcing a wrong
        language makes Whisper *translate* or invent ("Gracias."), so a non-default language must be
        earned. (The Hebrew-tuned model always answers "he", so detection uses the general model.)
        """
        if len(self.langs) == 1:
            return self.langs[0]
        _, _, probs = self.general.detect_language(audio=chunk)
        p = {lang: v for lang, v in dict(probs).items() if lang in self.langs}
        default = "he" if "he" in self.langs else max(p, key=p.get, default=self.langs[0])
        p_default = p.get(default, 0.0)
        for lang in self.langs:
            if lang == default:
                continue
            rivals = max((v for o, v in p.items() if o not in (lang, default)), default=0.0)
            if (p.get(lang, 0.0) >= config.LANGUAGE_MIN_PROB.get(lang, 0.6)
                    and p[lang] > 2 * p_default and p[lang] > rivals):
                return lang
        return default

    def transcribe_chunk(self, chunk: np.ndarray, lang: str):
        model = self.hebrew if lang == "he" else self.general
        segs, _ = model.transcribe(
            chunk, language=lang, vad_filter=False, word_timestamps=True, beam_size=5,
            condition_on_previous_text=False,
        )
        return list(segs)

    def run(self, conn, rec_id: int, path: Path, fresh: bool = False) -> None:
        """Transcribe one file - only the kindergarten hours (plus a margin) when they are set for its day.
        Progress is saved every CHECKPOINT_S seconds of audio; a file that was interrupted (killed, power
        loss, Ctrl+C) continues from its last checkpoint unless fresh=True. The windows are cut the same
        way from the same processed audio, so a resumed file lines up with what was already saved."""
        conn.execute("UPDATE recordings SET status='processing', error=NULL WHERE id=?", (rec_id,))
        conn.commit()
        t0 = time.time()

        print("  enhancing audio...", end="", flush=True)
        existed = (config.PROCESSED_DIR / (path.stem + ".wav")).exists()
        processed = enhance(path)
        conn.execute("UPDATE recordings SET processed_path=? WHERE id=?", (str(processed), rec_id))
        conn.commit()
        audio = AudioFile(processed)
        duration = len(audio) / SR
        rec = conn.execute("SELECT * FROM recordings WHERE id=?", (rec_id,)).fetchone()
        lo, hi = process_range(conn, rec, duration)
        part = f", transcribing {fmt(lo)}-{fmt(hi)} (kindergarten hours)" if (lo, hi) != (0.0, duration) else ""
        print(f" done ({fmt(duration)}{part})")

        done_until = rec["transcribed_until"]
        same_range = rec["transcribed_range"] == json.dumps([lo, hi])
        if fresh or not existed or not done_until or not same_range:
            done_until = 0.0                       # start over: new audio or a different part of the file
            with conn:
                conn.execute("DELETE FROM segments WHERE recording_id=?", (rec_id,))
                conn.execute("UPDATE recordings SET transcribed_until=0, transcribed_range=? WHERE id=?",
                             (json.dumps([lo, hi]), rec_id))
        else:
            with conn:   # drop anything written after the checkpoint (there should be none)
                conn.execute("DELETE FROM segments WHERE recording_id=? AND start>=?", (rec_id, done_until))
            print(f"  resuming from {fmt(done_until)} ({(done_until - lo) / max(hi - lo, 1):.0%} was already done)")
        todo_s = hi - max(lo, done_until)
        if todo_s > 0:
            print(f"  estimated {hardware.fmt_minutes(todo_s / hardware.speed('transcribe') / 60)} on this PC")

        kept, dropped = conn.execute(
            "SELECT count(*) - count(filtered), count(filtered) FROM segments WHERE recording_id=?", (rec_id,)).fetchone()
        rows, last_save, started_at = [], done_until, max(lo, done_until)
        t_work = time.time()

        def save(until: float) -> None:
            with conn:
                conn.executemany(
                    "INSERT INTO segments (recording_id, start, end, text, language, avg_logprob, no_speech_prob,"
                    " compression_ratio, filtered, words_json) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
                conn.execute("UPDATE recordings SET transcribed_until=? WHERE id=?", (until, rec_id))
            rows.clear()

        for w_start, w_end in windows(audio):
            if w_end <= done_until + 0.01 or w_end <= lo or w_start >= hi:
                continue
            chunk = audio[int(w_start * SR): int(w_end * SR)]
            lang = self.language(chunk)
            for seg in self.transcribe_chunk(chunk, lang):
                text = clean(seg.text)
                reason = filter_reason(text, seg.avg_logprob, seg.no_speech_prob, seg.compression_ratio)
                dropped += reason is not None
                kept += reason is None
                words = [{"w": clean(w.word), "s": round(w_start + w.start, 2), "e": round(w_start + w.end, 2),
                          "p": round(w.probability, 3)} for w in (seg.words or [])]
                start, end = w_start + seg.start, min(w_start + seg.end, w_end)
                rows.append((rec_id, start, end, text, lang, seg.avg_logprob, seg.no_speech_prob,
                             seg.compression_ratio, reason, json.dumps(words, ensure_ascii=False)))
            if w_end - last_save >= CHECKPOINT_S:
                save(w_end)
                last_save = w_end
            elapsed = time.time() - t0
            print(f"\r  {(w_end - lo) / max(hi - lo, 1):6.1%}  [{fmt(w_end)}]  kept {kept}, filtered {dropped}"
                  f"  ({(w_end - started_at) / max(time.time() - t_work, 1):.0f}x realtime)   ", end="", flush=True)
        print()
        save(duration)
        hardware.record_speed("transcribe", hi - started_at, time.time() - t_work)

        langs = dict(conn.execute("SELECT language, count(*) FROM segments WHERE recording_id=? AND filtered IS NULL"
                                  " GROUP BY language", (rec_id,)).fetchall())
        main_lang = max(langs, key=langs.get) if langs else "he"
        with conn:
            conn.execute(
                "UPDATE recordings SET status='done', duration=?, language=?, processed_at=? WHERE id=?",
                (duration, main_lang, datetime.now().isoformat(timespec="seconds"), rec_id))
        print(f"  done in {(time.time() - t0) / 60:.1f} min: {kept} segments kept, {dropped} filtered"
              f" as likely hallucinations; lines by language: {langs}")


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
        rec = conn.execute("SELECT * FROM recordings WHERE id=?", (rec_id,)).fetchone()
        if rec["status"] == "done" and not args.force:
            if rec["duration"] and not covers(rec["transcribed_range"], process_range(conn, rec, rec["duration"])):
                print(f"[redo] {path.name}: the kindergarten hours now include time that was not transcribed")
                todo.append((rec_id, path, True))
            else:
                print(f"[skip] already transcribed: {path.name}")
        else:
            todo.append((rec_id, path, args.force))
    if not todo:
        print("Nothing to do.")
        return

    hw = hardware.profile()
    print(f"Hardware: {hw.summary()}")
    for note in hw.notes + [hardware.battery_warning()]:
        if note:
            print(f"  ! {note}")
    tr = Transcriber()
    with hardware.keep_awake():
        _run_all(conn, tr, todo)


def _run_all(conn, tr, todo) -> None:
    for i, (rec_id, path, fresh) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {path.name}")
        try:
            tr.run(conn, rec_id, path, fresh=fresh)
        except KeyboardInterrupt:
            conn.execute("UPDATE recordings SET status='pending' WHERE id=?", (rec_id,))
            conn.commit()
            sys.exit("\nInterrupted; this file continues from its last checkpoint next run.")
        except Exception as e:  # keep going with the remaining files
            print(f"\n  ERROR: {e}")
            conn.execute("UPDATE recordings SET status='error', error=? WHERE id=?", (str(e), rec_id))
            conn.commit()


if __name__ == "__main__":
    main()
