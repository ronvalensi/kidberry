import shutil
import sqlite3
from datetime import datetime

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

# Columns added before versioning existed (version 1). Added to older databases on connect.
LEGACY_COLUMNS = {
    "recordings": {"processed_path": "TEXT", "started_at": "TEXT", "analyzed_at": "TEXT"},
    "segments": {"language": "TEXT", "compression_ratio": "REAL", "filtered": "TEXT",
                 "corrected": "TEXT",              # AI best guess of what was actually said
                 "corrected_confidence": "TEXT",   # high | medium | low
                 "translation": "TEXT",            # translation of a non-Hebrew line (see translation_lang)
                 "translated_from": "TEXT"},       # the text it was translated from, to detect stale ones
    "flags": {"quote": "TEXT", "segment_ids": "TEXT",
              "adult_response": "TEXT"},           # crying flags: yes | no | unclear (from the AI)
}


def _add_columns(conn, table: str, cols: dict) -> None:
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for col, typ in cols.items():
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


# Numbered schema upgrades. The database remembers its version (PRAGMA user_version); on connect every
# newer step runs once, in order, after a backup copy is made. Only ever APPEND steps here - never edit or
# reorder old ones, because people's databases are already past them.
def _v1_legacy_columns(conn):
    for table, cols in LEGACY_COLUMNS.items():
        _add_columns(conn, table, cols)


def _v2_transcribe_checkpoint(conn):
    # How far transcription got (seconds into the file), so an interrupted file resumes instead of restarting.
    _add_columns(conn, "recordings", {"transcribed_until": "REAL"})


def _v3_translation_language(conn):
    # Translations now go into the interface language (English or Hebrew); remember which one each is in.
    _add_columns(conn, "segments", {"translation_lang": "TEXT"})
    conn.execute("UPDATE segments SET translation_lang='en' WHERE translation IS NOT NULL")


UPGRADES = [_v1_legacy_columns, _v2_transcribe_checkpoint, _v3_translation_language]
SCHEMA_VERSION = len(UPGRADES)


def _upgrade(conn) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"The database is from a newer Kidberry (schema {version}, this version knows {SCHEMA_VERSION}). "
                           "Update Kidberry.")
    if version == SCHEMA_VERSION:
        return
    if version > 0 or conn.execute("SELECT count(*) FROM recordings").fetchone()[0]:
        backup = DB_PATH.with_name(f"{DB_PATH.stem}.before-v{SCHEMA_VERSION}-{datetime.now():%Y%m%d-%H%M%S}.db")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copy2(DB_PATH, backup)
        print(f"Upgrading the database to version {SCHEMA_VERSION} (backup: {backup.name})")
    for step in UPGRADES[version:]:
        with conn:
            step(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _upgrade(conn)
    return conn
