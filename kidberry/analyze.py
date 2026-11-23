"""Analyze transcripts: AI correction of misheard words + risk flags, then keyword flags.

Usage:
    python -m kidberry.analyze                 # every transcribed recording not fully analyzed (resumes)
    python -m kidberry.analyze --rec 2         # one recording
    python -m kidberry.analyze --blocks 1      # only N batches (a cheap test)
    python -m kidberry.analyze --force         # redo the AI review on everything
    python -m kidberry.analyze --keywords-only # only re-apply keywords.py (free, local, seconds)
    python -m kidberry.analyze --usage         # show AI token usage per day

The AI review runs on your Claude subscription through the Claude Code CLI by default
(KIDBERRY_AI_BACKEND=subscription), or on an API key with KIDBERRY_AI_BACKEND=api.
It stops cleanly at KIDBERRY_AI_DAILY_TOKEN_CAP tokens per day; the next run continues where it stopped.
Only transcript text is sent - never audio.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from . import config, keywords, settings
from .db import connect

PRICE_IN, PRICE_OUT = 4.0, 20.0   # USD per 1M tokens for claude-opus-5-5 (API list price)
EST_TOKENS_PER_BLOCK = 9000       # used only to avoid starting batches that would cross the daily cap

def _intro() -> str:
    """The first paragraph of the AI instructions, built from settings.json (child, other children, languages)."""
    st = settings.S
    c, kids, lang = st["child"], st["other_children"], st["staff_languages"]
    parts = [f"You are helping a parent review transcripts of audio recorded during their child's day at a kindergarten in {st['country']}. "
             "The recorder sits in the child's bag, so the audio is muffled, and the transcript comes from automatic speech recognition: "
             "many Hebrew words are misrecognized (similar-sounding words swapped, words split or merged)."]
    if lang.get("spanish"):
        note = f" ({lang['spanish_note']})" if lang.get("spanish_note") else ""
        parts.append(f"Most lines are Hebrew, some English, and some Spanish: several staff members speak Spanish{note} to each other "
                     "and sometimes to the children. Speech recognition sometimes writes Spanish out phonetically in Hebrew letters; "
                     'when a "he" line is clearly Spanish, reconstruct it in Spanish.')
    else:
        parts.append("Most lines are Hebrew, some English.")
    parts.append("Speakers are kindergarten staff (גננת, סייעת, nannies), children, and parents at drop-off and pickup.")
    if c["name"]:
        n = c["name"]
        latin = f" ({c['name_latin']})" if c.get("name_latin") else ""
        heard = [*c["heard_as"], *c["maybe_heard_as"], *c["heard_as_common_word"]]
        parts.append(f"The parent's child is named {n}{latin}.")
        if heard:
            nick = f" ({c['nickname_note']})" if c.get("nickname_note") else ""
            parts.append(f"Speech recognition often mishears the name as {', '.join(heard)}{nick}. "
                         f"When a line is clearly calling or talking about the child, write {n} in the correction"
                         + (f" (but keep {'/'.join(c['heard_as_common_word'])} when it is the everyday word)" if c["heard_as_common_word"] else "")
                         + f". Only write {n} where the recognizer's word is one of these variants; never turn other words into the name. "
                         "If unsure, keep the recognized word.")
        if kids:
            def kid(k):
                extra = [x for x in [k.get("native"), *(f"often misheard as {h}" for h in k.get("heard_as", [])), k.get("note")] if x]
                return k["name"] + (f" ({', '.join(extra)})" if extra else "")
            parts.append(f"Other children in the group include {', '.join(kid(k) for k in kids)}: they are not {n}.")
        parts.append(f"Pay extra attention to how adults speak to or about {n}: flag anything harsh, threatening, mocking, "
                     f"or ignoring the child when they need help, and mention {n} by name in the reason when it is about them.")
    return " ".join(parts)


SYSTEM = _intro() + """

You get numbered lines with their time offsets. Lines marked CONTEXT are for continuity only: do not return them. Do two things for the TARGET lines.

1. Correction. For every TARGET line, write your best reconstruction of what was most likely said, in the original language. Use the kindergarten setting, the neighbouring lines and Hebrew phonetics to fix misrecognized words. Keep the speaker's meaning and tone exactly: never soften or sharpen it, and never add content that is not supported by the recognized sounds. If a line cannot be reconstructed, return it unchanged with confidence "unintelligible". Confidence: "high" = nearly certain, "medium" = plausible, "low" = a guess.

2. Risk flags. Flag moments a concerned parent should listen to: physical harm, threats or intimidation, insults, humiliation or screaming at a child, secrecy ("don't tell your parents"), neglect or deprivation (food, toilet, leaving a child alone or excluded), and clear child distress (fear, calling for help, pain). Normal kindergarten firmness ("sit down", "wait your turn", "no running", "time to tidy up") is not a concern; do not flag it. Children's play shouting is usually not a concern. A false alarm costs the parent a minute of listening; a missed real incident costs much more - when a line is ambiguous but could be serious, flag it at low severity. Base severity on what was most likely said, and say in the reason when the flag depends on an uncertain reconstruction. Severity: 1 = worth a listen, 3 = concerning, 5 = urgent. Write reasons in English, one or two short sentences. quote = the relevant words in the original language. Also flag bad language: adults swearing or using crude/sexual language where children can hear, even when it is not aimed at a child (category "profanity", severity 1, or 2 when strong or repeated). Swearing or crude words aimed at a child are an insult (category "insult"), scored as such. Do not flag profanity that clearly comes from the radio or a song. Return an empty flags list when nothing is concerning.

3. Crying response. For every SOUND line of type crying, judge from the transcript whether an adult responded to a crying child during or right after it: "yes" (an adult clearly talks to, comforts or attends to a child), "no" (adults are heard but busy with something else, or nobody speaks for a long time), or "unclear". Return one entry per crying SOUND line, with sound_start copied exactly from the line, and a short English note."""

CATEGORY_ENUM = list(keywords.CATEGORIES) + ["other"]
SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "corrected": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low", "unintelligible"]},
                },
                "required": ["id", "corrected", "confidence"],
                "additionalProperties": False,
            },
        },
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_ids": {"type": "array", "items": {"type": "integer"}},
                    "category": {"type": "string", "enum": CATEGORY_ENUM},
                    "severity": {"type": "integer"},
                    "reason": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["line_ids", "category", "severity", "reason", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["lines", "flags", "crying"],
    "additionalProperties": False,
}
SCHEMA["properties"]["crying"] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "sound_start": {"type": "string"},
            "adult_response": {"type": "string", "enum": ["yes", "no", "unclear"]},
            "note": {"type": "string"},
        },
        "required": ["sound_start", "adult_response", "note"],
        "additionalProperties": False,
    },
}


def fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def load_env() -> None:
    env = config.ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def time_label(started_at: str | None):
    """Formatter for times in prompts: wall-clock when known, so the reviewer's reasons match what the
    parent sees; otherwise the offset into the file."""
    if not started_at:
        return globals()["fmt"]
    t0 = datetime.fromisoformat(started_at)
    return lambda s: (t0 + timedelta(seconds=s)).strftime("%H:%M:%S")


def build_prompt(rec_name: str, context: list, target: list, sounds: list | None = None,
                 excerpts: bool = False, started_at: str | None = None) -> str:
    fmt = time_label(started_at)

    def line(r, tag):
        return f"{tag} #{r['id']} [{fmt(r['start'])}] ({r['language']}) {r['text']}"
    rows = [(r["start"], line(r, "CONTEXT")) for r in context] + [(r["start"], line(r, "TARGET")) for r in target]
    # Sounds detected from the audio itself, interleaved by time, so the reviewer can judge e.g. long crying
    # with no adult responding. They are information only: flags must still reference line ids.
    for e in sounds or []:
        dur = e["end"] - e["start"]
        rows.append((e["start"], f"SOUND [{fmt(e['start'])}-{fmt(e['end'])}] {e['group']} detected for {dur:.0f} s"))
    body = "\n".join(t for _, t in sorted(rows, key=lambda x: x[0]))
    note = ("\nOnly excerpts are included: the minutes around sounds or warning words detected in this recording, "
            "so time may jump between lines. Lines marked SOUND are detected from the audio, not speech; use them as "
            "context (e.g. a child crying for minutes with no adult responding is a concern) and attach any flag "
            "to the nearest TARGET line ids.\n") if excerpts else ""
    return f"Recording: {rec_name}\n{note}\n{body}"


# ---------------------------------------------------------------- AI backends
# Both return (result_dict_or_None, tokens_in, tokens_out, cost_usd).

class UsageLimitHit(Exception):
    """The Claude plan's usage limit was reached - stop and resume later."""


class SubscriptionReviewer:
    """Runs each batch through `claude -p` (Claude Code CLI) on the logged-in Claude plan.

    Kept lean: our own short system prompt replaces Claude Code's, no tools, no MCP servers,
    no CLAUDE.md/plugins, no saved session - so each call carries little beyond the transcript.
    """
    name = "subscription"
    workers = 2

    def __init__(self):
        self.exe = shutil.which("claude")
        if not self.exe:
            sys.exit("Claude Code CLI ('claude') not found on PATH. Install Claude Code and log in, "
                     "or set KIDBERRY_AI_BACKEND=api.")
        fd, self.system_file = tempfile.mkstemp(prefix="kidberry_system_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(SYSTEM)
        self.env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # force subscription login

    def review(self, prompt: str):
        cmd = [self.exe, "-p", "--output-format", "json", "--system-prompt-file", self.system_file,
               "--tools", "", "--strict-mcp-config", "--safe-mode", "--no-session-persistence",
               "--model", config.AI_MODEL, "--effort", config.AI_EFFORT, "--json-schema", json.dumps(SCHEMA)]
        proc = subprocess.run(cmd, input=prompt, capture_output=True,
                              text=True, encoding="utf-8", env=self.env, timeout=900, cwd=tempfile.gettempdir())
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-500:]}")
        u = data.get("usage") or {}
        t_in = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
        t_out = u.get("output_tokens", 0)
        cost = data.get("total_cost_usd") or 0.0
        if data.get("is_error"):
            msg = str(data.get("result") or data.get("api_error_status") or "unknown error")
            if "limit" in msg.lower():
                raise UsageLimitHit(msg)
            raise RuntimeError(msg)
        if data.get("stop_reason") == "refusal":
            return None, t_in, t_out, cost
        result = data.get("structured_output")
        if result is None:
            try:
                result = json.loads(data.get("result") or "")
            except json.JSONDecodeError:
                result = None
        return result, t_in, t_out, cost


class APIReviewer:
    """Pay-as-you-go via ANTHROPIC_API_KEY."""
    name = "api"
    workers = 4

    def __init__(self):
        load_env()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit(f"ANTHROPIC_API_KEY is not set. Put it in {config.ROOT / '.env'} as\n"
                     "  ANTHROPIC_API_KEY=sk-ant-...\nor use the subscription backend.")
        import anthropic
        self.client = anthropic.Anthropic()

    def review(self, prompt: str):
        resp = self.client.beta.messages.create(
            model=config.AI_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            output_config={"effort": config.AI_EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        t_in, t_out = resp.usage.input_tokens, resp.usage.output_tokens
        cost = (t_in * PRICE_IN + t_out * PRICE_OUT) / 1e6
        if resp.stop_reason in ("refusal", "max_tokens"):
            return None, t_in, t_out, cost
        return json.loads(next(b.text for b in resp.content if b.type == "text")), t_in, t_out, cost


def make_reviewer():
    if config.AI_BACKEND == "api":
        return APIReviewer()
    if config.AI_BACKEND == "subscription":
        return SubscriptionReviewer()
    sys.exit(f"Unknown KIDBERRY_AI_BACKEND={config.AI_BACKEND!r} (use 'subscription' or 'api')")


# ---------------------------------------------------------------- usage tracking

def tokens_today(conn) -> int:
    row = conn.execute("SELECT COALESCE(SUM(tokens_in + tokens_out), 0) FROM ai_usage WHERE day=?",
                       (date.today().isoformat(),)).fetchone()
    return row[0]


def log_usage(conn, backend, rec_id, t_in, t_out, cost) -> None:
    with conn:
        conn.execute("INSERT INTO ai_usage (at, day, backend, recording_id, tokens_in, tokens_out, cost_usd)"
                     " VALUES (?,?,?,?,?,?,?)", (datetime.now().isoformat(timespec="seconds"),
                                                 date.today().isoformat(), backend, rec_id, t_in, t_out, cost))


def print_usage(conn) -> None:
    rows = conn.execute("SELECT day, backend, COUNT(*) n, SUM(tokens_in) i, SUM(tokens_out) o, SUM(cost_usd) c"
                        " FROM ai_usage GROUP BY day, backend ORDER BY day").fetchall()
    if not rows:
        print("No AI usage recorded yet.")
    for r in rows:
        print(f"{r['day']}  {r['backend']:<12} {r['n']:>3} batches  {r['i']:>9,} in  {r['o']:>8,} out  "
              f"= {r['i'] + r['o']:>9,} tokens   (API-price equivalent ${r['c'] or 0:.2f})")
    print(f"Daily cap: {config.AI_DAILY_TOKEN_CAP:,} tokens (KIDBERRY_AI_DAILY_TOKEN_CAP)")


# ---------------------------------------------------------------- kindergarten hours

def school_window(conn, rec) -> tuple[float, float] | None:
    """(from, to) offsets in seconds into the recording that fall between drop-off and pickup,
    or None when no hours are set for that day (then the whole recording is analyzed)."""
    if not rec["started_at"]:
        return None
    t0 = datetime.fromisoformat(rec["started_at"])
    row = conn.execute("SELECT dropoff, pickup FROM school_hours WHERE day=?", (t0.date().isoformat(),)).fetchone()
    if not row:
        return None
    def offset(hhmm):
        return (datetime.combine(t0.date(), datetime.strptime(hhmm, "%H:%M").time()) - t0).total_seconds()
    return offset(row["dropoff"]), offset(row["pickup"])


def segments_in_hours(conn, rec, cols: str) -> list:
    rows = conn.execute(f"SELECT {cols} FROM segments WHERE recording_id=? AND filtered IS NULL ORDER BY start",
                        (rec["id"],)).fetchall()
    win = school_window(conn, rec)
    return [r for r in rows if not win or win[0] <= r["start"] < win[1]]


def set_school_hours(conn, day: str, span: str) -> None:
    dropoff, pickup = span.split("-")
    for t in (dropoff, pickup):
        datetime.strptime(t, "%H:%M")
    date.fromisoformat(day)
    with conn:
        conn.execute("INSERT OR REPLACE INTO school_hours (day, dropoff, pickup) VALUES (?,?,?)",
                     (day, dropoff, pickup))
    # Flags outside the new window no longer apply.
    for rec in conn.execute("SELECT * FROM recordings WHERE substr(started_at,1,10)=?", (day,)).fetchall():
        win = school_window(conn, rec)
        with conn:
            conn.execute("DELETE FROM flags WHERE recording_id=? AND (end <= ? OR start >= ?)",
                         (rec["id"], win[0], win[1]))
    print(f"{day}: kindergarten hours {dropoff}-{pickup}")


# ---------------------------------------------------------------- AI review

class Stop(Exception):
    pass


def triggers(conn, rec) -> list:
    """Sound and keyword flags the parent hasn't dismissed: the moments smart mode reviews around."""
    return conn.execute("SELECT start, end FROM flags WHERE recording_id=? AND source IN ('sound','keyword')"
                        " AND COALESCE(review,'') != 'false_alarm'", (rec["id"],)).fetchall()


def select_segments(conn, rec, full: bool) -> tuple[list, list]:
    """(all lines in kindergarten hours, the lines to review). Smart mode: only lines within
    AI_SMART_PAD seconds of a trigger. Full mode: all of them."""
    segs = [dict(r) for r in segments_in_hours(conn, rec, "id, start, end, text, language, corrected")]
    if full:
        return segs, segs
    from .names import mention
    pad, npad = config.AI_SMART_PAD, config.AI_NAME_PAD
    trig = triggers(conn, rec)
    named = [s["start"] for s in segs if mention(s["text"], s["corrected"])]   # the child's name: a smaller window
    return segs, [s for s in segs if any(t["start"] - pad <= s["start"] <= t["end"] + pad for t in trig)
                  or any(n - npad <= s["start"] <= n + npad for n in named)]


def make_blocks(segs: list, selected: list) -> list[tuple[list, list]]:
    """Batches of up to AI_BLOCK_SEGMENTS selected lines, each with the lines just before it as context."""
    n, pos = config.AI_BLOCK_SEGMENTS, {s["id"]: i for i, s in enumerate(segs)}
    blocks = []
    for i in range(0, len(selected), n):
        tgt = selected[i: i + n]
        first = pos[tgt[0]["id"]]
        blocks.append((segs[max(0, first - config.AI_CONTEXT_SEGMENTS): first], tgt))
    return blocks


def ai_review_recording(conn, reviewer, rec, force: bool, budget: dict, full: bool = False) -> None:
    """Review a recording batch by batch. Each batch is saved as soon as it returns, so an interrupted
    run (cap, usage limit, Ctrl+C) resumes with the lines that have no corrections yet."""
    from .sounds import detect
    segs, selected = select_segments(conn, rec, full)
    if not segs:
        print("  no transcript lines within kindergarten hours")
        return
    by_id = {s["id"]: s for s in segs}
    todo_lines = [s for s in selected if force or s["corrected"] is None]
    todo = make_blocks(segs, todo_lines)
    events = detect(conn, rec)
    mode = "full day" if full else f"smart: +/-{config.AI_SMART_PAD // 60:.0f} min around {len(triggers(conn, rec))} triggers"
    print(f"  AI review ({mode}): {len(todo_lines)} of {len(segs)} lines, {len(todo)} batches")
    block_sounds = {}   # batch -> {time label shown to the AI: crying event start}
    started = rec["started_at"] if school_window(conn, rec) else None   # DB read here: workers run in threads

    def run(block):
        ctx, tgt = block
        pad = config.AI_SMART_PAD if not full else 30
        near = [e for e in events if tgt[0]["start"] - pad <= e["start"] <= tgt[-1]["end"] + pad
                and any(e["end"] > s["start"] - pad and e["start"] < s["end"] + pad for s in tgt)]
        label = time_label(started)
        block_sounds[id(tgt)] = {label(e["start"]): e["start"] for e in near if e["group"] == "crying"}
        try:
            prompt = build_prompt(rec["name"], ctx, tgt, sounds=near, excerpts=not full, started_at=started)
            return tgt, reviewer.review(prompt), None
        except UsageLimitHit as e:
            return tgt, None, e
        except Exception as e:  # noqa: BLE001 - report and continue with other batches
            return tgt, None, RuntimeError(str(e)[:300])

    done = 0
    stop_msg = None
    with ThreadPoolExecutor(max_workers=reviewer.workers) as pool:
        while todo and not stop_msg:
            # Respect the --blocks test limit and the daily token cap before starting each wave.
            room = (config.AI_DAILY_TOKEN_CAP - tokens_today(conn)) // EST_TOKENS_PER_BLOCK
            wave_size = min(reviewer.workers, len(todo), room, budget["blocks"])
            if wave_size <= 0:
                stop_msg = ("test batch limit reached" if budget["blocks"] <= 0 else
                            f"daily cap of {config.AI_DAILY_TOKEN_CAP:,} tokens reached - run again tomorrow "
                            "(or raise KIDBERRY_AI_DAILY_TOKEN_CAP)")
                break
            wave, todo = todo[:wave_size], todo[wave_size:]
            budget["blocks"] -= wave_size
            for tgt, out, err in pool.map(run, wave):
                label = f"lines #{tgt[0]['id']}-#{tgt[-1]['id']} ({fmt(tgt[0]['start'])}-{fmt(tgt[-1]['end'])})"
                if err:
                    print(f"  [error] {label}: {err}")
                    if isinstance(err, UsageLimitHit):
                        stop_msg = "Claude plan usage limit reached - run again later, it will resume"
                    continue
                result, t_in, t_out, cost = out
                log_usage(conn, reviewer.name, rec["id"], t_in, t_out, cost)
                done += 1
                if result is None:
                    print(f"  [warn] {label}: no usable answer (declined or cut off); skipped")
                    continue
                n_flags = save_block(conn, rec["id"], tgt, by_id, result)
                save_crying(conn, rec["id"], block_sounds.get(id(tgt), {}), result.get("crying", []))
                print(f"  batch {label}: {t_in:,} in + {t_out:,} out tokens, {n_flags} flags")
    if stop_msg:
        print(f"  stopped: {stop_msg}")
    from .sounds import sound_flags
    sound_flags(conn, rec)   # refresh crying flags with the AI's "did an adult respond?" answers
    _, selected = select_segments(conn, rec, full)
    remaining = sum(1 for s in selected if s["corrected"] is None)
    if not remaining:
        with conn:
            conn.execute("UPDATE recordings SET analyzed_at=? WHERE id=?",
                         (datetime.now().isoformat(timespec="seconds"), rec["id"]))
    print(f"  {done} batches done now; {remaining} selected lines still without AI review")
    if stop_msg:
        raise Stop(stop_msg)


def save_crying(conn, rec_id: int, labels: dict, answers: list) -> None:
    rows = [(rec_id, labels[a["sound_start"]], a["adult_response"], a["note"])
            for a in answers if a["sound_start"] in labels]
    with conn:
        conn.executemany("INSERT OR REPLACE INTO crying_responses (recording_id, event_start, response, note)"
                         " VALUES (?,?,?,?)", rows)


def save_block(conn, rec_id: int, tgt: list, by_id: dict, result: dict) -> int:
    tgt_ids = {s["id"] for s in tgt}
    corrections = [(ln["corrected"].strip(), ln["confidence"], ln["id"])
                   for ln in result.get("lines", []) if ln["id"] in tgt_ids]
    # Lines the model skipped still count as reviewed (kept as heard), so resume doesn't redo them.
    returned = {c[2] for c in corrections}
    corrections += [(by_id[i]["text"], "unintelligible", i) for i in tgt_ids - returned]
    flags = []
    for f in result.get("flags", []):
        ids = [i for i in f["line_ids"] if i in tgt_ids]
        if ids:
            flags.append((rec_id, min(by_id[i]["start"] for i in ids), max(by_id[i]["end"] for i in ids),
                          "ai", f["category"], max(1, min(5, f["severity"])), f["reason"], f["quote"],
                          json.dumps(ids)))
    with conn:
        conn.executemany("UPDATE segments SET corrected=?, corrected_confidence=? WHERE id=?", corrections)
    replace_flags(conn, rec_id, "ai", flags, only_lines=tgt_ids)
    return len(flags)


# ---------------------------------------------------------------- keywords

def keyword_flags(conn, rec_id: int) -> int:
    rec = conn.execute("SELECT * FROM recordings WHERE id=?", (rec_id,)).fetchone()
    rows = segments_in_hours(conn, rec, "id, start, end, text, corrected")
    flags = []
    for r in rows:
        hits = {h[0]: h for h in keywords.find(r["text"]) + keywords.find(r["corrected"] or "")}
        if not hits:
            continue
        top = max(hits.values(), key=lambda h: h[2])
        words = ", ".join(f'"{p}"' for p in hits)
        flags.append((rec_id, r["start"], r["end"], "keyword", top[1], top[2], f"Warning phrase: {words}",
                      r["corrected"] or r["text"], json.dumps([r["id"]])))
    replace_flags(conn, rec_id, "keyword", flags)
    return len(flags)


def replace_flags(conn, rec_id: int, source: str, flags: list[tuple], only_lines: set | None = None) -> None:
    """Swap in regenerated flags, keeping the parent's review verdicts for flags on the same lines.
    With only_lines, replace just the flags touching those lines (one AI batch)."""
    old = conn.execute("SELECT id, segment_ids, review FROM flags WHERE recording_id=? AND source=?",
                       (rec_id, source)).fetchall()
    if only_lines is not None:
        old = [r for r in old if set(json.loads(r["segment_ids"] or "[]")) & only_lines]
    reviews = {r["segment_ids"]: r["review"] for r in old if r["review"]}
    with conn:
        conn.executemany("DELETE FROM flags WHERE id=?", [(r["id"],) for r in old])
        conn.executemany(
            "INSERT INTO flags (recording_id, start, end, source, category, severity, reason, quote, segment_ids,"
            " review) VALUES (?,?,?,?,?,?,?,?,?,?)", [f + (reviews.get(f[-1]),) for f in flags])


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rec", type=int, help="only this recording id")
    ap.add_argument("--blocks", type=int, help="only run this many AI batches (for testing)")
    ap.add_argument("--force", action="store_true", help="redo the AI review even on reviewed lines")
    ap.add_argument("--keywords-only", action="store_true", help="skip the AI review")
    ap.add_argument("--usage", action="store_true", help="show AI token usage and exit")
    ap.add_argument("--hours", nargs=2, metavar=("DAY", "HH:MM-HH:MM"),
                    help="set drop-off/pickup for a day, e.g. --hours 2026-11-03 08:34-15:37, and exit")
    ap.add_argument("--estimate", action="store_true", help="show how many lines/tokens the AI review needs, and exit")
    ap.add_argument("--day", help="only recordings from this day (YYYY-MM-DD)")
    ap.add_argument("--full", action="store_true",
                    help="review every line in kindergarten hours, not just around sound/keyword triggers")
    args = ap.parse_args()
    full = args.full or config.AI_MODE == "full"

    conn = connect()
    if args.usage:
        print_usage(conn)
        return
    if args.hours:
        set_school_hours(conn, *args.hours)
        return
    q = "SELECT * FROM recordings WHERE status='done'"
    params = []
    if args.rec:
        q += " AND id=?"
        params.append(args.rec)
    if args.day:
        q += " AND substr(started_at,1,10)=?"
        params.append(args.day)
    recs = conn.execute(q + " ORDER BY started_at", params).fetchall()
    if not recs:
        sys.exit("No transcribed recordings found.")

    if args.estimate:
        total = 0
        print(f"Mode: {'full day' if full else f'smart (+/-{config.AI_SMART_PAD // 60} min around sound/keyword flags)'}")
        for rec in recs:
            keyword_flags(conn, rec["id"])
            segs, selected = select_segments(conn, rec, full)
            todo = [s for s in selected if args.force or s["corrected"] is None]
            batches = -(-len(todo) // config.AI_BLOCK_SEGMENTS)
            win = school_window(conn, rec)
            span = f"{fmt(max(0, win[0]))}-{fmt(win[1])} of the file" if win else "whole file (no hours set)"
            print(f"[{rec['id']}] {rec['name']}: {len(todo)} of {len(segs)} lines to review, {span}, ~{batches} batches")
            total += batches
        print(f"Estimated: ~{total * 7500:,} tokens (measured ~7,300 per batch). "
              f"Used today: {tokens_today(conn):,} of {config.AI_DAILY_TOKEN_CAP:,}")
        return

    reviewer = None if args.keywords_only else make_reviewer()
    if reviewer:
        print(f"AI backend: {reviewer.name}, model {config.AI_MODEL}; used today so far: "
              f"{tokens_today(conn):,} of {config.AI_DAILY_TOKEN_CAP:,} tokens")
    budget = {"blocks": args.blocks if args.blocks is not None else 10**9}
    before = tokens_today(conn)

    for rec in recs:
        print(f"[{rec['id']}] {rec['name']}")
        keyword_flags(conn, rec["id"])            # keyword hits are smart-mode triggers, so first
        if reviewer:
            try:
                ai_review_recording(conn, reviewer, rec, args.force, budget, full)
            except Stop:
                print(f"  {keyword_flags(conn, rec['id'])} keyword flags")
                break
        print(f"  {keyword_flags(conn, rec['id'])} keyword flags")   # again: corrections may reveal new words
    if reviewer:
        from .translate import translate_all   # AI corrections may have changed Spanish lines
        n = translate_all(conn)
        if n:
            print(f"Re-translated {n} corrected Spanish lines to English (local, free)")
        print(f"Tokens used this run: {tokens_today(conn) - before:,}; today: {tokens_today(conn):,} "
              f"of {config.AI_DAILY_TOKEN_CAP:,}")


if __name__ == "__main__":
    main()
