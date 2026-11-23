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

RAW = config.RAW_DIR


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
    from . import progress
    if not source.is_dir():
        print(f"Recorder not found at {source} - skipping copy.")
        return []
    RAW.mkdir(parents=True, exist_ok=True)
    new, verified = [], []
    wavs = {p.name.lower(): p for p in source.iterdir() if p.suffix.lower() == ".wav"}   # Windows: case-insensitive
    todo = [p for p in sorted(wavs.values(), key=lambda p: p.name)
            if not ((RAW / p.name).exists() and (RAW / p.name).stat().st_size == p.stat().st_size)]
    total = sum(p.stat().st_size for p in todo) or 1
    done_bytes = 0
    for src in sorted(wavs.values(), key=lambda p: p.name):
        dst = RAW / src.name
        if src not in todo:
            verified.append((src, dst))           # already copied earlier
            continue
        size = src.stat().st_size
        progress.step(done_bytes / total, f"copying {src.name}", force=True)
        print(f"  copying {src.name} ({size / 1e6:.0f} MB)...", end="", flush=True)
        shutil.copy2(src, dst)
        progress.step((done_bytes + 0.7 * size) / total, f"verifying {src.name}", force=True)
        if md5(src) != md5(dst):
            dst.unlink()
            print(" CHECKSUM MISMATCH - copy removed, original kept on the recorder")
            continue
        print(" verified")
        done_bytes += size
        new.append(dst)
        verified.append((src, dst))
    progress.step(1.0, "copied", force=True)
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


class _NoRun:
    """Stands in for progress.Run when nobody is watching (a run from the terminal)."""
    def begin(self, key): pass
    def set_minutes(self, key, minutes): pass
    def finish(self, error=None): pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", help="drop-off and pickup, e.g. 08:34-15:37")
    ap.add_argument("--day", help="the day the hours are for (YYYY-MM-DD)")
    ap.add_argument("--source", default=settings.S["recorder"]["path"] or None)
    ap.add_argument("--no-copy", action="store_true")
    ap.add_argument("--clear-recorder", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--yes", action="store_true", help="start without asking after the estimate")
    ap.add_argument("--progress", action="store_true", help="report progress for the review page")
    args = ap.parse_args()
    if config.AI_BACKEND == "none":
        args.no_ai = True                # "No AI" chosen in settings: keyword and sound flags only

    from . import estimate, hardware, progress
    conn = connect()
    if args.day and args.hours:          # the page gives the day and its hours up front
        run("kidberry.analyze", "--hours", args.day, args.hours)
    source = Path(args.source) if args.source and not args.no_copy else None
    if args.progress:
        import os
        os.environ["KIDBERRY_PROGRESS"] = "1"   # inherited by every step's process
        est0 = estimate.estimate(conn, source, full=args.full, ai=not args.no_ai)
        tracker = progress.Run({"copy": est0.minutes.get("copy", 0), "transcribe": est0.minutes["transcribe"],
                                "sounds": est0.minutes["sounds"], "translate": 0.5,
                                "ai": est0.minutes.get("ai", 0.3), "summary": 0.2},
                               params={"day": args.day, "hours": args.hours, "ai": not args.no_ai, "full": args.full},
                               log_path=os.environ.get("KIDBERRY_LOG"))
    else:
        tracker = _NoRun()
    try:
        _main(args, conn, source, tracker)
        tracker.finish()
    except SystemExit as e:
        tracker.finish(None if e.code in (None, 0) else str(e.code))
        raise
    except BaseException as e:
        tracker.finish(f"{type(e).__name__}: {e}")
        raise


def _main(args, conn, source, tracker) -> None:
    from . import estimate, hardware
    new = []
    if not args.no_copy:
        say("1/7 Copy from recorder")
        tracker.begin("copy")
        if source:
            new = copy_from_recorder(source, args.clear_recorder)
        else:
            print("No recorder folder set - add recorder.path to data/settings.json or pass --source.")

    # Which day(s) are we processing? The new recordings' days, or the given/latest day.
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
        span = args.hours if args.hours and day == days[-1] and not (args.day and have) else None
        if not span and not have:
            span = ask(f"Drop-off and pickup on {day}, e.g. 08:30-15:40 (Enter to skip): ")
        if span:
            run("kidberry.analyze", "--hours", day, span)
        elif have:
            print(f"{day}: {have['dropoff']}-{have['pickup']} (already set)")
        else:
            print(f"{day}: no hours set - the whole recording will be transcribed and analyzed, incl. the drive and after pickup. "
                  f"Set later with: python -m kidberry.analyze --hours {day} HH:MM-HH:MM")
    hw = hardware.profile()
    say("Estimate")
    print(f"Hardware: {hw.summary()}")
    for note in hw.notes + [hardware.battery_warning()]:
        if note:
            print(f"  ! {note}")
    est = estimate.estimate(conn, None, full=args.full, ai=not args.no_ai)
    if est.files:
        print("\n".join(est.lines(ai=not args.no_ai, cap=config.AI_DAILY_TOKEN_CAP)))
        if not args.no_ai and est.ai_tokens:
            from .analyze import tokens_today
            left = config.AI_DAILY_TOKEN_CAP - tokens_today(conn)
            if est.ai_tokens > left:
                print(f"  ! Only {max(left, 0):,} tokens are left under today's cap: the AI review stops there and "
                      "continues on the next run.")
    else:
        print("Nothing new to transcribe; continuing with the remaining steps.")
    for key in ("transcribe", "sounds", "ai"):
        if key in est.minutes:
            tracker.set_minutes(key, est.minutes[key])
    conn.close()
    if est.files and not args.yes and ask("Start? [Y/n] ").lower() in ("n", "no"):
        sys.exit("Not started. Run again when you are ready.")
    with hardware.keep_awake():
        steps(days, args, tracker)


def steps(days, args, tracker) -> None:
    say("3/7 Transcribe")
    tracker.begin("transcribe")
    run("kidberry.transcribe", str(RAW))
    say("4/7 Sound detection")
    tracker.begin("sounds")
    run("kidberry.sounds")
    say("5/7 Translate other languages (local)")
    tracker.begin("translate")
    run("kidberry.translate")

    tracker.begin("ai")
    for day in days:
        if args.no_ai:
            say(f"6/7 Keyword flags for {day} (AI review skipped)")
            run("kidberry.analyze", "--day", day, "--keywords-only")
        else:
            say(f"6/7 Keyword flags + {'full' if args.full else 'smart'} AI review for {day}")
            run("kidberry.analyze", "--day", day, *(["--full"] if args.full else []))

    say("7/7 Summary")
    tracker.begin("summary")
    run("kidberry.days")
    print(f"\nReview it at http://{config.HOST}:{config.PORT}  (start the page with: python -m kidberry.server)")


if __name__ == "__main__":
    main()
