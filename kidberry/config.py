import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("KIDBERRY_DATA_DIR") or ROOT / "data")   # override: a separate profile, or tests
DB_PATH = DATA_DIR / "kidberry.db"
PROCESSED_DIR = ROOT / "recordings" / "processed"   # volume-boosted copies used for transcription + playback

# Two models: a Hebrew-tuned one, and a general one for the other languages + language detection.
# Only the ones the kindergarten's languages need are loaded (settings.json "languages"): Hebrew-only
# kindergartens never download or load the general model. Each ~30s window goes to the model of its language.
MODEL_GENERAL = os.environ.get("KIDBERRY_MODEL_EN", "large-v3")
MODEL_HEBREW = os.environ.get("KIDBERRY_MODEL_HE", "ivrit-ai/whisper-large-v3-turbo-ct2")
# A window is treated as English only if the detector gives English at least this probability.
ENGLISH_MIN_PROB = float(os.environ.get("KIDBERRY_ENGLISH_MIN_PROB", "0.5"))
SPANISH_MIN_PROB = float(os.environ.get("KIDBERRY_SPANISH_MIN_PROB", "0.6"))  # below this, "Spanish" is usually noise
RUSSIAN_MIN_PROB = float(os.environ.get("KIDBERRY_RUSSIAN_MIN_PROB", "0.6"))
LANGUAGE_MIN_PROB = {"en": ENGLISH_MIN_PROB, "es": SPANISH_MIN_PROB, "ru": RUSSIAN_MIN_PROB}
# GPU or CPU, and how: see hardware.py (KIDBERRY_DEVICE=cpu|cuda and KIDBERRY_COMPUTE override it).

# Audio enhancement applied before transcription. The recorder audio is quiet/muffled, so we
# remove rumble and normalize loudness dynamically (quiet speech is raised, loud bursts are not).
ENHANCE_FILTER = "highpass=f=80,dynaudnorm=f=150:g=15:m=30"

# AI review of transcripts (correction + risk flags). Only transcript TEXT is sent, never audio.
# Chosen in data/settings.json under "ai" (see ai.py); a KIDBERRY_AI_* environment variable overrides it.
def _ai_setting(key: str, env: str, default):
    if os.environ.get(env):
        return os.environ[env]
    try:
        value = json.loads((DATA_DIR / "settings.json").read_text(encoding="utf-8")).get("ai", {}).get(key)
    except (OSError, ValueError, AttributeError):
        value = None
    return default if value in (None, "") else value


# claude-code | anthropic | openai | gemini | none  ("subscription"/"api" are the old names of the first two)
AI_BACKEND = _ai_setting("provider", "KIDBERRY_AI_BACKEND", "claude-code")
AI_BACKEND = {"subscription": "claude-code", "api": "anthropic"}.get(AI_BACKEND, AI_BACKEND)
AI_MODEL = _ai_setting("model", "KIDBERRY_AI_MODEL", "")        # empty = the provider's default
AI_EFFORT = os.environ.get("KIDBERRY_AI_EFFORT", "high")
AI_BLOCK_SEGMENTS = 60     # transcript lines per request
AI_CONTEXT_SEGMENTS = 8    # preceding lines included as context only
# "smart" = review only the minutes around sound/keyword flags (~5x fewer tokens); "full" = every line.
AI_MODE = _ai_setting("mode", "KIDBERRY_AI_MODE", "smart")
AI_NAME_PAD = int(os.environ.get("KIDBERRY_AI_NAME_PAD", "60"))   # smart mode also reviews +/-60 s around the child's name
AI_SMART_PAD = int(os.environ.get("KIDBERRY_AI_SMART_PAD", "180"))   # seconds before/after each trigger
AI_DAILY_TOKEN_CAP = int(_ai_setting("daily_token_cap", "KIDBERRY_AI_DAILY_TOKEN_CAP", 150000))  # stops cleanly
AI_WORKERS = int(_ai_setting("workers", "KIDBERRY_AI_WORKERS", 0)) or None         # requests at a time
_interval = _ai_setting("interval_s", "KIDBERRY_AI_INTERVAL_S", None)               # seconds between requests
AI_INTERVAL_S = float(_interval) if _interval is not None else None

# Model downloads: Windows without Developer Mode cannot use the cache's symlinks; that is fine, so no warning.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

HOST = "127.0.0.1"
PORT = int(os.environ.get("KIDBERRY_PORT", "8000"))
