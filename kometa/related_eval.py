"""The measuring stick for Related (recommendations design, step 2a, 2026-10-16).

Runs an engine over a COPY of the database — never the live file, never the
network — and reports what the owner and the design partner agreed to judge it by:

- the seed series' top cards, each with its reasons and its primary reason;
- shelf-wide gates: coverage (series with 4+ cards), hub series (no one series in
  more than 3% of the top-5 rows), a primary reason crowding a row (no more than 3
  of a top 5), cards earned only by a shared inker or letterer, and speed;
- precision at 5 against the owner's marks, when there are marks.

    docker exec -i kometa python3 -m kometa.related_eval --out /tmp/related_eval.md

An engine is any callable (series_id, path) -> list of card dicts carrying
series_id, title, why (reasons, strongest first) and score. A card may also carry
`channels` ({channel: contribution}); the gates use it when it's there.
"""
import argparse
import json
import math
import os
import sqlite3
import statistics
import tempfile
import time
from collections import Counter

import kometa.db as db

# The seeds, by title (ids differ between databases). Agreed in rounds 1–3.
SEEDS = ["Monstress", "Saga", "Batman (1940)", "Absolute Batman", "Invincible", "Fantastic Four",
         "Aliens - Berserker", "The Department of Truth", "Something Is Killing the Children",
         "The Joker Presents: A Puzzlebox"]

COVERAGE_MIN_CARDS = 4
COVERAGE_TARGET = 700               # series with 4+ cards (586 of 834 on 2026-10-16)
HUB_MAX_SHARE = 0.03                # of all top-5 rows
REASON_MAX_IN_TOP5 = 3
LATENCY_P95_MS = 50.0
WEAK_ROLE_WEIGHT = 2.0              # a card whose best shared credit is below this (inker, colorist, letterer)


# --- engines -------------------------------------------------------------------------
def baseline_engine(series_id: int, path: str) -> list[dict]:
    """Today's related(), called the way the series page calls it."""
    from kometa import related
    return related.related(series_id, limit=12, path=path)


def combined_engine(series_id: int, path: str) -> list[dict]:
    """The phase-1 combiner (kometa/related_combined.py)."""
    from kometa import related_combined
    return related_combined.related_combined(series_id, limit=12, path=path)


def _combined_with(themes: bool, community: str):
    def engine(series_id: int, path: str) -> list[dict]:
        from kometa import related_combined
        return related_combined.related_combined(series_id, limit=12, path=path,
                                                 opts={"themes": themes, "community": community})
    engine.__doc__ = f"The combiner with themes {'on' if themes else 'off'}, community {community}."
    return engine


# "combined" is whatever config has switched on; these pin the phase-2 switches
ENGINES = {"baseline": baseline_engine, "combined": combined_engine,
           "combined-p1": _combined_with(False, "off"),
           "combined+themes": _combined_with(True, "off"),
           "combined+boost": _combined_with(False, "boost"),
           "combined+cards": _combined_with(False, "cards"),
           "combined+themes+boost": _combined_with(True, "boost"),
           "combined+themes+cards": _combined_with(True, "cards")}


def _engine(name: str):
    if name in ENGINES:
        return ENGINES[name]
    from kometa import related
    fn = getattr(related, name, None)              # a candidate engine living in related.py
    if fn is None:
        raise SystemExit(f"unknown engine {name!r}: one of {sorted(ENGINES)} or a related.py function")
    return lambda sid, path: fn(sid, limit=12, path=path)


# --- reasons ---------------------------------------------------------------------------
def reason_kind(reason: str) -> str:
    """The family a 'why' string belongs to, so a row's primary reasons can be counted."""
    r = (reason or "").strip()
    low = r.lower()
    for prefix, kind in (("arc:", "arc"), ("franchise:", "franchise"), ("on a reading list", "list"),
                         ("reading list", "list"), ("universe", "universe"), ("team:", "cast"), ("stars ", "cast"),
                         ("genre:", "genre"), ("theme:", "genre"), ("companion", "associated"), ("liked by", "community")):
        if low.startswith(prefix):
            return kind
    return "creator"                                  # today's engine writes a bare creator name


def primary_reason(card: dict) -> str:
    why = card.get("why") or []
    return why[0] if why else ""


def _creator_strength(path: str) -> callable:
    """For two series, the strongest shared credit's weight (min of the two roles,
    as related() scores it) — so a card can be checked for 'only an inker'."""
    from kometa import related
    from kometa.naming import norm_key
    sig = related._signals(path)

    def weights(sid):
        out = {}
        for c in sig.get(sid, {}).get("creators", []):
            w = related._role_weight(c.get("role"))
            if w > 0:
                k = norm_key(c["name"])
                out[k] = max(out.get(k, 0.0), w)
        return out

    cache = {}

    def strength(a: int, b: int) -> float:
        wa = cache.setdefault(a, weights(a))
        wb = cache.setdefault(b, weights(b))
        return max((min(wa[k], wb[k]) for k in wa.keys() & wb.keys()), default=0.0)
    return strength


def weak_creator_only(seed: int, card: dict, strength) -> bool:
    """A card earned only by a shared inker, colorist or letterer: its only reasons
    are credits, and the strongest credit the two series share is weak. (No shared
    credit at all means the reason wasn't a credit — that isn't this gate's call.)"""
    s = card["credit_strength"] if "credit_strength" in card else strength(seed, card["series_id"])
    if not 0 < s < WEAK_ROLE_WEIGHT:
        return False
    ch = card.get("channels")
    if ch:
        return not {k for k, v in ch.items() if k != "creators" and v > 0}
    kinds = {reason_kind(r) for r in card.get("why") or []}
    return kinds == {"creator"}


# --- the run -----------------------------------------------------------------------------
def snapshot(src: str) -> str:
    """A private copy of the database: the harness never touches the live file."""
    fd, dst = tempfile.mkstemp(prefix="related-eval-", suffix=".db")
    os.close(fd)
    s, d = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    return dst


def discard(copy: str) -> None:
    """Remove a snapshot and the journal files SQLite opened beside it."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(copy + suffix)
        except FileNotFoundError:
            pass


def _series_ids(path: str) -> list[int]:
    with db._connect(path) as conn:
        return [r[0] for r in conn.execute("SELECT id FROM tracked_series WHERE kind IS NULL OR kind != 'arc' ORDER BY id")]


def _resolve_seeds(path: str, titles: list[str]) -> list[tuple[str, int | None]]:
    out = []
    with db._connect(path) as conn:
        for t in titles:
            r = conn.execute("SELECT id FROM tracked_series WHERE title = ? AND (kind IS NULL OR kind != 'arc') "
                             "ORDER BY id LIMIT 1", (t,)).fetchone()
            out.append((t, r[0] if r else None))
    return out


def evaluate(path: str, engine, seeds: list[str] | None = None, marks: dict | None = None) -> dict:
    """Run one engine over every series in `path`. Returns the numbers the report prints."""
    ids = _series_ids(path)
    titles = {}
    with db._connect(path) as conn:
        titles = {r[0]: r[1] for r in conn.execute("SELECT id, title FROM tracked_series")}
    strength = _creator_strength(path)
    rows, lat = {}, []
    engine(ids[0], path) if ids else None             # warm caches once, as a running server is
    for sid in ids:
        t0 = time.perf_counter()
        rows[sid] = engine(sid, path) or []
        lat.append((time.perf_counter() - t0) * 1000)
    hub = Counter(c["series_id"] for cards in rows.values() for c in cards[:5])
    crowded, weak = [], []
    for sid, cards in rows.items():
        top = Counter(primary_reason(c) for c in cards[:5] if primary_reason(c))
        if top and top.most_common(1)[0][1] > REASON_MAX_IN_TOP5:
            crowded.append((sid, top.most_common(1)[0]))
        for c in cards:
            if weak_creator_only(sid, c, strength):
                weak.append((sid, c["series_id"]))
    lat.sort()
    p95 = lat[min(len(lat) - 1, int(math.ceil(len(lat) * 0.95)) - 1)] if lat else 0.0
    covered = sum(1 for cards in rows.values() if len(cards) >= COVERAGE_MIN_CARDS)
    max_hub = hub.most_common(1)[0] if hub else (None, 0)
    seed_rows = []
    for title, sid in _resolve_seeds(path, seeds or SEEDS):
        cards = rows.get(sid, []) if sid else []
        seed_rows.append({"title": title, "series_id": sid, "cards": [
            {"series_id": c["series_id"], "title": c.get("title"), "score": c.get("score"),
             "why": c.get("why") or [], "primary": reason_kind(primary_reason(c)),
             "weak": weak_creator_only(sid, c, strength), "channels": c.get("channels")} for c in cards[:8]]})
    prec = _precision(seed_rows, marks or {})
    return {
        "series": len(ids),
        "covered": covered, "coverage_target": COVERAGE_TARGET,
        "empty": sum(1 for cards in rows.values() if not cards),
        "hub_top": [(titles.get(s, s), n) for s, n in hub.most_common(5)],
        "hub_max_share": (max_hub[1] / len(ids)) if ids else 0.0,
        "crowded": [(titles.get(s, s), reason, n) for s, (reason, n) in crowded],
        "weak_cards": [(titles.get(a, a), titles.get(b, b)) for a, b in weak],
        "latency_p50_ms": statistics.median(lat) if lat else 0.0, "latency_p95_ms": p95,
        "seeds": seed_rows, "precision_at_5": prec,
    }


def _precision(seed_rows: list[dict], marks: dict) -> dict | None:
    """Share of each seed's top 5 the owner marked good or ok. Unmarked cards are counted apart."""
    if not marks:
        return None
    good = total = unmarked = 0
    for s in seed_rows:
        m = marks.get(str(s["series_id"])) or marks.get(s["title"]) or {}
        for c in s["cards"][:5]:
            v = m.get(str(c["series_id"])) or m.get(c["title"] or "")
            if v is None:
                unmarked += 1
                continue
            total += 1
            good += v in ("good", "ok")
    return {"precision": good / total if total else None, "marked": total, "unmarked": unmarked}


def gates(r: dict) -> list[tuple[str, str, bool]]:
    """(gate, value, passed) — the round-3 thresholds."""
    return [
        (f"Series with {COVERAGE_MIN_CARDS}+ cards", f"{r['covered']} of {r['series']} (target {r['coverage_target']})",
         r["covered"] >= r["coverage_target"]),
        ("Largest hub (share of top-5 rows)", f"{r['hub_top'][0][0] if r['hub_top'] else '-'}: "
         f"{r['hub_max_share']:.1%} (max {HUB_MAX_SHARE:.0%})", r["hub_max_share"] <= HUB_MAX_SHARE),
        (f"Rows where one reason fills {REASON_MAX_IN_TOP5 + 1}+ of the top 5", str(len(r["crowded"])), not r["crowded"]),
        ("Cards earned only by an inker, colorist or letterer", str(len(r["weak_cards"])), not r["weak_cards"]),
        ("Speed, 95th percentile", f"{r['latency_p95_ms']:.1f} ms (max {LATENCY_P95_MS:.0f})",
         r["latency_p95_ms"] <= LATENCY_P95_MS),
    ]


def report(results: dict[str, dict]) -> str:
    """Markdown: one gates table per engine, then the seeds side by side."""
    out = ["# Related — evaluation", ""]
    for name, r in results.items():
        out += [f"## Engine: {name}", "", "| Gate | Value | Pass |", "|---|---|---|"]
        out += [f"| {g} | {v} | {'yes' if ok else 'NO'} |" for g, v, ok in gates(r)]
        out += ["", f"Series with no cards: {r['empty']}. Busiest hubs: "
                + ", ".join(f"{t} ({n})" for t, n in r["hub_top"]) + "."]
        if r["precision_at_5"]:
            p = r["precision_at_5"]
            out += [f"Precision at 5 against the owner's marks: "
                    f"{(p['precision'] or 0):.0%} of {p['marked']} marked cards ({p['unmarked']} unmarked)."]
        if r["weak_cards"]:
            out += ["", "Weak-credit cards: " + "; ".join(f"{a} → {b}" for a, b in r["weak_cards"][:15])
                    + (" …" if len(r["weak_cards"]) > 15 else "")]
        if r["crowded"]:
            out += ["", "Crowded rows: " + "; ".join(f"{t} ({reason} ×{n})" for t, reason, n in r["crowded"][:15])
                    + (" …" if len(r["crowded"]) > 15 else "")]
        out.append("")
    first = next(iter(results.values()))
    for i, seed in enumerate(first["seeds"]):
        out += [f"### {seed['title']}" + ("" if seed["series_id"] else " (not on this shelf)"), ""]
        for name, r in results.items():
            cards = r["seeds"][i]["cards"]
            out.append(f"**{name}:** " + ("none" if not cards else ""))
            for n, c in enumerate(cards, 1):
                flag = " ⚠ weak credit only" if c["weak"] else ""
                out.append(f"{n}. {c['title']} — {', '.join(c['why'][:3]) or '(no reason)'} "
                           f"[{c['primary']}, {c['score']}]{flag}")
            out.append("")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Evaluate Related engines on a copy of the database.")
    ap.add_argument("--db", default=db.DB_PATH, help="database to copy (default: the app's)")
    ap.add_argument("--engine", action="append", help="baseline, or a related.py function name; repeatable")
    ap.add_argument("--marks", help="JSON {seed: {card: good|ok|bad}} by id or title")
    ap.add_argument("--out", help="write the markdown report here too")
    ap.add_argument("--json", help="write the raw numbers here")
    a = ap.parse_args(argv)
    marks = json.load(open(a.marks)) if a.marks else None
    copy = snapshot(a.db)
    try:
        results = {name: evaluate(copy, _engine(name), marks=marks) for name in (a.engine or ["baseline"])}
    finally:
        discard(copy)
    text = report(results)
    print(text)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(results, fh, indent=1, default=str)
    return results


if __name__ == "__main__":
    main()
