"""Local review UI.  Run:  python -m kidberry.server   then open http://127.0.0.1:8000"""
import re
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import config
from .analyze import school_window
from .db import connect

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Kidberry")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/recordings")
def recordings():
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.id, r.name, r.path, r.size_bytes, r.file_mtime, r.started_at, r.duration, r.language,
                      r.status, r.error,
                      (SELECT COUNT(*) FROM segments s WHERE s.recording_id=r.id AND s.filtered IS NULL)
                          AS segment_count,
                      (SELECT COUNT(*) FROM flags f WHERE f.recording_id=r.id
                          AND COALESCE(f.review, '') != 'false_alarm') AS flag_count,
                      (SELECT MAX(severity) FROM flags f WHERE f.recording_id=r.id
                          AND COALESCE(f.review, '') != 'false_alarm') AS max_severity
               FROM recordings r ORDER BY r.file_mtime"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            win = school_window(conn, r)   # kindergarten hours as offsets into this file, or None
            d["school_from"], d["school_to"] = win if win else (None, None)
            out.append(d)
    return out


@app.get("/api/recordings/{rec_id}/segments")
def segments(rec_id: int):
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, start, end, text, corrected, corrected_confidence, translation, translation_lang, language, avg_logprob,"
            " no_speech_prob,"
            " filtered FROM segments"
            " WHERE recording_id=? ORDER BY start", (rec_id,)
        ).fetchall()
    from .names import mention
    return [{**dict(r), "mention": mention(r["text"], r["corrected"])} for r in rows]


@app.get("/api/name")
def child_name():
    from .names import NAME, NAME_EN, highlight_pattern
    return {"name": NAME, "name_en": NAME_EN, "pattern": highlight_pattern()}


@app.get("/api/mentions")
def mentions():
    """Every line where the child's name (or something that sounds like it) is said, across all recordings."""
    from .names import mention
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.recording_id, r.name, r.started_at, s.start, s.end, s.text, s.corrected, s.translation, s.translation_lang
               FROM segments s JOIN recordings r ON r.id=s.recording_id
               WHERE s.filtered IS NULL ORDER BY r.started_at, s.start""").fetchall()
        out = []
        for r in rows:
            tier = mention(r["text"], r["corrected"])
            if tier:
                out.append({**dict(r), "mention": tier, "shown": r["corrected"] or r["text"]})
    return out


@app.get("/api/recordings/{rec_id}/flags")
def flags(rec_id: int):
    with connect() as conn:
        rows = conn.execute("SELECT * FROM flags WHERE recording_id=? ORDER BY start", (rec_id,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/days")
def days_page():
    return FileResponse(STATIC / "days.html")


@app.get("/api/days")
def days():
    from .days import day_stats, patterns
    with connect() as conn:
        stats = day_stats(conn)
    return {"days": stats, **patterns(stats)}


@app.get("/api/recordings/{rec_id}/sounds")
def sounds(rec_id: int):
    """All detected sound events (incl. short crying that is not flagged), for the timeline."""
    from .sounds import detect
    with connect() as conn:
        rec = conn.execute("SELECT * FROM recordings WHERE id=?", (rec_id,)).fetchone()
        if not rec:
            raise HTTPException(404, "recording not found")
        return detect(conn, rec)


class Review(BaseModel):
    review: Literal["concerning", "false_alarm"] | None


@app.post("/api/flags/{flag_id}/review")
def review_flag(flag_id: int, body: Review):
    """The parent's verdict on a flag after listening. Kept for tuning the analysis later."""
    with connect() as conn:
        cur = conn.execute("UPDATE flags SET review=? WHERE id=?", (body.review, flag_id))
    if not cur.rowcount:
        raise HTTPException(404, "flag not found")
    return {"ok": True}


@app.get("/api/search")
def search(q: str, limit: int = 300):
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.recording_id, r.name, s.start, s.end, COALESCE(s.corrected, s.text) AS text FROM segments s
               JOIN recordings r ON r.id=s.recording_id
               WHERE (s.text LIKE ? OR s.corrected LIKE ? OR s.translation LIKE ?) AND s.filtered IS NULL
               ORDER BY r.file_mtime, s.start LIMIT ?""",
            (f"%{q}%", f"%{q}%", f"%{q}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/recordings/{rec_id}/audio")
def audio(rec_id: int, original: bool = False, range: str | None = Header(default=None)):
    """Serve the enhanced WAV (or the untouched original) with HTTP Range support for instant seeking."""
    with connect() as conn:
        row = conn.execute("SELECT path, processed_path FROM recordings WHERE id=?", (rec_id,)).fetchone()
    if not row:
        raise HTTPException(404, "recording not found")
    path = Path(row["path"] if original or not row["processed_path"] else row["processed_path"])
    if not path.is_file():
        raise HTTPException(404, f"audio file not found: {path}")
    size = path.stat().st_size

    m = re.fullmatch(r"bytes=(\d*)-(\d*)", range or "")
    if not m or (not m[1] and not m[2]):
        return FileResponse(path, media_type="audio/wav", headers={"Accept-Ranges": "bytes"})
    if m[1]:
        start, end = int(m[1]), (int(m[2]) if m[2] else size - 1)
    else:  # suffix range: the last N bytes
        start, end = max(size - int(m[2]), 0), size - 1
    end = min(end, size - 1)
    if start > end:
        raise HTTPException(416, "range not satisfiable")

    def body(chunk: int = 1024 * 1024):
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = f.read(min(chunk, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(body(), status_code=206, media_type="audio/wav", headers={
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(end - start + 1),
    })


# ---------------------------------------------------------------- processing runs from the page

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
import threading  # noqa: E402
_start_lock = threading.Lock()   # the page's requests run in threads: one start at a time
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _recorder() -> dict:
    """Is the recorder plugged in, and what new recordings does it hold (by day)?"""
    from . import settings
    from .estimate import RAW
    from .transcribe import started_at_from_name
    path = settings.S["recorder"]["path"]
    if not path:
        return {"path": None, "connected": False, "new": [], "days": []}
    src = Path(path)
    if not src.is_dir():
        return {"path": path, "connected": False, "new": [], "days": []}
    new = [f.name for f in sorted(src.iterdir()) if f.suffix.lower() == ".wav"
           and not ((RAW / f.name).exists() and (RAW / f.name).stat().st_size == f.stat().st_size)]
    days = sorted({(started_at_from_name(n) or "")[:10] for n in new} - {""})
    return {"path": path, "connected": True, "new": new, "days": days}


@app.get("/api/job")
def job_status():
    from . import progress
    return progress.read()


@app.get("/api/job/setup")
def job_setup():
    """What the import form needs: the recorder's state, a suggested day and hours."""
    from datetime import date, datetime
    rec = _recorder()
    day = rec["days"][-1] if rec["days"] else date.today().isoformat()
    with connect() as conn:
        have = conn.execute("SELECT dropoff, pickup FROM school_hours WHERE day=?", (day,)).fetchone()
        if not have:   # same weekday last time, else the latest day
            wd = datetime.strptime(day, "%Y-%m-%d").strftime("%w")
            have = (conn.execute("SELECT dropoff, pickup FROM school_hours WHERE strftime('%w', day)=? "
                                 "ORDER BY day DESC LIMIT 1", (wd,)).fetchone()
                    or conn.execute("SELECT dropoff, pickup FROM school_hours ORDER BY day DESC LIMIT 1").fetchone())
    return {"recorder": rec, "day": day, "dropoff": have["dropoff"] if have else "08:00",
            "pickup": have["pickup"] if have else "16:00", "cap": config.AI_DAILY_TOKEN_CAP}


@app.get("/api/job/estimate")
def job_estimate(day: str, dropoff: str, pickup: str, ai: bool = True, full: bool = False):
    """The estimate for these hours - computed with the hours applied but not saved (rolled back)."""
    from . import hardware, settings
    from .analyze import tokens_today
    from .estimate import estimate
    if not (DAY.match(day) and HHMM.match(dropoff) and HHMM.match(pickup)) or dropoff >= pickup:
        raise HTTPException(400, "give the day as YYYY-MM-DD and times as HH:MM, drop-off before pickup")
    conn = connect()
    try:
        conn.execute("INSERT OR REPLACE INTO school_hours (day, dropoff, pickup) VALUES (?,?,?)", (day, dropoff, pickup))
        src = settings.S["recorder"]["path"]
        est = estimate(conn, Path(src) if src else None, full=full, ai=ai)
        used = tokens_today(conn)
    finally:
        conn.rollback()
        conn.close()
    hw = hardware.profile()
    return {"files": est.files, "minutes": {k: round(v, 1) for k, v in est.minutes.items() if ai or k != "ai"},
            "total_minutes": round(est.total_minutes - (0 if ai else est.minutes.get("ai", 0)), 1),
            "ai_tokens": est.ai_tokens if ai else 0, "tokens_used_today": used, "cap": config.AI_DAILY_TOKEN_CAP,
            "hardware": hw.summary(), "profile": hw.name, "notes": hw.notes + [n for n in [hardware.battery_warning()] if n],
            "lines": est.lines(ai=ai, cap=config.AI_DAILY_TOKEN_CAP)}


class JobStart(BaseModel):
    day: str
    dropoff: str
    pickup: str
    ai: bool = True
    full: bool = False


@app.post("/api/job/start")
def job_start(req: JobStart):
    import os
    import subprocess
    import sys
    from datetime import datetime
    from . import progress
    if not (DAY.match(req.day) and HHMM.match(req.dropoff) and HHMM.match(req.pickup)) or req.dropoff >= req.pickup:
        raise HTTPException(400, "give the day as YYYY-MM-DD and times as HH:MM, drop-off before pickup")
    with _start_lock:
        if progress.read().get("status") == "running":
            raise HTTPException(409, "a run is already in progress")
        return _spawn(req)


def _spawn(req) -> dict:
    import os
    import subprocess
    import sys
    from datetime import datetime
    from . import progress
    progress.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = progress.LOG_DIR / f"run-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
    cmd = [sys.executable, "-u", "-m", "kidberry.daily", "--progress", "--yes", "--day", req.day,
           "--hours", f"{req.dropoff}-{req.pickup}"] + (["--no-ai"] if not req.ai else []) + (["--full"] if req.full else [])
    env = {**os.environ, "KIDBERRY_LOG": str(log), "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0
    with open(log, "wb") as out:
        proc = subprocess.Popen(cmd, cwd=config.ROOT, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, creationflags=flags, start_new_session=sys.platform != "win32")
    progress.starting(proc.pid, {"day": req.day, "hours": f"{req.dropoff}-{req.pickup}", "ai": req.ai, "full": req.full},
                      str(log))
    return {"started": True, "log": log.name}


@app.post("/api/job/cancel")
def job_cancel():
    """Stop the run and everything it started. Transcription continues from its last checkpoint next time."""
    import psutil
    from . import progress
    run = progress._read(progress.RUN_FILE)
    pid = run.get("pid")
    if not pid or progress.read().get("status") != "running":
        return {"stopped": False}
    try:
        parent = psutil.Process(pid)
        procs = parent.children(recursive=True) + [parent]
        for p in procs:
            p.kill()
        psutil.wait_procs(procs, timeout=10)
    except psutil.NoSuchProcess:
        pass
    run["finished"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    run["error"] = "Stopped by you. Continue to pick up where it stopped."
    run["cancelled"] = True
    progress._write(progress.RUN_FILE, run)
    return {"stopped": True}


if __name__ == "__main__":
    import uvicorn
    print(f"Kidberry UI: http://{config.HOST}:{config.PORT}")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")
