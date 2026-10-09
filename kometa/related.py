"""What's near this? One engine, two rows.

- Related (series page): series near ONE series.
- Suggestions (On Deck): series near what you've been reading lately, summed.

Three signals, each labelled on the card so you know why it's there:
  creators  — writer / artist credits (Metron issue credits, one representative
              issue per series, cached as the series' 'signals')
  arcs      — a shared story arc or event name
  lists     — beside it on an imported reading list (a curated 'related')

Signals fill lazily: the first time a series page asks, and a background
trickle for the rest (one Metron call per series, 30-day refresh). Scoring is
in-memory over the cached signals, so the rows are cheap to render.
"""
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

import kometa.db as db
from kometa.naming import norm_key

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
READER_ID = "me"
SIGNAL_TTL_DAYS = 30
RECENT_DAYS = 60
_ROLE_WEIGHT = {"writer": 3.0, "story": 3.0, "script": 3.0, "plot": 2.0, "artist": 2.0, "penciller": 2.0,
                "inker": 1.0, "colorist": 0.5, "letterer": 0.25, "cover": 0.0}


def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS series_signals (
                tracked_series_id INTEGER PRIMARY KEY,
                creators_json     TEXT,
                arcs_json         TEXT,
                fetched_at        TEXT DEFAULT (datetime('now'))
            )""")


# --- signals ----------------------------------------------------------------------
def _representative_issue(series_id: int, path) -> dict | None:
    issues = [i for i in db.get_issues_for_series(series_id, path) if i.get("metron_issue_id")]
    if not issues:
        return None
    owned = [i for i in issues if i.get("owned")]
    return min(owned or issues, key=lambda i: i["number"])


def fill_signals(series_id: int, path=None, detail=None) -> bool:
    """Fetch and store one series' creators + arcs. False when there's nothing to
    ask (no Metron issue) — stored as empty so it isn't asked again this month."""
    path = path or DB_PATH
    ensure_tables(path)
    issue = _representative_issue(series_id, path)
    creators, arcs = [], []
    if issue:
        from kometa import metron_client
        d = (detail or metron_client.issue_detail)(issue["metron_issue_id"])
        for c in d.get("credits") or []:
            if c.get("name"):
                creators.append({"role": (c.get("role") or "").lower(), "name": c["name"]})
        arcs = [a for a in (d.get("arcs") or []) if a]
    with db._connect(path) as conn:
        conn.execute("INSERT OR REPLACE INTO series_signals (tracked_series_id, creators_json, arcs_json, fetched_at) "
                     "VALUES (?, ?, ?, datetime('now'))", (series_id, json.dumps(creators), json.dumps(arcs)))
    return bool(issue)


def _signals(path) -> dict[int, dict]:
    ensure_tables(path)
    with db._connect(path) as conn:
        return {r["tracked_series_id"]: {"creators": json.loads(r["creators_json"] or "[]"),
                                         "arcs": json.loads(r["arcs_json"] or "[]"),
                                         "fetched_at": r["fetched_at"]}
                for r in conn.execute("SELECT * FROM series_signals")}


def _stale(fetched_at: str | None) -> bool:
    if not fetched_at:
        return True
    try:
        return datetime.strptime(fetched_at, "%Y-%m-%d %H:%M:%S") < datetime.utcnow() - timedelta(days=SIGNAL_TTL_DAYS)
    except ValueError:
        return True


def trickle_signals(limit: int = 20, path=None) -> int:
    """Background: fill signals for series that have none (or stale ones).
    Stops at the first Metron refusal — the budget is shared with everything."""
    path = path or DB_PATH
    have = _signals(path)
    todo = [s for s in db.get_all_series(path) if s.get("kind") != "arc" and s.get("metron_series_id")
            and (s["id"] not in have or _stale(have[s["id"]]["fetched_at"]))]
    n = 0
    from kometa import metron_client
    for s in todo[:limit]:
        try:
            fill_signals(s["id"], path)
            n += 1
        except metron_client.MetronUnavailable as e:
            logger.info(f"Related signals paused: {e}")
            break
        except Exception as e:
            logger.info(f"Related signals skipped {s['title']!r}: {e}")
    if n:
        logger.info(f"Related: signals filled for {n} series")
    return n


# --- scoring ----------------------------------------------------------------------
def _list_neighbours(path) -> dict[int, dict[int, float]]:
    """series_id → {other_series_id: weight} from every imported reading list."""
    from kometa import readlists
    out: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    try:
        lists = readlists.get_lists(path)
    except Exception:
        return out
    for l in lists:
        try:
            res = readlists.resolve(l["id"], path)
        except Exception:
            continue
        seq = [(e["position"], e["series_id"]) for e in res["entries"] if e.get("series_id")]
        for i, (pa, a) in enumerate(seq):
            for pb, b in seq:
                if a == b:
                    continue
                out[a][b] = max(out[a][b], 2.0 + (1.0 if abs(pa - pb) <= 2 else 0.0))
    return out


def _creator_index(sig: dict[int, dict]) -> dict[str, list[tuple[int, float]]]:
    idx: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for sid, s in sig.items():
        seen = {}
        for c in s["creators"]:
            w = _ROLE_WEIGHT.get(c["role"], 1.0)
            if w <= 0:
                continue
            k = norm_key(c["name"])
            seen[k] = max(seen.get(k, 0), w)
        for k, w in seen.items():
            idx[k].append((sid, w))
    return idx


def related(series_id: int, limit: int = 12, path=None, signals=None, neighbours=None) -> list[dict]:
    path = path or DB_PATH
    sig = signals if signals is not None else _signals(path)
    me = sig.get(series_id, {"creators": [], "arcs": []})
    scores: dict[int, float] = defaultdict(float)
    why: dict[int, list[str]] = defaultdict(list)
    cidx = _creator_index(sig)
    mine = {}
    for c in me["creators"]:
        w = _ROLE_WEIGHT.get(c["role"], 1.0)
        if w > 0:
            mine[norm_key(c["name"])] = (max(mine.get(norm_key(c["name"]), (0, ""))[0], w), c["name"])
    for k, (w, name) in mine.items():
        for sid, w2 in cidx.get(k, []):
            if sid == series_id:
                continue
            scores[sid] += min(w, w2)
            why[sid].append(name)
    my_arcs = {norm_key(a): a for a in me["arcs"]}
    for sid, s in sig.items():
        if sid == series_id:
            continue
        for a in s["arcs"]:
            if norm_key(a) in my_arcs:
                scores[sid] += 3.0
                why[sid].append(f"arc: {a}")
    nb = neighbours if neighbours is not None else _list_neighbours(path)
    for sid, w in nb.get(series_id, {}).items():
        scores[sid] += w
        why[sid].append("on a reading list together")
    series = {s["id"]: s for s in db.get_all_series(path)}
    out = []
    for sid, sc in sorted(scores.items(), key=lambda kv: -kv[1])[:limit]:
        s = series.get(sid)
        if not s or s.get("kind") == "arc":
            continue
        seen, reasons = set(), []
        for r in why[sid]:
            if r not in seen:
                seen.add(r); reasons.append(r)
        out.append({"series_id": sid, "title": s["title"], "publisher": s.get("publisher"), "score": round(sc, 2),
                    "why": reasons[:3], "owned": s.get("owned") or 0, "total": (s.get("owned") or 0) + (s.get("missing") or 0)})
    return out


def _recent_series(path, days: int = RECENT_DAYS) -> list[int]:
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db._connect(path) as conn:
        return [r[0] for r in conn.execute("""
            SELECT b.tracked_series_id FROM read_progress p JOIN books b ON b.id = p.book_id
            WHERE p.reader_id = ? AND p.updated_at >= ? AND b.tracked_series_id IS NOT NULL
            GROUP BY b.tracked_series_id ORDER BY MAX(p.updated_at) DESC LIMIT 20""", (READER_ID, since))]


def suggestions(limit: int = 16, path=None) -> list[dict]:
    """Near what you've been reading lately, summed; series you already own but
    haven't started rank first (they're one tap from reading)."""
    path = path or DB_PATH
    seeds = _recent_series(path)
    if not seeds:
        return []
    sig = _signals(path)
    nb = _list_neighbours(path)
    agg: dict[int, dict] = {}
    for seed in seeds:
        for r in related(seed, limit=30, path=path, signals=sig, neighbours=nb):
            if r["series_id"] in seeds:
                continue
            a = agg.setdefault(r["series_id"], {**r, "score": 0.0, "why": [], "because": []})
            a["score"] += r["score"]
            for w in r["why"]:
                if w not in a["why"]:
                    a["why"].append(w)
            a["because"].append(seed)
    with db._connect(path) as conn:
        started = {r[0] for r in conn.execute(
            "SELECT DISTINCT b.tracked_series_id FROM read_progress p JOIN books b ON b.id = p.book_id WHERE p.reader_id = ?", (READER_ID,))}
    titles = {s["id"]: s["title"] for s in db.get_all_series(path)}
    out = sorted(agg.values(), key=lambda r: (r["series_id"] in started, -(r["owned"] > 0), -r["score"]))
    for r in out:
        r["because"] = [titles.get(x, "") for x in r["because"][:2]]
        r["why"] = r["why"][:3]
        r["started"] = r["series_id"] in started
    return out[:limit]


# --- API --------------------------------------------------------------------------
@router.get("/api/series/{series_id}/related")
def api_related(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    sig = _signals(DB_PATH)
    pending = False
    if series_id not in sig or _stale(sig[series_id]["fetched_at"]):
        try:
            fill_signals(series_id, DB_PATH)
        except Exception as e:
            logger.info(f"Related: signals for {s['title']!r} not available yet: {e}")
            pending = True
    return {"related": related(series_id), "pending": pending}


@router.get("/api/ondeck/suggestions")
def api_suggestions():
    return {"suggestions": suggestions()}
