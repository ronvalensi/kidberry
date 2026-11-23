"""Per-day statistics and patterns across days.

Usage:
    python -m kidberry.days          # print the days table and current patterns

A single crying episode means little in a toddler group; the same thing repeating does. Patterns look at
the last PATTERN_DAYS recorded kindergarten days (days with drop-off/pickup hours set). Flags the parent
marked "False alarm" never count.
"""
from collections import defaultdict
from datetime import datetime, timedelta

from .db import connect

PATTERN_DAYS = 5          # look-back: the last N recorded kindergarten days
MIN_DAYS = 3              # ...a pattern needs this many of them
TIME_WINDOW = 30          # minutes either side: "at a similar time of day"
UNATTENDED_MIN_DAYS = 2   # crying with no adult response on this many days is a pattern
SERIOUS = {"physical", "threat", "insult", "secrecy", "neglect"}

LABELS = {"physical": "Physical harm", "threat": "Threats / intimidation", "insult": "Insults / humiliation",
          "secrecy": "Secrecy", "neglect": "Neglect / deprivation", "distress": "Child distress",
          "profanity": "Bad language near children", "crying": "Long crying", "screaming": "Screaming",
          "yelling": "Yelling", "impact": "Slap / impact sounds", "other": "Other"}


def _clock(started_at: str, offset: float) -> datetime:
    return datetime.fromisoformat(started_at) + timedelta(seconds=offset)


def day_stats(conn) -> list[dict]:
    from .analyze import school_window
    from .sounds import crying_episodes, detect

    days = defaultdict(lambda: {"recordings": [], "flags": [], "sounds": []})
    hours = {r["day"]: (r["dropoff"], r["pickup"]) for r in conn.execute("SELECT * FROM school_hours")}
    for rec in conn.execute("SELECT * FROM recordings WHERE status='done' AND started_at IS NOT NULL"
                            " ORDER BY started_at").fetchall():
        day = rec["started_at"][:10]
        d = days[day]
        d["recordings"].append({"id": rec["id"], "name": rec["name"], "started_at": rec["started_at"],
                                "duration": rec["duration"]})
        win = school_window(conn, rec)
        inside = lambda a, b: not win or (b > win[0] and a < win[1])   # noqa: E731
        events = [e for e in detect(conn, rec) if inside(e["start"], e["end"])]
        for e in events:
            t = _clock(rec["started_at"], e["start"])
            d["sounds"].append({"group": e["group"], "rec": rec["id"], "start": e["start"],
                                "minute": t.hour * 60 + t.minute + t.second / 60,
                                "duration": e["end"] - e["start"]})
        eps = crying_episodes(events)
        d["longest_crying"] = max([d.get("longest_crying", 0)] + [e["crying"] for e in eps])
        for f in conn.execute("SELECT * FROM flags WHERE recording_id=? AND COALESCE(review,'') != 'false_alarm'"
                              " ORDER BY start", (rec["id"],)):
            t = _clock(rec["started_at"], f["start"])
            d["flags"].append({"id": f["id"], "rec": rec["id"], "start": f["start"], "end": f["end"],
                               "minute": t.hour * 60 + t.minute + t.second / 60, "clock": t.strftime("%H:%M"),
                               "source": f["source"], "category": f["category"], "severity": f["severity"],
                               "reason": f["reason"], "quote": f["quote"], "review": f["review"],
                               "adult_response": f["adult_response"]})

    out = []
    for day in sorted(days):
        d = days[day]
        flags = d["flags"]
        cats = defaultdict(int)
        for f in flags:
            cats[f["category"]] += 1
        crying_sec = sum(s["duration"] for s in d["sounds"] if s["group"] == "crying")
        out.append({
            "day": day, "hours": hours.get(day), "recordings": d["recordings"], "flags": flags, "sounds": d["sounds"],
            "flag_count": len(flags), "max_severity": max((f["severity"] for f in flags), default=0),
            "by_category": dict(cats),
            "crying_minutes": round(crying_sec / 60, 1),
            "longest_crying_minutes": round(d.get("longest_crying", 0) / 60, 1),
            "unattended_crying": sum(1 for f in flags if f["category"] == "crying" and f["adult_response"] == "no"),
            "profanity": cats.get("profanity", 0),
            "concerning": sum(1 for f in flags if f["review"] == "concerning"),
        })
    return out


def _fmt_min(m: float) -> str:
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"


def patterns(days: list[dict]) -> dict:
    """Patterns over the last PATTERN_DAYS kindergarten days (days with hours set)."""
    school = [d for d in days if d["hours"]][-PATTERN_DAYS:]
    found = []

    # 1. The same kind of flag at a similar time of day on several days.
    by_cat = defaultdict(list)
    for d in school:
        for f in d["flags"]:
            by_cat[f["category"]].append((d["day"], f))
    for cat, items in by_cat.items():
        best = None
        for _, anchor in items:
            hits = [(day, f) for day, f in items if abs(f["minute"] - anchor["minute"]) <= TIME_WINDOW]
            n_days = len({day for day, _ in hits})
            if n_days >= MIN_DAYS and (not best or n_days > best[0]):
                best = (n_days, hits)
        if best:
            n_days, hits = best
            lo, hi = min(f["minute"] for _, f in hits), max(f["minute"] for _, f in hits)
            found.append({"kind": "same_time", "category": cat, "days": sorted({d for d, _ in hits}),
                          "severity": max(f["severity"] for _, f in hits),
                          "title": f"{LABELS.get(cat, cat)} around {_fmt_min(lo)}-{_fmt_min(hi)}",
                          "detail": f"on {n_days} of the last {len(school)} kindergarten days",
                          "flags": [f["id"] for _, f in hits]})

    # 2. Serious categories / bad language on several days, at any time.
    for cat, items in by_cat.items():
        n_days = len({day for day, _ in items})
        if (cat in SERIOUS or cat == "profanity") and n_days >= MIN_DAYS \
                and not any(p["category"] == cat and p["kind"] == "same_time" for p in found):
            found.append({"kind": "repeated", "category": cat, "days": sorted({d for d, _ in items}),
                          "severity": max(f["severity"] for _, f in items),
                          "title": f"{LABELS.get(cat, cat)} on {n_days} days",
                          "detail": f"on {n_days} of the last {len(school)} kindergarten days",
                          "flags": [f["id"] for _, f in items]})

    # 3. Crying with no adult response, on several days.
    unatt = [(d["day"], f) for d in school for f in d["flags"]
             if f["category"] == "crying" and f["adult_response"] == "no"]
    n_days = len({day for day, _ in unatt})
    if n_days >= UNATTENDED_MIN_DAYS:
        found.append({"kind": "unattended", "category": "crying", "days": sorted({d for d, _ in unatt}),
                      "severity": 3, "title": "Long crying with no adult heard responding",
                      "detail": f"{len(unatt)} times on {n_days} of the last {len(school)} kindergarten days",
                      "flags": [f["id"] for _, f in unatt]})

    # 4. Something the parent marked "Concerning" happening again on a later day.
    concerning = [(d["day"], f) for d in days for f in d["flags"] if f["review"] == "concerning"]
    for cday, cf in concerning:
        later = [(d["day"], f) for d in days if d["day"] > cday for f in d["flags"]
                 if f["category"] == cf["category"] and f["review"] != "concerning"]
        if later:
            found.append({"kind": "repeat_of_concerning", "category": cf["category"],
                          "days": sorted({cday} | {d for d, _ in later}), "severity": max(cf["severity"], 3),
                          "title": f"{LABELS.get(cf['category'], cf['category'])} again after you marked it Concerning",
                          "detail": f"you marked {cday} {cf['clock']}; it came up again on "
                                    f"{', '.join(sorted({d for d, _ in later}))}",
                          "flags": [cf["id"]] + [f["id"] for _, f in later]})

    found.sort(key=lambda p: -p["severity"])
    return {"patterns": found, "school_days": [d["day"] for d in school],
            "needed": MIN_DAYS, "look_back": PATTERN_DAYS}


def main() -> None:
    conn = connect()
    days = day_stats(conn)
    print(f"{'day':<11} {'hours':<12} {'flags':>5} {'max':>3} {'cry min':>7} {'longest':>7} {'no-resp':>7} "
          f"{'bad lang':>8} {'concern':>7}")
    for d in days:
        h = "-".join(d["hours"]) if d["hours"] else "(not set)"
        print(f"{d['day']:<11} {h:<12} {d['flag_count']:>5} {d['max_severity']:>3} {d['crying_minutes']:>7} "
              f"{d['longest_crying_minutes']:>7} {d['unattended_crying']:>7} {d['profanity']:>8} {d['concerning']:>7}")
    p = patterns(days)
    print()
    if not p["patterns"]:
        print(f"No patterns yet ({len(p['school_days'])} kindergarten day(s) with hours set; "
              f"a pattern needs {p['needed']} of the last {p['look_back']}).")
    for x in p["patterns"]:
        print(f"PATTERN [{x['severity']}] {x['title']} - {x['detail']}  ({', '.join(x['days'])})")


if __name__ == "__main__":
    main()
