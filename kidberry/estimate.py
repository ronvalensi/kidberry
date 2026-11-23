"""How long processing will take, and how many AI tokens it will use - shown before a run starts.

    python -m kidberry.estimate                 # what is waiting in recordings/raw
    python -m kidberry.estimate --source E:\\RECORD   # also what is on the recorder

Times come from the speeds measured on this PC (hardware.py), or the profile's first guess before the
first run. AI tokens come from this database's past days (tokens per kindergarten hour), or a default.
"""
import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import config, hardware, settings
from .db import PROCESS_MARGIN_S, connect, covers

RAW = config.RAW_DIR
DEFAULT_TOKENS_PER_HOUR = 15000    # smart AI review, measured on real days (~15-16k per kindergarten hour)
FULL_MODE_FACTOR = 5               # --full reviews every line: ~5x the tokens
SECONDS_PER_AI_BATCH = 60          # one batch of ~60 lines takes about a minute; some run at the same time
COPY_MB_PER_S = 40                 # USB recorders are slow


@dataclass
class Estimate:
    files: list = field(default_factory=list)     # [{"name", "audio_s", "work_s", "copy"}]
    transcribe_s: float = 0.0                     # audio seconds still to transcribe
    sounds_s: float = 0.0                         # audio seconds still to scan
    copy_mb: float = 0.0
    ai_hours: float = 0.0                         # kindergarten hours the AI review will cover
    ai_tokens: int = 0
    minutes: dict = field(default_factory=dict)   # step -> minutes

    @property
    def total_minutes(self) -> float:
        return sum(self.minutes.values())

    def lines(self, ai: bool = True, cap: int | None = None) -> list[str]:
        out = []
        if self.files:
            audio = sum(f["audio_s"] for f in self.files) / 3600
            work = sum(f["work_s"] for f in self.files) / 3600
            part = f", {work:.1f} h of it inside kindergarten hours" if work < audio - 0.05 else ""
            out.append(f"{len(self.files)} recording(s), {audio:.1f} h of audio{part}")
        for step, label in (("copy", "copy from the recorder"), ("transcribe", "transcription"),
                            ("sounds", "sound detection"), ("ai", "AI review")):
            if self.minutes.get(step, 0) >= 0.5 and (step != "ai" or ai):
                out.append(f"  {label:<23} ~{hardware.fmt_minutes(self.minutes[step])}")
        total = self.total_minutes - (0 if ai else self.minutes.get("ai", 0))
        out.append(f"  {'total':<23} ~{hardware.fmt_minutes(total)}"
                   + ("  (finishes around " + (datetime.now() + timedelta(minutes=total)).strftime("%H:%M") + ")"
                      if total >= 1 else ""))
        if ai and self.ai_tokens:
            from . import ai as ai_
            share = f" = about {self.ai_tokens / cap:.0%} of your daily cap of {cap:,}" if cap else ""
            out.append(f"  AI tokens               ~{self.ai_tokens:,}{share}")
            p = ai_.PROVIDERS[ai_.provider()]
            out.append(f"  AI: {p['label']}. {p['cost']}")
        return out


def wav_seconds(path: Path) -> float:
    """Length of a WAV from its header (no decoding); falls back to the recorder's usual 32 kB/s."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        i = head.find(b"fmt ")
        byte_rate = int.from_bytes(head[i + 16:i + 20], "little") if i >= 0 else 0
        return path.stat().st_size / (byte_rate or 32000)
    except OSError:
        return 0.0


def _work_range(conn, name: str, audio_s: float) -> tuple[float, float]:
    """The part of a not-yet-registered file that will be processed (kindergarten hours + margin)."""
    from .transcribe import started_at_from_name
    started = started_at_from_name(name)
    if not started:
        return 0.0, audio_s
    t0 = datetime.fromisoformat(started)
    row = conn.execute("SELECT dropoff, pickup FROM school_hours WHERE day=?", (t0.date().isoformat(),)).fetchone()
    if not row:
        return 0.0, audio_s

    def off(hhmm):
        return (datetime.combine(t0.date(), datetime.strptime(hhmm, "%H:%M").time()) - t0).total_seconds()
    return max(0.0, off(row["dropoff"]) - PROCESS_MARGIN_S), min(audio_s, max(0.0, off(row["pickup"]) + PROCESS_MARGIN_S))


def tokens_per_hour(conn) -> float:
    """AI tokens per kindergarten hour: the highest of the last 3 real days (better to over- than
    underestimate credits), or the default before any day was reviewed."""
    rows = conn.execute("""SELECT u.day, SUM(u.tokens_in + u.tokens_out) AS t, h.dropoff, h.pickup
                           FROM ai_usage u JOIN school_hours h ON h.day = u.day GROUP BY u.day ORDER BY u.day""").fetchall()
    rates = []
    for r in rows:
        hours = (datetime.strptime(r["pickup"], "%H:%M") - datetime.strptime(r["dropoff"], "%H:%M")).seconds / 3600
        if hours >= 2 and r["t"] >= 20000:          # skip test runs
            rates.append(r["t"] / hours)
    return max(rates[-3:]) if rates else DEFAULT_TOKENS_PER_HOUR


def estimate(conn, source: Path | None = None, full: bool = False, ai: bool = True) -> Estimate:
    est = Estimate()
    # Files on the recorder that are not copied yet.
    pending_names = set()
    if source and source.is_dir():
        for src in sorted(p for p in source.iterdir() if p.suffix.lower() == ".wav"):
            dst = RAW / src.name
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                continue
            audio = wav_seconds(src)
            lo, hi = _work_range(conn, src.name, audio)
            est.files.append({"name": src.name, "audio_s": audio, "work_s": hi - lo, "copy": True})
            est.copy_mb += src.stat().st_size / 1e6
            est.transcribe_s += hi - lo
            est.sounds_s += hi - lo
            pending_names.add(src.name.lower())
    # Files already copied but not (fully) processed.
    for path in sorted(RAW.glob("*.WAV")) if RAW.is_dir() else []:
        if path.name.lower() in pending_names:
            continue
        rec = conn.execute("SELECT * FROM recordings WHERE name=?", (path.name,)).fetchone()
        audio = (rec and rec["duration"]) or wav_seconds(path)
        lo, hi = _work_range(conn, path.name, audio)
        todo_t = todo_s = 0.0
        if not rec or rec["status"] != "done" or not covers(rec["transcribed_range"], (lo, hi)):
            done = (rec["transcribed_until"] or 0.0) if rec and rec["status"] != "done" else 0.0
            todo_t = max(0.0, hi - max(lo, done))
        scanned = rec and conn.execute("SELECT 1 FROM sound_scores WHERE recording_id=? LIMIT 1", (rec["id"],)).fetchone()
        if not scanned or not covers(rec["sounds_range"], (lo, hi)):
            todo_s = hi - lo
        if todo_t or todo_s:
            est.files.append({"name": path.name, "audio_s": audio, "work_s": hi - lo, "copy": False})
            est.transcribe_s += todo_t
            est.sounds_s += todo_s
    est.minutes["copy"] = est.copy_mb / COPY_MB_PER_S / 60
    est.minutes["transcribe"] = est.transcribe_s / hardware.speed("transcribe") / 60
    est.minutes["sounds"] = est.sounds_s / hardware.speed("sounds") / 60
    if ai:
        est.ai_hours = sum(f["work_s"] for f in est.files) / 3600
        est.ai_tokens = int(round(est.ai_hours * tokens_per_hour(conn) * (FULL_MODE_FACTOR if full else 1), -3))
        from . import ai as ai_
        p = ai_.PROVIDERS[ai_.provider()]
        workers = config.AI_WORKERS or p["workers"] or 1
        interval = config.AI_INTERVAL_S if config.AI_INTERVAL_S is not None else p["interval_s"]
        batches = est.ai_tokens / 10000
        est.minutes["ai"] = batches * max(SECONDS_PER_AI_BATCH / workers, interval) / 60
    return est


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=settings.S["recorder"]["path"] or None, help="the recorder's folder")
    ap.add_argument("--full", action="store_true", help="estimate a full AI review (every line)")
    ap.add_argument("--no-ai", action="store_true")
    args = ap.parse_args()
    est = estimate(connect(), Path(args.source) if args.source else None, args.full, not args.no_ai)
    print(f"Hardware: {hardware.profile().summary()}")
    if not est.files:
        print("Nothing to process.")
        return
    print("\n".join(est.lines(ai=not args.no_ai, cap=config.AI_DAILY_TOKEN_CAP)))


if __name__ == "__main__":
    main()
