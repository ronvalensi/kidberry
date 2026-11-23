"""A support report: technical facts that help fix a problem, which you read and send yourself.

    python -m kidberry.support         # writes data/support/kidberry-support-<time>.txt and prints it

Kidberry never sends anything on its own. This report is built from an allowlist of technical facts:
the version, Windows, the hardware profile, measured speeds, the chosen languages and AI provider, and
where recent runs failed (the error type and the line of Kidberry's code, never the message).

It never contains audio, transcripts, names, flags, dates or times of recordings, file paths, the
settings file or API keys. As a last check, anything that still looks like a path, an email address,
Hebrew/Russian text or a name from the settings is blanked out.
"""
import json
import platform
import re
import subprocess
import sys
from datetime import datetime

from . import __version__, config, progress, settings

SUPPORT_DIR = config.DATA_DIR / "support"
KNOWN_MODELS = ("ivrit-ai", "faster-whisper", "Systran", "mobiuslabsgmbh", "ast-finetuned", "opus-mt")
_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\w+)')
_EXC = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Hit|Warning))\b")


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=config.ROOT, capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _frame(path: str, line: str, func: str) -> str:
    """Only where in the code: kidberry/transcribe.py:120 in run, or a library's name."""
    p = path.replace("\\", "/")
    if "/kidberry/" in p:
        return f"kidberry/{p.rsplit('/kidberry/', 1)[1]}:{line} in {func}"
    if "site-packages/" in p:
        return f"[{p.rsplit('site-packages/', 1)[1].split('/', 1)[0]}] in {func}"
    return f"[python] in {func}"


def recent_errors(n_logs: int = 10) -> list[dict]:
    """From the last runs' logs: each error's type and the Kidberry code lines it passed through."""
    out = []
    logs = sorted(progress.LOG_DIR.glob("run-*.log"))[-n_logs:] if progress.LOG_DIR.is_dir() else []
    for log in logs:
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").replace("\r", "\n").split("\n")
        except OSError:
            continue
        frames = []
        for ln in lines:
            m = _FRAME.search(ln)
            if m:
                frames.append(_frame(*m.groups()))
                continue
            e = _EXC.match(ln.strip())
            if e and frames:
                out.append({"run": len(out) + 1, "error": e.group(1), "where": frames[-4:]})
                frames = []
    return out[-10:]


def _windows() -> str:
    ver = platform.version()            # e.g. 10.0.26200; Python calls every Windows from 10 on "10"
    try:
        build = int(ver.split(".")[2])
    except (IndexError, ValueError):
        return platform.platform()
    return f"Windows {11 if build >= 22000 else platform.release()} (build {build})"


def _models() -> list[str]:
    from pathlib import Path
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    if not hub.is_dir():
        return []
    return sorted(d.name.removeprefix("models--").replace("--", "/") for d in hub.iterdir()
                  if d.name.startswith("models--") and any(k in d.name for k in KNOWN_MODELS))


def collect() -> dict:
    from . import ai, hardware
    from .db import SCHEMA_VERSION, connect
    hw = hardware.profile()
    try:
        speeds = json.loads(hardware.SPEED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        speeds = {}
    try:
        with connect() as conn:
            n_recs = conn.execute("SELECT count(*) FROM recordings").fetchone()[0]
            by_status = dict(conn.execute("SELECT status, count(*) FROM recordings GROUP BY status").fetchall())
            db_version = conn.execute("PRAGMA user_version").fetchone()[0]
    except Exception as e:  # noqa: BLE001 - a broken database is exactly what a report should show
        n_recs, by_status, db_version = None, {}, f"cannot open ({type(e).__name__})"
    run = progress.read()
    return {
        "kidberry": {"version": __version__, "commit": _git_commit(), "database_version": db_version,
                     "database_version_expected": SCHEMA_VERSION},
        "system": {"windows": _windows(),"python": platform.python_version(),
                   "cpu_cores": hw.cores, "ram_gb": round(hw.ram_gb), "ram_free_gb": round(hw.ram_free_gb)},
        "hardware_profile": {"profile": hw.name, "device": hw.device, "compute": hw.compute_type,
                             "gpu": hw.gpu_name or None, "gpu_memory_gb": round(hw.vram_gb, 1) or None,
                             "threads": hw.cpu_threads, "general_model": hw.model_general, "notes": hw.notes},
        "measured_speed_x_realtime": speeds,
        "models_downloaded": _models(),
        "setup": {"languages": settings.languages(), "ui_language": settings.ui_language(),
                  "child_name_set": bool(settings.S["child"]["name"]),
                  "other_children_count": len(settings.S.get("other_children") or []),
                  "recorder_folder_set": bool(settings.S["recorder"]["path"]),
                  "ai_provider": ai.provider(), "ai_model_set": bool(config.AI_MODEL), "ai_mode": config.AI_MODE,
                  "ai_key_found": bool(ai.api_key(ai.provider())) if ai.PROVIDERS[ai.provider()].get("key") else None},
        "recordings": {"count": n_recs, "by_status": by_status},
        "last_run": {"status": run["status"], "step": run.get("step"), "steps_done": run.get("done", []),
                     "failed": bool(run.get("error")) and run["status"] == "error"},
        "recent_errors": recent_errors(),
    }


def _private_words() -> list[str]:
    S = settings.S
    child = S["child"]
    words = [child.get("name"), child.get("name_latin"), *child.get("heard_as", []), *child.get("maybe_heard_as", []),
             *child.get("heard_as_common_word", [])]
    for kid in S.get("other_children") or []:
        words += [kid.get("name"), kid.get("native"), *(kid.get("heard_as") or [])]
    return [w for w in words if w and len(w) >= 2]


def scrub(text: str) -> str:
    """The last check: blank out anything that looks private, whatever put it there."""
    text = re.sub(r"[A-Za-z]:[\\/][^\s\"',]*", "[path]", text)
    text = re.sub(r"(?<![\w\[])/(?:Users|home)/[^\s\"',]*", "[path]", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)
    text = re.sub(r"[֐-׿؀-ۿЀ-ӿ]+", "[text]", text)   # Hebrew, Arabic, Cyrillic
    for w in sorted(_private_words(), key=len, reverse=True):
        text = re.sub(rf"(?i)\b{re.escape(w)}\b", "[name]", text)
    return text


def report() -> tuple[str, str]:
    """(file name, text). Also saves it in data/support/."""
    body = json.dumps(collect(), ensure_ascii=False, indent=2)
    text = scrub("Kidberry support report\n"
                 f"Made {datetime.now():%Y-%m-%d %H:%M}\n\n"
                 "This report has no audio, transcripts, names, flags, recording dates or file paths.\n"
                 "Please read it before you send it.\n\n" + body + "\n")
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"kidberry-support-{datetime.now():%Y%m%d-%H%M}.txt"
    (SUPPORT_DIR / name).write_text(text, encoding="utf-8")
    return name, text


def main() -> None:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    name, text = report()
    print(text)
    print(f"Saved as {SUPPORT_DIR / name}. Read it, then send the file to whoever helps you.")


if __name__ == "__main__":
    main()
