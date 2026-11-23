"""Progress of a processing run, for the progress bar in the review page.

The daily run (daily.py) owns data/progress.json: which steps there are, their estimated minutes, which
one is running, and when the run started and ended. Each step (a separate process) reports its own
percentage to data/progress_step.json with step(). The page reads both through read().

Steps only report when KIDBERRY_PROGRESS=1 is set (daily.py sets it), so running a step on its own in
a terminal never changes what the page shows.
"""
import json
import os
import time
from datetime import datetime

from .config import DATA_DIR

RUN_FILE = DATA_DIR / "progress.json"
STEP_FILE = DATA_DIR / "progress_step.json"
LOG_DIR = DATA_DIR / "logs"
STEP_LABELS = {"copy": "Copying from the recorder", "transcribe": "Transcribing", "sounds": "Detecting sounds",
               "translate": "Translating", "ai": "Flags and AI review", "summary": "Summary"}
_last_write = 0.0


def _write(path, data) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for attempt in range(20):      # Windows refuses to replace a file another process is reading: retry
        try:
            os.replace(tmp, path)  # atomic: the page never reads half a file
            return
        except PermissionError:
            time.sleep(0.05)
    tmp.unlink(missing_ok=True)    # progress is best-effort; never fail a run over it


def starting(pid: int, params: dict, log_path: str) -> None:
    """Written by the page the moment it starts a run, so a second start is refused even before the run
    has written its own state."""
    _write(STEP_FILE, {})
    _write(RUN_FILE, {"pid": pid, "started": datetime.now().isoformat(timespec="seconds"), "steps": [],
                      "current": None, "done": [], "finished": None, "error": None, "params": params, "log": log_path})


_last_good = {}


def _read(path) -> dict:
    """Read a progress file. While another process is replacing it, Windows can refuse the read for a
    moment: then the last good value is returned, so the bar never jumps back."""
    for _ in range(5):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _last_good[path] = data
            return data
        except FileNotFoundError:
            _last_good.pop(path, None)
            return {}
        except (json.JSONDecodeError, PermissionError, OSError):
            time.sleep(0.03)
    return _last_good.get(path, {})


# ---------------------------------------------------------------- the run (daily.py)

class Run:
    def __init__(self, minutes: dict, params: dict, log_path: str | None = None):
        """minutes: estimated minutes per step, in order (copy, transcribe, sounds, translate, ai, summary)."""
        prev = _read(RUN_FILE)
        started = prev.get("started") if prev.get("pid") == os.getpid() else None   # keep the page's start time
        self.state = {"pid": os.getpid(), "started": started or datetime.now().isoformat(timespec="seconds"),
                      "steps": [{"key": k, "label": STEP_LABELS[k], "minutes": max(0.2, m)} for k, m in minutes.items()],
                      "current": None, "done": [], "finished": None, "error": None, "params": params, "log": log_path}
        _write(STEP_FILE, {})
        _write(RUN_FILE, self.state)

    def begin(self, key: str) -> None:
        if self.state["current"]:
            self.state["done"].append(self.state["current"])
        self.state["current"] = key
        _write(STEP_FILE, {"step": key, "pct": 0.0, "message": ""})
        _write(RUN_FILE, self.state)

    def set_minutes(self, key: str, minutes: float) -> None:
        for s in self.state["steps"]:
            if s["key"] == key:
                s["minutes"] = max(0.2, minutes)
        _write(RUN_FILE, self.state)

    def finish(self, error: str | None = None) -> None:
        if self.state["current"] and not error:
            self.state["done"].append(self.state["current"])
            self.state["current"] = None
        self.state["finished"] = datetime.now().isoformat(timespec="seconds")
        self.state["error"] = error
        _write(RUN_FILE, self.state)


# ---------------------------------------------------------------- a step reports

def step(pct: float, message: str = "", force: bool = False) -> None:
    """Report the current step's progress (0..1). Cheap: writes at most once a second."""
    global _last_write
    if os.environ.get("KIDBERRY_PROGRESS") != "1":
        return
    now = time.time()
    if not force and now - _last_write < 1.0:
        return
    _last_write = now
    cur = _read(STEP_FILE)
    _write(STEP_FILE, {"step": cur.get("step"), "pct": round(max(0.0, min(1.0, pct)), 4), "message": message})


# ---------------------------------------------------------------- the page reads

def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        import psutil
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE and "python" in p.name().lower()
    except Exception:
        return False


def _log_tail(path: str | None, n: int = 6) -> list[str]:
    if not path:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8000))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = [ln.split("\r")[-1].rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    return [ln for ln in lines if ln.strip()][-n:]


def read() -> dict:
    """Everything the page needs: status, overall % and step %, time left, the last log lines."""
    run, cur = _read(RUN_FILE), _read(STEP_FILE)
    if not run:
        return {"status": "idle", "pct": 0.0, "steps": [], "done": [], "elapsed_min": 0.0, "log": []}
    steps = run.get("steps") or []
    total = sum(s["minutes"] for s in steps) or 1
    done_min = sum(s["minutes"] for s in steps if s["key"] in run["done"])
    run.setdefault("done", [])
    current = next((s for s in steps if s["key"] == run.get("current")), None)
    step_pct = cur.get("pct", 0.0) if current and cur.get("step") == run["current"] else 0.0
    pct = (done_min + (current["minutes"] * step_pct if current else 0)) / total
    started = datetime.fromisoformat(run["started"])
    elapsed = (datetime.now() - started).total_seconds() / 60
    if run["finished"]:
        status = "stopped" if run.get("cancelled") else "error" if run["error"] else "done"
    elif _alive(run["pid"]):
        status = "running"
    else:
        status = "stopped"          # the process ended without finishing (closed, killed, crashed)
    # Time left: the estimate for what remains, corrected by how the run is doing against its estimate.
    left = total - done_min - (current["minutes"] * step_pct if current else 0)
    if pct > 0.05 and elapsed > 2:
        left = left * min(3.0, max(0.33, elapsed / max(0.1, total * pct)))
    return {"status": status, "pct": round(pct, 4), "step": run["current"],
            "step_label": current["label"] if current else None, "step_pct": round(step_pct, 4),
            "message": cur.get("message", ""), "steps": steps, "done": run["done"], "started": run["started"],
            "finished": run["finished"], "error": run["error"], "elapsed_min": round(elapsed, 1),
            "left_min": None if status != "running" else round(max(0.0, left), 1), "params": run.get("params", {}),
            "log": _log_tail(run.get("log"))}
