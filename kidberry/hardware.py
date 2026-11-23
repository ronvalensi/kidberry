"""What this PC can do, and how Kidberry should run on it.

    python -m kidberry.hardware      # show the profile, memory check and estimated time for a day

Profiles:
    gpu        NVIDIA GPU with 6 GB+ memory: float16, the full general model (fastest)
    gpu-small  NVIDIA GPU with 3-6 GB: int8_float16 (compressed, same models)
    cpu        no usable GPU: int8 on all CPU cores, the lighter "turbo" general model, and the sound
               detector checks every 10 s instead of every 5 s. A day takes hours: let it run overnight.

Override in data/settings.json: "hardware": {"device": "cpu"} (or "gpu"), or with KIDBERRY_DEVICE=cpu|cuda.
"""
import ctypes
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache

from . import config, settings

SPEED_FILE = config.DATA_DIR / "speed.json"   # measured speeds on this PC, to estimate how long a day takes
# Starting guesses (times realtime) until this PC has been measured. From tests on an RTX 3080 Ti and a
# 16-core CPU limited to 4 threads (a laptop-like CPU).
DEFAULT_SPEED = {"gpu": {"transcribe": 12, "sounds": 300}, "gpu-small": {"transcribe": 6, "sounds": 150},
                 "cpu": {"transcribe": 2, "sounds": 8}}
# Rough memory (GB of RAM) each part needs while it runs.
RAM_NEED = {"gpu": 3.0, "gpu-small": 3.0, "cpu": 5.0}


@dataclass
class Profile:
    name: str                     # gpu | gpu-small | cpu
    device: str                   # cuda | cpu (for faster-whisper and torch)
    compute_type: str             # float16 | int8_float16 | int8
    cpu_threads: int
    model_general: str
    sound_hop: float              # seconds between sound-detector windows
    gpu_name: str = ""
    vram_gb: float = 0.0
    ram_gb: float = 0.0
    ram_free_gb: float = 0.0
    cores: int = 0
    notes: list = field(default_factory=list)
    note_keys: list = field(default_factory=list)   # the same notes for the page to show in its language

    def summary(self) -> str:
        hw = f"{self.gpu_name} ({self.vram_gb:.0f} GB)" if self.device == "cuda" else f"CPU, {self.cores} cores"
        return f"{self.name} - {hw}, {self.compute_type}, RAM {self.ram_free_gb:.0f} of {self.ram_gb:.0f} GB free"


def _gpu() -> tuple[str, float]:
    """(name, memory GB) of the first NVIDIA GPU that faster-whisper can use, or ("", 0)."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return "", 0.0
    except Exception:
        return "", 0.0
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        name, mem = [x.strip() for x in out.split(",")]
        return name, float(mem) / 1024
    except Exception:
        return "NVIDIA GPU", 0.0


@lru_cache(maxsize=1)
def profile() -> Profile:
    import psutil
    vm = psutil.virtual_memory()
    cores = psutil.cpu_count(logical=False) or os.cpu_count() or 4
    want = (os.environ.get("KIDBERRY_DEVICE") or (settings.S.get("hardware") or {}).get("device") or "auto").lower()
    gpu_name, vram = _gpu() if want != "cpu" else ("", 0.0)
    base = dict(cpu_threads=cores, gpu_name=gpu_name, vram_gb=vram, ram_gb=vm.total / 1e9,
                ram_free_gb=vm.available / 1e9, cores=cores)
    notes, keys = [], []
    if gpu_name and (vram >= 6 or (vram == 0 and want in ("gpu", "cuda"))):
        p = Profile("gpu", "cuda", "float16", model_general=config.MODEL_GENERAL, sound_hop=5.0, **base)
    elif gpu_name and vram >= 3:
        p = Profile("gpu-small", "cuda", "int8_float16", model_general=config.MODEL_GENERAL, sound_hop=5.0, **base)
    else:
        if want in ("gpu", "cuda"):
            notes.append("A GPU was requested, but no usable NVIDIA GPU was found - running on the CPU.")
            keys.append({"key": "hw.no_gpu"})
        elif gpu_name:
            notes.append(f"{gpu_name} has only {vram:.1f} GB - running on the CPU.")
            keys.append({"key": "hw.small_gpu", "gpu": gpu_name, "gb": round(vram, 1)})
        p = Profile("cpu", "cpu", "int8", model_general=os.environ.get("KIDBERRY_MODEL_EN_CPU", "large-v3-turbo"),
                    sound_hop=10.0, **base)
    if os.environ.get("KIDBERRY_COMPUTE"):
        p.compute_type = os.environ["KIDBERRY_COMPUTE"]
    if p.ram_free_gb < RAM_NEED[p.name]:
        notes.append(f"Only {p.ram_free_gb:.1f} GB of memory is free; Kidberry needs about {RAM_NEED[p.name]:.0f} GB. "
                     "Close other programs (browsers especially) before processing, or it may be stopped.")
        keys.append({"key": "hw.low_ram", "free": round(p.ram_free_gb, 1), "need": RAM_NEED[p.name]})
    p.notes = notes
    p.note_keys = keys
    return p


# ---------------------------------------------------------------- speed and time estimates

def _speeds() -> dict:
    try:
        return json.loads(SPEED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def speed(step: str) -> float:
    """Times realtime for a step on this PC: measured if known, else the profile's starting guess."""
    p = profile()
    return _speeds().get(p.name, {}).get(step) or DEFAULT_SPEED[p.name][step]


def record_speed(step: str, audio_seconds: float, wall_seconds: float) -> None:
    """Remember how fast a step ran (smoothed), so later estimates fit this PC."""
    if audio_seconds < 60 or wall_seconds < 5:
        return
    data, p = _speeds(), profile()
    new = audio_seconds / wall_seconds
    old = data.get(p.name, {}).get(step)
    data.setdefault(p.name, {})[step] = round(new if old is None else 0.5 * old + 0.5 * new, 2)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    SPEED_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def estimate_minutes(audio_seconds: float, steps=("transcribe", "sounds")) -> float:
    return sum(audio_seconds / speed(s) for s in steps) / 60


def fmt_minutes(m: float) -> str:
    if m < 1:
        return "under a minute"
    return f"{m:.0f} min" if m < 90 else f"{m / 60:.1f} hours"


# ---------------------------------------------------------------- laptops

class keep_awake:
    """Stop Windows from sleeping while processing runs (the screen may still turn off)."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __enter__(self):
        if sys.platform == "win32":
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
        return self

    def __exit__(self, *exc):
        if sys.platform == "win32":
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)


def battery_warning() -> str | None:
    try:
        import psutil
        b = psutil.sensors_battery()
    except Exception:
        return None
    if b and not b.power_plugged:
        return (f"The laptop is on battery ({b.percent:.0f}%). Processing takes a long time and drains the battery - "
                "plug in the charger.")
    return None


def main() -> None:
    p = profile()
    print(f"Profile: {p.summary()}")
    print(f"  speech models on {p.device} ({p.compute_type}, {p.cpu_threads} CPU threads); "
          f"general model: {p.model_general}; sound detector every {p.sound_hop:.0f} s")
    for n in p.notes:
        print("  !", n)
    if (w := battery_warning()):
        print("  !", w)
    hours = 8
    measured = "measured on this PC" if _speeds().get(p.name) else "first guess until measured"
    print(f"  an {hours}-hour kindergarten day takes about {fmt_minutes(estimate_minutes(hours * 3600))} "
          f"(transcription {speed('transcribe'):.0f}x and sounds {speed('sounds'):.0f}x realtime, {measured})")


if __name__ == "__main__":
    main()
