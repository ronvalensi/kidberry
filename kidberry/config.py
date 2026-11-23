import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "kidberry.db"
PROCESSED_DIR = ROOT / "recordings" / "processed"   # volume-boosted copies used for transcription + playback

# Two models: a general one (language detection + English) and a Hebrew-tuned one.
# Each ~30s window is routed to the model of its detected language (Hebrew or English only).
MODEL_GENERAL = os.environ.get("KIDBERRY_MODEL_EN", "large-v3")
MODEL_HEBREW = os.environ.get("KIDBERRY_MODEL_HE", "ivrit-ai/whisper-large-v3-turbo-ct2")
# A window is treated as English only if the detector gives English at least this probability.
ENGLISH_MIN_PROB = float(os.environ.get("KIDBERRY_ENGLISH_MIN_PROB", "0.5"))
SPANISH_MIN_PROB = float(os.environ.get("KIDBERRY_SPANISH_MIN_PROB", "0.6"))  # below this, "Spanish" is usually noise
DEVICE = os.environ.get("KIDBERRY_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("KIDBERRY_COMPUTE", "float16")

# Audio enhancement applied before transcription. The recorder audio is quiet/muffled, so we
# remove rumble and normalize loudness dynamically (quiet speech is raised, loud bursts are not).
ENHANCE_FILTER = "highpass=f=80,dynaudnorm=f=150:g=15:m=30"

# AI review of transcripts (correction + risk flags). Only transcript TEXT is sent, never audio.
# The API key is read from the ANTHROPIC_API_KEY environment variable or from a .env file in ROOT.
AI_MODEL = os.environ.get("KIDBERRY_AI_MODEL", "claude-opus-5-5")
AI_EFFORT = os.environ.get("KIDBERRY_AI_EFFORT", "high")
AI_BLOCK_SEGMENTS = 60     # transcript lines per request
AI_CONTEXT_SEGMENTS = 8    # preceding lines included as context only
# "subscription" = run through the Claude Code CLI on your Claude plan (no API key, no extra bill).
# "api" = Anthropic API key from .env, pay-as-you-go.
# "smart" = review only the minutes around sound/keyword flags (~5x fewer tokens); "full" = every line.
AI_MODE = os.environ.get("KIDBERRY_AI_MODE", "smart")
AI_NAME_PAD = int(os.environ.get("KIDBERRY_AI_NAME_PAD", "60"))   # smart mode also reviews +/-60 s around the child's name
AI_SMART_PAD = int(os.environ.get("KIDBERRY_AI_SMART_PAD", "180"))   # seconds before/after each trigger
AI_BACKEND = os.environ.get("KIDBERRY_AI_BACKEND", "subscription")
AI_DAILY_TOKEN_CAP = int(os.environ.get("KIDBERRY_AI_DAILY_TOKEN_CAP", "150000"))  # stops cleanly when reached

HOST = "127.0.0.1"
PORT = int(os.environ.get("KIDBERRY_PORT", "8000"))
