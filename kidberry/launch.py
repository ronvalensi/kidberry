"""Start Kidberry and open it in the browser. This is what the desktop shortcut runs, with pythonw.exe,
so no console window appears. If Kidberry is already running, it just opens the browser.

The server runs hidden; its output goes to data/logs/server.log. "Quit" on the page stops it.
"""
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import config


def _kidberry_at(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{config.HOST}:{port}/api/name", timeout=1.5) as r:
            return "setup_needed" in json.loads(r.read())
    except Exception:
        return False


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex((config.HOST, port)) != 0


def _message(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, "Kidberry", 0x40)


def main() -> None:
    port = None
    for p in range(config.PORT, config.PORT + 10):    # the usual port, unless another program has it
        if _kidberry_at(p):
            webbrowser.open(f"http://{config.HOST}:{p}/")
            return
        if _free(p):
            port = p
            break
    if port is None:
        _message(f"Kidberry could not find a free port ({config.PORT}-{config.PORT + 9}).")
        return
    log_dir = config.DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable).with_name("python.exe")     # the console twin of pythonw.exe, run hidden
    env = {**os.environ, "KIDBERRY_PORT": str(port), "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    with open(log_dir / "server.log", "ab") as log:
        subprocess.Popen([str(python), "-m", "kidberry.server"], cwd=config.ROOT, stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, env=env,
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    for _ in range(90):                                     # first start can be slow (antivirus scans the files)
        if _kidberry_at(port):
            webbrowser.open(f"http://{config.HOST}:{port}/")
            return
        time.sleep(0.5)
    _message("Kidberry did not start. Details are in:\n" + str(log_dir / "server.log"))


if __name__ == "__main__":
    main()
