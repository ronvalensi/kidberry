import sqlite3
from .config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id             INTEGER PRIMARY KEY,
    path           TEXT UNIQUE NOT NULL,   -- original file (never modified)
    processed_path TEXT,                   -- enhanced copy used for transcription + playback
    name           TEXT NOT NULL,
    size_bytes     INTEGER,
    file_mtime     REAL,
    started_at     TEXT,                   -- wall-clock start, ISO, from the recorder's filename
    duration       REAL,
    language       TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending | processing | done | error
    error          TEXT,
    processed_at   TEXT
);
CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY,
    recording_id   INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    start          REAL NOT NULL,
    end            REAL NOT NULL,
    text           TEXT NOT NULL,
    language       TEXT,
    avg_logprob    REAL,
    no_speech_prob REAL,
    compression_ratio REAL,
    filtered       TEXT,                   -- NULL = real speech; else why it looks like a hallucination
    words_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_segments_rec ON segments(recording_id, start);
-- Filled by the analysis phases (keywords, sound events, AI review).
CREATE TABLE IF NOT EXISTS flags (
    id           INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    start        REAL NOT NULL,
    end          REAL NOT NULL,
    source       TEXT NOT NULL,      -- keyword | sound | ai
    category     TEXT,
    severity     INTEGER,            -- 1 (low) .. 5 (high)
    reason       TEXT,
    review       TEXT                -- NULL | concerning | false_alarm
);
-- Drop-off / pickup per day. Analysis (AI review, flags) only covers this window; days without a row
-- are analyzed in full.
CREATE TABLE IF NOT EXISTS school_hours (
    day     TEXT PRIMARY KEY,   -- YYYY-MM-DD
    dropoff TEXT NOT NULL,      -- HH:MM
    pickup  TEXT NOT NULL       -- HH:MM
);
-- Sound-model scores per 10 s window (every 5 s), kept so thresholds can be retuned without re-scanning.
CREATE TABLE IF NOT EXISTS sound_scores (
    id           INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    start        REAL NOT NULL,
    scores       TEXT NOT NULL      -- JSON {AudioSet class: probability}
);
CREATE INDEX IF NOT EXISTS idx_sound_rec ON sound_scores(recording_id, start);
-- AI's answer per detected crying event: did an adult respond?
CREATE TABLE IF NOT EXISTS crying_responses (
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    event_start  REAL NOT NULL,     -- start of the crying sound event (seconds into the file)
    response     TEXT NOT NULL,     -- yes | no | unclear
    note         TEXT,
    PRIMARY KEY (recording_id, event_start)
);
-- One row per AI request, to measure and cap usage.
CREATE TABLE IF NOT EXISTS ai_usage (
    id           INTEGER PRIMARY KEY,
    at           TEXT NOT NULL,      -- ISO timestamp
    day          TEXT NOT NULL,      -- YYYY-MM-DD, for the daily cap
    backend      TEXT NOT NULL,      -- subscription | api
    recording_id INTEGER,
    tokens_in    INTEGER NOT NULL,   -- incl. cached input
    tokens_out   INTEGER NOT NULL,   -- incl. thinking
    cost_usd     REAL                -- API list-price equivalent
);
"""

# Columns added after the first version; added to existing databases on connect.
MIGRATIONS = {
    "recordings": {"processed_path": "TEXT", "started_at": "TEXT", "analyzed_at": "TEXT"},
    "segments": {"language": "TEXT", "compression_ratio": "REAL", "filtered": "TEXT",
                 "corrected": "TEXT",              # AI best guess of what was actually said
                 "corrected_confidence": "TEXT",   # high | medium | low
                 "translation": "TEXT",            # English translation (Spanish lines)
                 "translated_from": "TEXT"},       # the text it was translated from, to detect stale ones
    "flags": {"quote": "TEXT", "segment_ids": "TEXT",
              "adult_response": "TEXT"},           # crying flags: yes | no | unclear (from the AI)
}


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    return conn
