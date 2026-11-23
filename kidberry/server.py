"""Local review UI.  Run:  python -m kidberry.server   then open http://127.0.0.1:8000"""
import re
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__, config
from . import ai
from .analyze import school_window
from .db import connect

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Kidberry")
from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/recordings")
def recordings():
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.id, r.name, r.path, r.size_bytes, r.file_mtime, r.started_at, r.duration, r.language,
                      r.status, r.error, r.reviewed_at,
                      (SELECT COUNT(*) FROM flags f WHERE f.recording_id=r.id AND f.review IS NULL) AS open_count,
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
    from . import names, settings
    return {"name": names.NAME, "name_en": names.NAME_EN, "pattern": names.highlight_pattern(),
            "ui_language": settings.ui_language(), "setup_needed": settings.setup_needed(),
            "can_quit": bool(config.INSTALL_DIR), "version": __version__}


@app.post("/api/quit")
def quit_app():
    """Stop Kidberry (the installed app runs hidden, so the page is where it is closed). A processing run
    that is going on is a separate process and keeps going."""
    import os
    import threading
    if not config.INSTALL_DIR:
        raise HTTPException(403, "only the installed app quits from the page")
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"ok": True}


@app.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC / "kidberry.ico")


# ---------------------------------------------------------------- settings (the wizard and the settings page)

def _reload() -> None:
    """Settings changed: re-read them everywhere this server process uses them. (Processing runs are
    separate processes, so each run reads the file fresh anyway.)"""
    import importlib
    from . import hardware, names, settings
    importlib.reload(config)
    importlib.reload(settings)
    importlib.reload(names)
    hardware.profile.cache_clear()


def _settings_view() -> dict:
    from . import hardware, settings
    hw = hardware.profile()
    return {"settings": settings.S, "setup_needed": settings.setup_needed(),
            "providers": [ai.info(k) for k in ai.PROVIDERS], "current_provider": ai.provider(),
            "hardware": {"profile": hw.name, "summary": hw.summary(), "cores": hw.cores},
            "languages": settings.LANGUAGE_NAMES}


@app.get("/api/settings")
def get_settings():
    return _settings_view()


def _words(v, field: str) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    if not isinstance(v, list) or not all(isinstance(w, str) for w in v):
        raise HTTPException(400, f"{field}: a list of words")
    return [w.strip() for w in v if w.strip()][:30]


def _text(v, field: str, n: int = 200) -> str:
    if not isinstance(v, str) or len(v) > n:
        raise HTTPException(400, f"{field}: text up to {n} characters")
    return v.strip()


class SettingsIn(BaseModel):
    settings: dict
    keys: dict = {}            # provider -> new API key ("" = keep the saved one)
    remove_keys: list = []


@app.post("/api/settings")
def save_settings(req: SettingsIn):
    """Save what the form sends. Only known fields are taken, each checked; keys the form does not
    know (hardware, language_notes, ...) are kept as they are. API keys go to data/secrets.json."""
    import json
    from . import settings
    new, out = req.settings, settings.raw()
    if "child" in new:
        c = new["child"] or {}
        out["child"] = {**out.get("child", {}), "name": _text(c.get("name", ""), "child.name", 40),
                        "name_latin": _text(c.get("name_latin", ""), "child.name_latin", 40),
                        **{k: _words(c.get(k, []), f"child.{k}") for k in
                           ("heard_as", "maybe_heard_as", "heard_as_common_word")}}
    if "other_children" in new:
        kids = new["other_children"] or []
        if not isinstance(kids, list) or len(kids) > 60:
            raise HTTPException(400, "other_children: a list")
        out["other_children"] = [{"name": _text(k.get("name", ""), "other_children.name", 40),
                                  "native": _text(k.get("native", ""), "other_children.native", 40),
                                  "heard_as": _words(k.get("heard_as", []), "other_children.heard_as"),
                                  "note": _text(k.get("note", ""), "other_children.note")}
                                 for k in kids if isinstance(k, dict) and (k.get("name") or k.get("native"))]
    if "languages" in new:
        langs = [l for l in (new["languages"] or []) if l in settings.LANGUAGE_NAMES]
        if not langs:
            raise HTTPException(400, "choose at least one language")
        out["languages"] = langs
        out.pop("staff_languages", None)          # the old Spanish switch: replaced by languages
    if "country" in new:
        out["country"] = _text(new["country"], "country", 60) or "Israel"
    if "ui_language" in new:
        if new["ui_language"] not in ("en", "he"):
            raise HTTPException(400, "ui_language: en or he")
        out["ui_language"] = new["ui_language"]
    if "recorder" in new:
        out["recorder"] = {**out.get("recorder", {}), "path": _text((new["recorder"] or {}).get("path", ""),
                                                                    "recorder.path", 260)}
    if "ai" in new:
        a = new["ai"] or {}
        if a.get("provider") not in ai.PROVIDERS:
            raise HTTPException(400, "ai.provider: one of " + ", ".join(ai.PROVIDERS))
        cap = a.get("daily_token_cap", 150000)
        if not isinstance(cap, int) or not 5000 <= cap <= 10_000_000:
            raise HTTPException(400, "ai.daily_token_cap: a number from 5,000 to 10,000,000")
        if a.get("mode", "smart") not in ("smart", "full"):
            raise HTTPException(400, "ai.mode: smart or full")
        out["ai"] = {**out.get("ai", {}), "provider": a["provider"], "model": _text(a.get("model", ""), "ai.model", 80),
                     "daily_token_cap": cap, "mode": a.get("mode", "smart")}
    if "support" in new:
        email = _text((new["support"] or {}).get("email", ""), "support.email", 120)
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise HTTPException(400, "support.email: an email address")
        out["support"] = {"email": email}
    old_lang = settings.ui_language()
    settings.save(out)
    if req.keys or req.remove_keys:
        try:
            secrets = json.loads(ai.SECRETS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            secrets = {}
        for name, key in (req.keys or {}).items():
            if name in ai.PROVIDERS and ai.PROVIDERS[name].get("key") and isinstance(key, str) and key.strip():
                secrets[ai.PROVIDERS[name]["key"]] = key.strip()
        for name in req.remove_keys or []:
            if name in ai.PROVIDERS and ai.PROVIDERS[name].get("key"):
                secrets.pop(ai.PROVIDERS[name]["key"], None)
        ai.SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ai.SECRETS_PATH.write_text(json.dumps(secrets, indent=2), encoding="utf-8")
    _reload()
    if settings.ui_language() != old_lang:      # keyword and sound flag reasons follow the interface language
        import threading
        from . import text
        threading.Thread(target=text.relabel, daemon=True).start()
    return _settings_view()


class FolderCheck(BaseModel):
    path: str


@app.post("/api/settings/check-recorder")
def check_recorder(req: FolderCheck):
    """Is this the recorder's folder? Counts the recordings in it."""
    p = Path(req.path.strip().strip('"'))
    if not req.path.strip() or not p.is_dir():
        return {"exists": False, "wavs": 0}
    try:
        wavs = sum(1 for f in p.iterdir() if f.suffix.lower() == ".wav")
    except OSError:
        return {"exists": False, "wavs": 0}
    return {"exists": True, "wavs": wavs}


@app.post("/api/ai/test")
def ai_test():
    """Run `python -m kidberry.ai --test` with the saved settings: one small made-up batch."""
    import os
    import subprocess
    import sys
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        p = subprocess.run([sys.executable, "-m", "kidberry.ai", "--test"], cwd=config.ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env, timeout=600, creationflags=flags)
        return {"ok": p.returncode == 0, "output": (p.stdout + p.stderr)[-4000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "No answer within 10 minutes."}


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
    review: Literal["concerning", "ok", "false_alarm"] | None   # ok = listened, real, but fine


class BulkReview(BaseModel):
    flag_ids: list[int]
    review: Literal["ok"] | None          # "ok" sets flags with no verdict; None undoes those set to "ok"


@app.post("/api/flags/review")
def review_flags(body: BulkReview):
    """Mark many flags at once (a whole pattern), only those without a verdict; or undo that.
    Returns the ids that changed, so the page can offer Undo."""
    ids = body.flag_ids[:5000]
    if not ids:
        return {"changed": []}
    marks = ",".join("?" * len(ids))
    with connect() as conn:
        where = "review IS NULL" if body.review else "review='ok'"
        changed = [r[0] for r in conn.execute(f"SELECT id FROM flags WHERE id IN ({marks}) AND {where}", ids)]
        if changed:
            conn.execute(f"UPDATE flags SET review=? WHERE id IN ({','.join('?' * len(changed))})", [body.review, *changed])
            conn.commit()
    return {"changed": changed}


class Reviewed(BaseModel):
    reviewed: bool


def _mark_reviewed(conn, rec_ids: list[int], reviewed: bool) -> list[int]:
    """Reviewed: stamp the recordings and make their open flags "ok" (a Concerning verdict stays).
    Not reviewed: only remove the stamp; the flags keep the verdicts they have (Undo restores them)."""
    from datetime import datetime
    if not rec_ids:
        return []
    marks = ",".join("?" * len(rec_ids))
    changed = []
    if reviewed:
        changed = [r[0] for r in conn.execute(
            f"SELECT id FROM flags WHERE recording_id IN ({marks}) AND review IS NULL", rec_ids)]
        conn.execute(f"UPDATE flags SET review='ok' WHERE recording_id IN ({marks}) AND review IS NULL", rec_ids)
    conn.execute(f"UPDATE recordings SET reviewed_at=? WHERE id IN ({marks})",
                 [datetime.now().isoformat(timespec="seconds") if reviewed else None, *rec_ids])
    conn.commit()
    return changed


@app.post("/api/recordings/{rec_id}/reviewed")
def recording_reviewed(rec_id: int, body: Reviewed):
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM recordings WHERE id=?", (rec_id,)).fetchone():
            raise HTTPException(404, "recording not found")
        return {"changed": _mark_reviewed(conn, [rec_id], body.reviewed)}


@app.post("/api/days/{day}/reviewed")
def day_reviewed(day: str, body: Reviewed):
    if not DAY.match(day):
        raise HTTPException(400, "day as YYYY-MM-DD")
    with connect() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM recordings WHERE status='done' AND substr(started_at,1,10)=?",
                                          (day,))]
        if not ids:
            raise HTTPException(404, "no recordings on that day")
        return {"changed": _mark_reviewed(conn, ids, body.reviewed)}


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
    from . import settings
    return {"recorder": rec, "day": day, "support_email": settings.S.get("support", {}).get("email", ""),
            "dropoff": have["dropoff"] if have else "08:00",
            "pickup": have["pickup"] if have else "16:00", "cap": config.AI_DAILY_TOKEN_CAP, "ai": ai.info()}


@app.post("/api/support")
def support_report():
    """Make a support report (see support.py): shown to the parent, who decides whether to send it."""
    from . import support
    name, text = support.report()
    return {"name": name, "text": text}


@app.get("/api/support/{name}")
def support_file(name: str):
    from . import support
    if not re.fullmatch(r"kidberry-support-\d{8}-\d{4}\.txt", name) or not (support.SUPPORT_DIR / name).is_file():
        raise HTTPException(404)
    return FileResponse(support.SUPPORT_DIR / name, media_type="text/plain; charset=utf-8", filename=name)


@app.get("/api/ai")
def ai_providers():
    """The AI providers with their cost and privacy notes, and which one is chosen. Never the keys."""
    return {"current": ai.provider(), "providers": [ai.info(k) for k in ai.PROVIDERS]}


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
            "note_keys": hw.note_keys + ([{"key": "hw.battery"}] if hardware.battery_warning() else []),
            "audio_h": round(sum(f["audio_s"] for f in est.files) / 3600, 2),
            "work_h": round(sum(f["work_s"] for f in est.files) / 3600, 2),
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
