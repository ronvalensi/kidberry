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
            "SELECT id, start, end, text, corrected, corrected_confidence, translation, language, avg_logprob,"
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
            """SELECT s.recording_id, r.name, r.started_at, s.start, s.end, s.text, s.corrected, s.translation
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


if __name__ == "__main__":
    import uvicorn
    print(f"Kidberry UI: http://{config.HOST}:{config.PORT}")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")
