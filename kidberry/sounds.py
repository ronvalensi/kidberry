"""Sound-event detection: crying, screaming, yelling, slaps/impacts - from the audio itself.

Catches incidents that have no clear words. Runs locally (free), on the GPU when there is one.
Only the kindergarten hours (plus a margin) are scanned when they are set for the day.

Usage:
    python -m kidberry.sounds              # every transcribed recording not scanned yet
    python -m kidberry.sounds --rec 3      # one recording
    python -m kidberry.sounds --force      # re-scan
    python -m kidberry.sounds --redetect   # re-apply thresholds to stored scores (seconds, no GPU)
    python -m kidberry.sounds --stats      # how often each sound occurs (for tuning thresholds)

Model: Audio Spectrogram Transformer fine-tuned on Google AudioSet (527 sound classes), 16 kHz native.
The audio is scored in 10-second windows every 5 seconds (every 10 s on a PC without a GPU, to halve the
time); per-window scores are stored, so thresholds can be tuned later with --redetect without re-scanning.
"""
import argparse
import json
import sys
import time

import numpy as np

from . import config, hardware, progress
from .db import connect, covers, process_range

MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"
SR = 16000
WIN = 10.24   # AST's input length
HOP = 5.0
BATCH = 32

# Our sound groups -> AudioSet class names. A group's score is the max over its classes.
GROUPS = {
    "crying":    ["Crying, sobbing", "Baby cry, infant cry", "Whimper", "Wail, moan"],
    "screaming": ["Screaming"],
    "yelling":   ["Shout", "Yell", "Bellow"],
    "children_shouting": ["Children shouting"],   # mostly play; stored, not flagged on its own
    "impact":    ["Slap, smack", "Whack, thwack"],
}

# Detection rules per group: score threshold, and how severity grows with duration.
# Sound alone never proves abuse, so sound flags stay at severity 1-3; the AI/words decide more.
RULES = {
    "crying":    {"threshold": 0.25, "label": "Crying"},
    "screaming": {"threshold": 0.30, "label": "Screaming"},
    "yelling":   {"threshold": 0.30, "label": "Yelling / shouting"},
    "impact":    {"threshold": 0.35, "label": "Slap / impact sound"},
}
MERGE_GAP = 10.0   # seconds of quiet allowed inside one event (timeline markers)

# Which events become flags. Short crying is normal in a toddler group (dozens of times a day), so it
# is only shown on the timeline; a crying *episode* (bouts less than CRY_EPISODE_GAP apart) is flagged
# when the crying in it adds up to CRY_FLAG_MIN seconds.
CRY_EPISODE_GAP = 60.0
CRY_FLAG_MIN = 90.0
CRY_LONG = 240.0          # severity 3 from here


def severity(group: str, duration: float, peak: float) -> int:
    if group == "crying":
        return 3 if duration >= CRY_LONG else 2
    if group in ("screaming", "yelling"):
        return 2 if duration >= 20 or peak >= 0.6 else 1
    return 1


def crying_episodes(events: list[dict]) -> list[dict]:
    eps = []
    for e in (e for e in events if e["group"] == "crying"):
        if eps and e["start"] - eps[-1]["end"] <= CRY_EPISODE_GAP:
            ep = eps[-1]
            ep["end"] = max(ep["end"], e["end"])
            ep["peak"] = max(ep["peak"], e["peak"])
            ep["crying"] += e["end"] - e["start"]
            ep["bouts"] += 1
        else:
            eps.append({**e, "crying": e["end"] - e["start"], "bouts": 1})
    return eps


# ---------------------------------------------------------------- scanning

class Scanner:
    def __init__(self):
        import torch
        from transformers import ASTFeatureExtractor, ASTForAudioClassification
        self.torch = torch
        hw = hardware.profile()
        self.device = "cuda" if hw.device == "cuda" and torch.cuda.is_available() else "cpu"
        self.hop = hw.sound_hop
        if self.device == "cpu":
            torch.set_num_threads(hw.cpu_threads)
        print(f"Loading sound model {MODEL} on {self.device}...")
        self.fe = ASTFeatureExtractor.from_pretrained(MODEL)
        self.model = ASTForAudioClassification.from_pretrained(MODEL).to(self.device).eval()
        if self.device == "cuda":
            self.model = self.model.half()
        label2id = self.model.config.label2id
        missing = [c for cs in GROUPS.values() for c in cs if c not in label2id]
        if missing:
            raise SystemExit(f"AudioSet classes not found in model: {missing}")
        self.classes = sorted({c for cs in GROUPS.values() for c in cs})
        self.class_idx = [label2id[c] for c in self.classes]

    def scan(self, audio, lo: float = 0.0, hi: float | None = None):
        """Yield (start_seconds, {class: score}) for each window between lo and hi seconds."""
        n = int(WIN * SR)
        end = len(audio) if hi is None else min(len(audio), int(hi * SR))
        starts = np.arange(int(lo * SR), max(end - SR, int(lo * SR) + 1), self.hop * SR).astype(int)
        for b in range(0, len(starts), BATCH):
            chunk_starts = starts[b: b + BATCH]
            chunks = []
            for s in chunk_starts:
                c = audio[s: s + n]
                if len(c) < n:
                    c = np.pad(c, (0, n - len(c)))
                chunks.append(c)
            feats = self.fe(chunks, sampling_rate=SR, return_tensors="pt")["input_values"].to(self.device)
            if self.device == "cuda":
                feats = feats.half()
            with self.torch.no_grad():
                probs = self.torch.sigmoid(self.model(feats).logits.float())[:, self.class_idx].cpu().numpy()
            for s, p in zip(chunk_starts, probs):
                yield s / SR, {c: round(float(v), 4) for c, v in zip(self.classes, p)}


def scan_recording(conn, scanner: Scanner, rec, report=None) -> None:
    from .transcribe import AudioFile
    t0 = time.time()
    audio = AudioFile(rec["processed_path"] or rec["path"])
    lo, hi = process_range(conn, rec, len(audio) / SR)
    print(f"  estimated {hardware.fmt_minutes((hi - lo) / hardware.speed('sounds') / 60)} on this PC")
    rows = []
    for start, scores in scanner.scan(audio, lo, hi):
        rows.append((rec["id"], start, json.dumps(scores)))
        if len(rows) % 50 == 0:
            print(f"\r  {(start - lo) / max(hi - lo, 1):6.1%}", end="", flush=True)
            if report:
                report((start - lo) / max(hi - lo, 1))
    with conn:
        conn.execute("DELETE FROM sound_scores WHERE recording_id=?", (rec["id"],))
        conn.executemany("INSERT INTO sound_scores (recording_id, start, scores) VALUES (?,?,?)", rows)
        conn.execute("UPDATE recordings SET sounds_range=? WHERE id=?", (json.dumps([lo, hi]), rec["id"]))
    hardware.record_speed("sounds", hi - lo, time.time() - t0)
    print(f"\r  scanned {len(rows)} windows in {(time.time() - t0) / 60:.1f} min")


# ---------------------------------------------------------------- detection

def group_scores(scores: dict) -> dict:
    return {g: max(scores.get(c, 0.0) for c in cs) for g, cs in GROUPS.items()}


def detect(conn, rec) -> list[dict]:
    """Turn stored window scores into events: runs of windows above a group's threshold."""
    windows = [(r["start"], group_scores(json.loads(r["scores"]))) for r in conn.execute(
        "SELECT start, scores FROM sound_scores WHERE recording_id=? ORDER BY start", (rec["id"],))]
    events = []
    for group, rule in RULES.items():
        cur = None
        for start, gs in windows:
            if gs[group] < rule["threshold"]:
                continue
            end = start + WIN
            if cur and start - cur["end"] <= MERGE_GAP:
                cur["end"] = end
                cur["peak"] = max(cur["peak"], gs[group])
            else:
                if cur:
                    events.append(cur)
                cur = {"group": group, "start": start, "end": end, "peak": gs[group]}
        if cur:
            events.append(cur)
    return sorted(events, key=lambda e: e["start"])


def sound_flags(conn, rec) -> int:
    from .analyze import school_window
    win = school_window(conn, rec)
    events = [e for e in detect(conn, rec) if not win or (e["end"] > win[0] and e["start"] < win[1])]
    segs = conn.execute("SELECT id, start, end FROM segments WHERE recording_id=? AND filtered IS NULL",
                        (rec["id"],)).fetchall()
    from .text import span, t
    responses = conn.execute("SELECT event_start, response, note FROM crying_responses WHERE recording_id=?",
                             (rec["id"],)).fetchall()
    flags = []
    for e in crying_episodes(events):
        if e["crying"] < CRY_FLAG_MIN:
            continue
        resp = episode_response([r for r in responses if e["start"] <= r["event_start"] < e["end"]])
        reason = t("crying", span=span(e["end"] - e["start"]), crying=span(e["crying"]), bouts=e["bouts"],
                   peak=f"{e['peak']:.0%}")
        reason += t(f"resp_{resp}" if resp in ("yes", "no", "unclear") else "resp_none")
        flags.append({**e, "category": "crying", "severity": severity("crying", e["crying"], e["peak"]),
                      "reason": reason, "adult_response": resp})
    for e in events:
        if e["group"] == "crying":
            continue
        dur = e["end"] - e["start"]
        reason = t("sound", label=t(e["group"]), span=span(dur), peak=f"{e['peak']:.0%}")
        flags.append({**e, "category": e["group"], "severity": severity(e["group"], dur, e["peak"]),
                      "reason": reason})
    for f in flags:
        f["ids"] = [s["id"] for s in segs if s["end"] > f["start"] and s["start"] < f["end"]]
    # Keep the parent's verdicts: match by group + start time (rounded to the 5 s window grid).
    key = lambda cat, start: f"{cat}@{int(start // HOP)}"
    reviews = {key(r["category"], r["start"]): r["review"] for r in conn.execute(
        "SELECT category, start, review FROM flags WHERE recording_id=? AND source='sound' AND review IS NOT NULL",
        (rec["id"],))}
    with conn:
        conn.execute("DELETE FROM flags WHERE recording_id=? AND source='sound'", (rec["id"],))
        conn.executemany(
            "INSERT INTO flags (recording_id, start, end, source, category, severity, reason, quote, segment_ids,"
            " review, adult_response) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(rec["id"], f["start"], f["end"], "sound", f["category"], f["severity"], f["reason"], None,
              json.dumps(f["ids"]), reviews.get(key(f["category"], f["start"])), f.get("adult_response"))
             for f in flags])
    return len(flags)


def episode_response(answers: list) -> str | None:
    """One answer for a crying episode from its bouts: any "no" with no "yes" -> no; any "yes" -> yes."""
    got = {a["response"] for a in answers}
    if not got:
        return None
    if "no" in got and "yes" not in got:
        return "no"
    if "yes" in got and "no" not in got:
        return "yes"
    return "unclear"


def stats(conn, rec) -> None:
    rows = [group_scores(json.loads(r["scores"])) for r in conn.execute(
        "SELECT scores FROM sound_scores WHERE recording_id=?", (rec["id"],))]
    if not rows:
        print("  not scanned")
        return
    for g in GROUPS:
        v = np.array([r[g] for r in rows])
        print(f"  {g:<18} windows >=0.2: {(v >= .2).sum():>4}  >=0.3: {(v >= .3).sum():>4}  "
              f">=0.5: {(v >= .5).sum():>4}  max {v.max():.2f}")
    evs = detect(conn, rec)
    by = {}
    for e in evs:
        by[e["group"]] = by.get(e["group"], 0) + 1
    print(f"  events (whole file): {by}")


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rec", type=int, help="only this recording id")
    ap.add_argument("--force", action="store_true", help="re-scan even if already scanned")
    ap.add_argument("--redetect", action="store_true", help="only re-apply thresholds to stored scores")
    ap.add_argument("--stats", action="store_true", help="show score statistics")
    args = ap.parse_args()

    conn = connect()
    q = "SELECT * FROM recordings WHERE status='done'" + (" AND id=?" if args.rec else "") + " ORDER BY started_at"
    recs = conn.execute(q, (args.rec,) if args.rec else ()).fetchall()
    if not recs:
        sys.exit("No transcribed recordings found.")

    scanner = None
    with hardware.keep_awake():
        for i, rec in enumerate(recs):
            progress.step(i / len(recs), f"{rec['name']}")
            print(f"[{rec['id']}] {rec['name']}")
            if args.stats:
                stats(conn, rec)
                continue
            scanned = conn.execute("SELECT COUNT(*) FROM sound_scores WHERE recording_id=?", (rec["id"],)).fetchone()[0]
            wider = scanned and rec["duration"] and not covers(rec["sounds_range"], process_range(conn, rec, rec["duration"]))
            if not args.redetect and (args.force or not scanned or wider):
                scanner = scanner or Scanner()
                scan_recording(conn, scanner, rec,
                               report=lambda f, i=i, n=len(recs), name=rec["name"]: progress.step((i + f) / n, name))
            print(f"  {sound_flags(conn, rec)} sound flags")


if __name__ == "__main__":
    main()
