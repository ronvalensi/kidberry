"""The whole daily routine in one command.

    python -m kidberry.daily --hours 08:34-15:37

Steps:
  1. copy new recordings from the recorder (recorder.path in data/settings.json) to recordings/raw, checksum-verified
     (and, if you agree / --clear-recorder, delete the verified ones from the recorder)
  2. set the kindergarten hours for the day (asked if not given)
  3. transcribe (GPU, free)            4. detect sounds (GPU, free)
  5. translate other languages (free)  6. keyword flags + smart AI review (your Claude plan, capped)
  7. print the day's summary and any patterns across days

Options:
    --day 2026-11-04       which day the hours are for (default: the day of the new recordings, else today)
    --source E:\\RECORD     where the recorder is mounted (default: recorder.path in settings)
    --no-copy              skip step 1        --clear-recorder   delete verified files from the recorder
    --no-ai                skip the AI review (everything else still runs)
    --full                 AI-review every line instead of only around sound/keyword triggers
"""
import argparse
import hashlib
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

from . import config, settings
from .db import connect
from .transcribe import started_at_from_name

RAW = config.ROOT / "recordings" / "raw"


def say(msg: str) -> None:
    print(f"\n=== {msg}", flush=True)


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ask(question: str) -> str:
    if not sys.stdin.isatty():
        return ""
    try:
        return input(question).strip()
    except EOFError:
        return ""


def copy_from_recorder(source: Path, clear: bool) -> list[Path]:
    """Copy new WAVs, verify each by checksum. Returns the newly copied files."""
    if not source.is_dir():
        print(f"Recorder not found at {source} - skipping copy.")
        return []
    RAW.mkdir(parents=True, exist_ok=True)
    new, verified = [], []
    wavs = {p.name.lower(): p for p in source.iterdir() if p.suffix.lower() == ".wav"}   # Windows: case-insensitive
    for src in sorted(wavs.values(), key=lambda p: p.name):
        dst = RAW / src.name
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            verified.append((src, dst))           # already copied earlier
            continue
        print(f"  copying {src.name} ({src.stat().st_size / 1e6:.0f} MB)...", end="", flush=True)
        shutil.copy2(src, dst)
        if md5(src) != md5(dst):
            dst.unlink()
            print(" CHECKSUM MISMATCH - copy removed, original kept on the recorder")
            continue
        print(" verified")
        new.append(dst)
        verified.append((src, dst))
    if not new:
        print("  no new recordings on the recorder")
    if verified and (clear or ask(f"Delete {len(verified)} backed-up recording(s) from the recorder? [y/N] ").lower() == "y"):
        for src, dst in verified:
            if md5(src) == md5(dst):              # check again right before deleting
                src.unlink()
                print(f"  deleted from recorder: {src.name}")
            else:
                print(f"  KEPT on recorder (copy differs): {src.name}")
    return new


def run(*args: str) -> int:
    """Run one step as its own process, so each step's GPU memory is released when it ends."""
    return subprocess.call([sys.executable, "-m", *args], cwd=config.ROOT)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", help="drop-off and pickup, e.g. 08:34-15:37")
    ap.add_argument("--day", help="the day the hours are for (YYYY-MM-DD)")
    ap.add_argument("--source", default=settings.S["recorder"]["path"] or None)
    ap.add_argument("--no-copy", action="store_true")
    ap.add_argument("--clear-recorder", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    new = []
    if not args.no_copy:
        say("1/7 Copy from recorder")
        if args.source:
            new = copy_from_recorder(Path(args.source), args.clear_recorder)
        else:
            print("No recorder folder set - add recorder.path to data/settings.json or pass --source.")

    # Which day(s) are we processing? The new recordings' days, or the given/latest day.
    conn = connect()
    days = sorted({(started_at_from_name(p.name) or "")[:10] for p in new} - {""})
    if args.day:
        days = [args.day]
    if not days:
        pending = [started_at_from_name(p.name) for p in RAW.glob("*.WAV")
                   if not conn.execute("SELECT 1 FROM recordings WHERE name=? AND status='done'", (p.name,)).fetchone()]
        days = sorted({p[:10] for p in pending if p}) or [date.today().isoformat()]

    say("2/7 Kindergarten hours")
    for day in days:
        have = conn.execute("SELECT dropoff, pickup FROM school_hours WHERE day=?", (day,)).fetchone()
        span = args.hours if args.hours and day == days[-1] else None
        if not span and not have:
            span = ask(f"Drop-off and pickup on {day}, e.g. 08:30-15:40 (Enter to skip): ")
        if span:
            run("kidberry.analyze", "--hours", day, span)
        elif have:
            print(f"{day}: {have['dropoff']}-{have['pickup']} (already set)")
        else:
            print(f"{day}: no hours set - the whole recording will be transcribed and analyzed, incl. the drive and after pickup. "
                  f"Set later with: python -m kidberry.analyze --hours {day} HH:MM-HH:MM")
    conn.close()

    from . import hardware
    hw = hardware.profile()
    print(f"\nHardware: {hw.summary()}")
    for note in hw.notes + [hardware.battery_warning()]:
        if note:
            print(f"  ! {note}")
    with hardware.keep_awake():
        steps(days, args)


def steps(days, args) -> None:
    say("3/7 Transcribe")
    run("kidberry.transcribe", str(RAW))
    say("4/7 Sound detection")
    run("kidberry.sounds")
    say("5/7 Translate other languages (local)")
    run("kidberry.translate")

    for day in days:
        if args.no_ai:
            say(f"6/7 Keyword flags for {day} (AI review skipped)")
            run("kidberry.analyze", "--day", day, "--keywords-only")
        else:
            say(f"6/7 Keyword flags + {'full' if args.full else 'smart'} AI review for {day}")
            run("kidberry.analyze", "--day", day, *(["--full"] if args.full else []))

    say("7/7 Summary")
    run("kidberry.days")
    print(f"\nReview it at http://{config.HOST}:{config.PORT}  (start the page with: python -m kidberry.server)")


if __name__ == "__main__":
    main()
