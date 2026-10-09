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
import threading
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
    creators, arcs = [], []
    # The local record first (kometa/record.py): a Metron row with creator ids,
    # else an LOCG row with names — enough for shelf-side Related on runs Metron
    # hasn't got. The catalogue is asked only when the record holds nothing.
    from kometa import record
    rows = [r for r in record.issues_for_series(series_id, path) if r.get("credits")]
    rows.sort(key=lambda r: (r.get("source") != "metron", r["number"]))
    issue = None if rows else _representative_issue(series_id, path)
    if not rows and not issue:
        # an LOCG-only run: its credits come when a pass is live — queue it
        try:
            s_ = db.get_series_by_id(series_id, path)
            if s_ and s_.get("locg_series_id"):
                from kometa import topup
                topup.enqueue("credits", str(series_id), path)
        except Exception as e:
            logger.info(f"Signals: top-up enqueue skipped for {series_id}: {e}")
    if rows:
        d = rows[0]
        for c in d.get("credits") or []:
            if c.get("name"):
                creators.append({"role": (c.get("role") or "").lower(), "name": c["name"], "id": c.get("metron_creator_id")})
        arcs = sorted({a for r in rows for a in (r.get("arcs") or []) if a})
    elif issue:
        from kometa import metron_client
        d = (detail or metron_client.issue_detail)(issue["metron_issue_id"])
        for c in d.get("credits") or []:
            if c.get("name"):
                creators.append({"role": (c.get("role") or "").lower(), "name": c["name"], "id": c.get("metron_creator_id")})
        arcs = [a for a in (d.get("arcs") or []) if a]
    with db._connect(path) as conn:
        conn.execute("INSERT OR REPLACE INTO series_signals (tracked_series_id, creators_json, arcs_json, fetched_at) "
                     "VALUES (?, ?, ?, datetime('now'))", (series_id, json.dumps(creators), json.dumps(arcs)))
    return bool(issue) or bool(rows)


def _signals(path) -> dict[int, dict]:
    ensure_tables(path)
    with db._connect(path) as conn:
        return {r["tracked_series_id"]: {"creators": json.loads(r["creators_json"] or "[]"),
                                         "arcs": json.loads(r["arcs_json"] or "[]"),
                                         "fetched_at": r["fetched_at"]}
                for r in conn.execute("SELECT * FROM series_signals")}


def _needs_ids(sig: dict) -> bool:
    """Signals written before creator ids were kept can't look outward."""
    return any(c.get("id") is None for c in sig.get("creators", []) if _ROLE_WEIGHT.get(c.get("role"), 1.0) >= 2.0)


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
            and (s["id"] not in have or _stale(have[s["id"]]["fetched_at"]) or _needs_ids(have[s["id"]]))]
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
_nb_cache: dict = {"at": 0.0, "path": None, "value": None}
NEIGHBOURS_TTL_S = 300


def _list_neighbours(path) -> dict[int, dict[int, float]]:
    """series_id → {other_series_id: weight} from every imported reading list.
    Resolving every list is the expensive part of this module (19 lists, each a
    scan of the books table), so it's kept for five minutes — a Related call
    during a page's cover flood took six seconds without this."""
    now = time.time()
    if _nb_cache["value"] is not None and _nb_cache["path"] == path and now - _nb_cache["at"] < NEIGHBOURS_TTL_S:
        return _nb_cache["value"]
    value = _compute_list_neighbours(path)
    _nb_cache.update(at=now, path=path, value=value)
    return value


def _compute_list_neighbours(path) -> dict[int, dict[int, float]]:
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
        # A short list is an order: everything on it is related. A long one (the
        # 59-series 'Batman' collection) is a shelf: only true neighbours count,
        # two positions either side, or every Batman book relates to every other.
        big = len(seq) > 15
        for i, (pa, a) in enumerate(seq):
            for pb, b in seq:
                if a == b:
                    continue
                near = abs(pa - pb) <= 2
                if big and not near:
                    continue
                out[a][b] = max(out[a][b], (1.5 if big else 2.0) + (1.0 if near else 0.0))
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
            if min(w, w2) < 1.0:             # a shared letterer or colorist is not a relation
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
        r["kind"] = "owned"
    # Outward: entries you don't have on the lists you've been reading from —
    # related by the list's own say-so, and one Get away.
    gaps = _list_gaps_near(seeds, path, titles)
    return (out + gaps)[:limit + len(gaps)]


_lists_cache = {"at": 0.0, "path": None, "value": None}
LISTS_TTL_S = 120


def _resolved_lists(path) -> list[dict]:
    """Every reading list resolved ONCE and kept warm: a Because-rows call used
    to resolve all nineteen lists per anchor (114 resolutions, 2–4 s a page).
    Gaps and adjacency change on import or download, not per tap."""
    import time
    from kometa import readlists
    now = time.time()
    if _lists_cache["value"] is not None and _lists_cache["path"] == path and now - _lists_cache["at"] < LISTS_TTL_S:
        return _lists_cache["value"]
    out = []
    try:
        readlists.ensure_tables(path)
        with db._connect(path) as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM reading_lists ORDER BY name")]
        for lid in ids:
            try:
                out.append(readlists.resolve(lid, path))
            except Exception as e:
                logger.info(f"Related: list {lid} skipped: {e}")
    except Exception as e:
        logger.info(f"Related: lists unavailable: {e}")
    _lists_cache.update(at=now, path=path, value=out)
    return out


def warm_lists(path=None) -> int:
    """Scheduler: keep the resolved lists and the neighbour map warm so the
    first On Deck after a restart doesn't pay the 2.5 s build. Returns lists."""
    path = path or DB_PATH
    _lists_cache["value"] = None
    n = len(_resolved_lists(path))
    _list_neighbours(path)
    return n


def _list_gaps_near(seeds: list[int], path, titles: dict, per_list: int = 4) -> list[dict]:
    out, seen = [], set()
    for res in _resolved_lists(path):
        l = {"id": res["id"], "name": res["name"]}
        on_list = {e["series_id"] for e in res["entries"] if e.get("series_id")}
        touching = [sid for sid in seeds if sid in on_list]
        if not touching:
            continue
        # the next gaps after the furthest seed on the list: what comes next in that order
        last_pos = max((e["position"] for e in res["entries"] if e.get("series_id") in touching), default=0)
        n = 0
        for e in res["entries"]:
            if e["status"] == "owned" or e["position"] < last_pos or e["series"] in seen:
                continue
            seen.add(e["series"])
            out.append({"kind": "gap", "series_id": e.get("series_id"), "title": e["series"], "list_id": l["id"],
                        "list_name": l["name"], "item_id": e["item_id"], "cover": e.get("cover"),
                        "why": [f"next on {l['name']}"], "because": [titles.get(s, "") for s in touching[:1]],
                        "owned": 0, "total": 0, "score": 0, "started": False})
            n += 1
            if n >= per_list:
                break
    return out


def because_rows(max_rows: int = 2, min_items: int = 4, path=None, exclude=None, cached_only: bool = True):
    """On Deck's discovery rows: 'Because you read {series}', one per anchor,
    the anchors being what you've read most recently. The row title IS the
    reason, so the cards carry none. Each row mixes what the shelf holds near
    the anchor (unstarted first), the next gaps on lists the anchor sits on,
    and catalogue runs by its writers and artists. A series is used once across
    rows; rows under min_items don't exist. → (rows, pending)."""
    path = path or DB_PATH
    seeds = _recent_series(path)
    if not seeds:
        return [], False
    # what you rated highly anchors before what you merely read (spec: ratings feed suggestions)
    try:
        from kometa.marks import rated_series
        rated = rated_series(path)
        seeds.sort(key=lambda s: -(rated.get(s) or 0))
    except Exception as e:
        logger.info(f"Because-row: ratings skipped: {e}")
    sig, nb = _signals(path), _list_neighbours(path)
    titles = {s["id"]: s["title"] for s in db.get_all_series(path)}
    with db._connect(path) as conn:
        started = {r[0] for r in conn.execute(
            "SELECT DISTINCT b.tracked_series_id FROM read_progress p JOIN books b ON b.id = p.book_id WHERE p.reader_id = ?", (READER_ID,))}
    used = {f"s:{x}" for x in (exclude or [])} | {f"s:{x}" for x in seeds}
    rows, pending = [], False
    for seed in seeds:
        owned = [dict(r, kind="owned", because=[], started=r["series_id"] in started)
                 for r in related(seed, limit=30, path=path, signals=sig, neighbours=nb)
                 if f"s:{r['series_id']}" not in used]
        owned.sort(key=lambda r: (r["started"], -r["score"]))
        gaps = [g for g in _list_gaps_near([seed], path, titles)
                if f"s:{g['series_id']}" not in used and f"t:{norm_key(g['title'])}" not in used]
        cat = []
        try:
            cat, p = outward([seed], limit=8, path=path, cached_only=True)
            pending = pending or p
        except Exception as e:
            logger.info(f"Because-row: outward for {seed} skipped: {e}")
        cat = [dict(c, because=[]) for c in cat if f"m:{c['metron_series_id']}" not in used]
        # unstarted shelf runs lead (one tap from reading), then the list's next
        # gaps, then the catalogue, then runs already started
        items = [r for r in owned if not r["started"]] + gaps + cat + [r for r in owned if r["started"]]
        if len(items) < min_items:
            continue
        items = items[:16]
        for it in items:
            if it.get("series_id"):
                used.add(f"s:{it['series_id']}")
            if it.get("metron_series_id"):
                used.add(f"m:{it['metron_series_id']}")
            used.add(f"t:{norm_key(it['title'])}")
        rows.append({"anchor_id": seed, "anchor": titles.get(seed, ""), "items": items})
        if len(rows) >= max_rows:
            break
    return rows, pending


# --- outward: the catalogue, not the shelf ---------------------------------------
def _cached_works(creator_id: int, path) -> list[dict] | None:
    with db._connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS creator_works (
            creator_id INTEGER PRIMARY KEY, works_json TEXT, fetched_at TEXT DEFAULT (datetime('now')))""")
        r = conn.execute("SELECT works_json, fetched_at FROM creator_works WHERE creator_id = ?", (creator_id,)).fetchone()
    if not r or _stale(r["fetched_at"]):
        return None
    works = json.loads(r["works_json"] or "[]")
    if works and works[0].get("partial"):
        # a fill Metron cut short: good for ten minutes, then ask again
        try:
            age = (datetime.utcnow() - datetime.strptime(r["fetched_at"][:19], "%Y-%m-%d %H:%M:%S")).total_seconds()
        except Exception:
            age = 1e9
        if age > 600:
            return None
    return works


_filling: set = set()


def _fill_works_in_background(creator_ids: list[int], path):
    """One thread per batch: the row renders now from cache and fills on the next look."""
    todo = [c for c in creator_ids if c not in _filling]
    if not todo:
        return
    _filling.update(todo)

    def run():
        try:
            for cid in todo:
                try:
                    creator_works(cid, path)
                except Exception as e:
                    logger.info(f"Outward: creator {cid} fetch failed: {e}")
        finally:
            _filling.difference_update(todo)
    threading.Thread(target=run, daemon=True).start()


ROLE_LOOKUPS = 30                     # issue details fetched per creator, to learn the role on each work
_ROLE_LABEL = {"writer": "Writer", "story": "Writer", "script": "Writer", "plot": "Writer", "artist": "Artist",
               "penciller": "Artist", "inker": "Inker", "colorist": "Colorist", "letterer": "Letterer", "cover": "Cover"}


def _roles_of(detail: dict, creator_id: int) -> list[str]:
    return sorted({(c.get("role") or "").lower() for c in detail.get("credits") or []
                   if c.get("metron_creator_id") == creator_id and c.get("role")})


def work_label(w: dict, name: str) -> str:
    """'Artist: Paul Pope' — the person's role on THAT work, not on the seed.
    Paul Pope drew Batman: Year 100; on Adventure Time he only did a cover."""
    roles = w.get("roles") or []
    main = next((r for r in ("writer", "story", "script", "plot", "artist", "penciller", "inker", "colorist", "letterer", "cover")
                 if r in roles), None)
    return f"{_ROLE_LABEL.get(main, main.title() if main else 'Credit')}: {name}"


def creator_works(creator_id: int, path=None, fetch=None, detail=None) -> list[dict]:
    """Series a creator worked on, from Metron's issue list, grouped: [{metron_series_id,
    title, year, count, cover, roles, cover_only}]. Metron's creator filter answers
    with EVERY credit, variant covers included — so each work gets one issue-detail
    look to learn the role, and a cover-only credit is flagged (a variant cover
    isn't 'more from' anyone). Cached 30 days."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS creator_works (
            creator_id INTEGER PRIMARY KEY, works_json TEXT, fetched_at TEXT DEFAULT (datetime('now')))""")
        r = conn.execute("SELECT works_json, fetched_at FROM creator_works WHERE creator_id = ?", (creator_id,)).fetchone()
    if r and not _stale(r["fetched_at"]):
        return json.loads(r["works_json"] or "[]")
    from kometa import metron_client
    rows = (fetch or (lambda cid: metron_client._all_pages("issue/", max_pages=3, creator_id=cid)))(creator_id)
    by: dict[int, dict] = {}
    for i in rows:
        ser = i.get("series") or {}
        sid = ser.get("id")
        if not sid:
            continue
        w = by.setdefault(sid, {"metron_series_id": sid, "title": re.sub(r"\s*\(\d{4}\)\s*$", "", ser.get("name") or ""),
                                "year": ser.get("year_began"), "count": 0, "cover": None})
        w["count"] += 1
        if not w["cover"] and i.get("image"):
            w["cover"] = i["image"]
        if w.get("_issue") is None and i.get("id"):
            w["_issue"] = i["id"]
    works = sorted(by.values(), key=lambda w: -w["count"])[:60]
    detail = detail or metron_client.issue_detail
    looked, partial = 0, False
    for w in works:
        iid = w.pop("_issue", None)
        w["roles"], w["cover_only"] = [], False
        if not iid or w["count"] < 2 or looked >= ROLE_LOOKUPS or partial:
            continue
        looked += 1
        try:
            w["roles"] = _roles_of(detail(iid), creator_id)
            w["cover_only"] = bool(w["roles"]) and set(w["roles"]) <= {"cover"}
        except Exception as e:
            # Metron said no (rate limit, outage): keep what we have, but as a
            # PARTIAL fill that goes stale in minutes, not a month — otherwise a
            # bad moment freezes 'Credit: Name' on every card for 30 days.
            partial = True
            logger.info(f"Creator {creator_id}: role lookups paused at {w['title']!r}: {e}")
    if partial:
        for w in works:
            w["partial"] = True
    with db._connect(path) as conn:
        conn.execute("INSERT OR REPLACE INTO creator_works (creator_id, works_json, fetched_at) VALUES (?, ?, datetime('now'))",
                     (creator_id, json.dumps(works)))
    return works


def outward(seed_ids: list[int], limit: int = 12, path=None, fetch=None, max_creators: int = 5,
            cached_only: bool = False) -> list[dict] | tuple[list[dict], bool]:
    """Series NOT on the shelf by the writers and artists of the seed series.
    cached_only: answer from the creator cache now, fetch the rest in the
    background, and say whether anything is still coming → (rows, pending)."""
    path = path or DB_PATH
    sig = _signals(path)
    weight: dict[int, tuple[float, str, str]] = {}
    for seed in seed_ids:
        for c in sig.get(seed, {}).get("creators", []):
            w = _ROLE_WEIGHT.get(c.get("role"), 1.0)
            if w >= 2.0 and c.get("id"):
                prev = weight.get(c["id"], (0, "", ""))
                weight[c["id"]] = (prev[0] + w, c["name"], "wrote" if w >= 3 else "drew")
    top = sorted(weight.items(), key=lambda kv: -kv[1][0])[:max_creators]
    all_series = [s for s in db.get_all_series(path) if s.get("kind") != "arc"]
    have_ids = {s.get("metron_series_id") for s in all_series if s.get("metron_series_id")}
    have_titles = {norm_key(re.sub(r"\s*\(\d{4}\)\s*$", "", s["title"])) for s in all_series}
    out: dict[int, dict] = {}
    pending = False
    missing = [cid for cid, _ in top if cached_only and _cached_works(cid, path) is None]
    if missing:
        pending = True
        _fill_works_in_background(missing, path)
    for cid, (w, name, verb) in top:
        if cached_only:
            works = _cached_works(cid, path)
            if works is None:
                continue
        else:
            try:
                works = creator_works(cid, path, fetch)
            except Exception as e:
                logger.info(f"Outward: creator {name!r} skipped: {e}")
                continue
        for wk in works:
            if wk["metron_series_id"] in have_ids or norm_key(wk["title"]) in have_titles or wk["count"] < 2 or wk.get("cover_only"):
                continue
            o = out.setdefault(wk["metron_series_id"], {"kind": "catalogue", "metron_series_id": wk["metron_series_id"],
                                                        "title": wk["title"], "year": wk["year"], "cover": wk["cover"],
                                                        "score": 0.0, "why": [], "owned": 0, "total": wk["count"]})
            o["score"] += w * min(wk["count"], 12) / 12
            o["why"].append(work_label(wk, name))
    res = sorted(out.values(), key=lambda o: -o["score"])[:limit]
    for o in res:
        o["why"] = o["why"][:2]
    return (res, pending) if cached_only else res


# --- per-creator: "More from {Name}" rows, and the creator modal ---------------------
def _creator_shelf(creator_id: int, sig: dict[int, dict], series: dict[int, dict], exclude: int | None = None,
                   name: str | None = None) -> list[dict]:
    """Shelf series carrying this creator id in their signals, as owned cards,
    each saying the person's role on THAT series: 'Artist: Paul Pope'."""
    out = []
    for sid, s_ in sig.items():
        if sid == exclude or sid not in series or series[sid].get("kind") == "arc":
            continue
        roles = sorted({(c.get("role") or "").lower() for c in s_["creators"] if c.get("id") == creator_id and c.get("role")})
        if roles:
            s = series[sid]
            out.append({"kind": "owned", "series_id": sid, "title": s["title"], "publisher": s.get("publisher"),
                        "owned": s.get("owned") or 0, "total": (s.get("owned") or 0) + (s.get("missing") or 0),
                        "why": [work_label({"roles": roles}, name)] if name else [], "because": [], "score": 0.0})
    return out


def _creator_catalogue(creator_id: int, path, series: dict[int, dict], cached_only: bool = True, fetch=None, name: str | None = None,
                       include_cover_only: bool = False) -> tuple[list[dict], bool]:
    """Catalogue works by this creator not on the shelf, as catalogue cards. → (rows, pending)."""
    have_ids = {s.get("metron_series_id") for s in series.values() if s.get("metron_series_id")}
    have_titles = {norm_key(re.sub(r"\s*\(\d{4}\)\s*$", "", s["title"])) for s in series.values()}
    if cached_only:
        works = _cached_works(creator_id, path)
        if works is None:
            _fill_works_in_background([creator_id], path)
            return [], True
    else:
        works = creator_works(creator_id, path, fetch)
    out = [{"kind": "catalogue", "metron_series_id": w["metron_series_id"], "title": w["title"], "year": w["year"],
            "cover": w["cover"], "score": float(w["count"]), "why": [work_label(w, name)] if name else [], "owned": 0, "total": w["count"]}
           for w in works
           if w["metron_series_id"] not in have_ids and norm_key(w["title"]) not in have_titles and w["count"] >= 2
           and (include_cover_only or not w.get("cover_only"))]
    return out, False


def _top_creators(series_id: int, sig: dict[int, dict], max_creators: int) -> list[tuple[int, str, str]]:
    """(id, name, verb) for the series' weightiest credited people with a Metron id."""
    weight: dict[int, tuple[float, str, str]] = {}
    for c in sig.get(series_id, {}).get("creators", []):
        w = _ROLE_WEIGHT.get(c.get("role"), 1.0)
        if w >= 2.0 and c.get("id"):
            prev = weight.get(c["id"], (0, "", ""))
            weight[c["id"]] = (prev[0] + w, c["name"], "wrote" if w >= 3 else "drew")
    return [(cid, name, verb) for cid, (w, name, verb) in sorted(weight.items(), key=lambda kv: -kv[1][0])[:max_creators]]


def creator_rows(series_id: int, max_rows: int = 2, min_items: int = 4, path=None, cached_only: bool = True):
    """The series page's 'More from {Name}' rows: one per weighty credit, shelf
    series by them first, then catalogue runs. The title is the reason, so the
    cards carry none. Rows under min_items don't exist. → (rows, pending)."""
    path = path or DB_PATH
    sig = _signals(path)
    series = {s["id"]: s for s in db.get_all_series(path)}
    rows, pending = [], False
    for cid, name, verb in _top_creators(series_id, sig, max_creators=max_rows + 2):
        # same rule both sides of the row: a cover alone isn't 'more from' anyone
        shelf = [x for x in _creator_shelf(cid, sig, series, exclude=series_id, name=name)
                 if not (x.get("why") and x["why"][0].startswith("Cover:"))]
        cat, p = _creator_catalogue(cid, path, series, cached_only, name=name)
        pending = pending or p
        items = shelf + cat
        if len(items) < min_items:
            continue
        rows.append({"creator_id": cid, "name": name, "role_verb": verb, "items": items[:16]})
        if len(rows) >= max_rows:
            break
    return rows, pending


def creator_page(creator_id: int, name: str | None = None, path=None, cached_only: bool = True) -> dict:
    """The creator modal: everything by one person, shelf then catalogue."""
    path = path or DB_PATH
    sig = _signals(path)
    series = {s["id"]: s for s in db.get_all_series(path)}
    if not name:
        name = next((c["name"] for s_ in sig.values() for c in s_["creators"] if c.get("id") == creator_id), None)
    shelf = _creator_shelf(creator_id, sig, series, name=name)
    # everything by this person, covers included — labelled 'Cover: Name' like the modal's credits
    cat, pending = _creator_catalogue(creator_id, path, series, cached_only, name=name, include_cover_only=True)
    return {"creator_id": creator_id, "name": name, "shelf": shelf, "catalogue": cat, "pending": pending}


def _safe_fill(series_id: int):
    try:
        fill_signals(series_id, DB_PATH)
    except Exception as e:
        logger.info(f"Related: signals for series {series_id} not available yet: {e}")


# --- API --------------------------------------------------------------------------
@router.get("/api/series/{series_id}/related")
def api_related(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    sig = _signals(DB_PATH)
    pending = False
    if series_id not in sig or _stale(sig[series_id]["fetched_at"]) or _needs_ids(sig[series_id]):
        pending = True
        threading.Thread(target=lambda: _safe_fill(series_id), daemon=True).start()
    out, out_pending = [], False
    try:
        out, out_pending = outward([series_id], limit=8, cached_only=True)
    except Exception as e:
        logger.info(f"Related: outward skipped: {e}")
    creators, c_pending = [], False
    try:
        creators, c_pending = creator_rows(series_id, path=DB_PATH)
    except Exception as e:
        logger.info(f"Related: creator rows skipped: {e}")
    return {"related": related(series_id), "outward": out, "creators": creators,
            "pending": pending or out_pending or c_pending}


@router.get("/api/creators/{creator_id}")
def api_creator(creator_id: int, name: str = ""):
    return creator_page(creator_id, name or None, path=DB_PATH)


@router.get("/api/ondeck/because")
def api_because(exclude: str = ""):
    """exclude: series ids already on the page's task rows, comma-separated."""
    ids = [int(x) for x in exclude.split(",") if x.strip().isdigit()]
    rows, pending = because_rows(path=DB_PATH, exclude=ids)
    return {"rows": rows, "pending": pending}


@router.get("/api/ondeck/suggestions")
def api_suggestions():
    sug = suggestions()
    out, pending = [], False
    try:
        out, pending = outward(_recent_series(DB_PATH), limit=10, cached_only=True)
    except Exception as e:
        logger.info(f"Suggestions: outward skipped: {e}")
    for o in out:
        o["because"] = []
    return {"suggestions": sug + out, "pending": pending}
