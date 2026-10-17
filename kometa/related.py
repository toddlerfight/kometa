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
from kometa.auth import current_reader
from kometa.naming import norm_key, _pub_key

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
SIGNAL_TTL_DAYS = 30
EMPTY_RETRY_DAYS = 1                 # an empty answer with nothing to ask is retried this soon
RECENT_DAYS = 60
_ROLE_WEIGHT = {"writer": 3.0, "story": 3.0, "script": 3.0, "plot": 2.0, "artist": 2.0, "penciller": 2.0,
                "inker": 1.0, "colorist": 0.5, "letterer": 0.25, "cover": 0.0}


def _role_weight(role: str | None) -> float:
    """Weight for creator roles. Unknown roles (editor, designer, executive producer,
    cover artist) default strictly to 0.0 so editorial staff never pollutes relations."""
    if not role:
        return 0.0
    r = role.lower().strip()
    if r in _ROLE_WEIGHT:
        return _ROLE_WEIGHT[r]
    if "," in r:
        return max((_ROLE_WEIGHT.get(p.strip(), 0.0) for p in r.split(",")), default=0.0)
    return 0.0


def _clean_stem(title: str | None) -> str:
    """Extract clean franchise stem for publisher-bound fallback matching.
    Splits on ':', '-', or '(' and requires length >= 4."""
    if not title:
        return ""
    t = re.split(r"[:\-\(]", title)[0].strip()
    return t if len(t) >= 4 else ""


@db.once_per_db
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
        total_issues = len(rows)
        creator_issues: dict[tuple[str, str], set] = defaultdict(set)
        creator_info: dict[tuple[str, str], dict] = {}
        for r in rows:
            iss_num = r.get("number")
            for c in r.get("credits") or []:
                name = c.get("name")
                if not name:
                    continue
                role = (c.get("role") or "").lower()
                key = (norm_key(name), role)
                creator_issues[key].add(iss_num)
                if key not in creator_info:
                    creator_info[key] = {"role": role, "name": name, "id": c.get("metron_creator_id")}
                elif c.get("metron_creator_id") and not creator_info[key].get("id"):
                    creator_info[key]["id"] = c.get("metron_creator_id")
        # Keep creators with >= 20% presence across the run (drops guest creators like Snyder on Dept of Truth)
        creators = [info for key, info in creator_info.items()
                    if total_issues <= 1 or (len(creator_issues[key]) / total_issues) >= 0.20]
        arcs = sorted({a for r in rows for a in (r.get("arcs") or []) if a})
    elif issue:
        from kometa import metron_client
        d = (detail or metron_client.issue_detail)(issue["metron_issue_id"])
        for c in d.get("credits") or []:
            if c.get("name"):
                creators.append({"role": (c.get("role") or "").lower(), "name": c["name"], "id": c.get("metron_creator_id")})
        arcs = [a for a in (d.get("arcs") or []) if a]
    asked = bool(issue) or bool(rows)
    if not asked:
        # Safety net: series has a Metron series ID, but local issues have no Metron issue IDs yet.
        # Restrict to series with exactly one Metron issue and one local issue, or match by issue number; never guess.
        s_ = db.get_series_by_id(series_id, path)
        if s_ and s_.get("metron_series_id"):
            from kometa import metron_client
            try:
                m_issues = metron_client.series_issues(s_["metron_series_id"])
                local_issues = db.get_issues_for_series(series_id, path)
                owned_issues = [i for i in local_issues if i.get("owned")]
                matched_mid, matched_num = None, None

                if len(m_issues) == 1 and len(owned_issues) == 1:
                    # Exactly one Metron issue and one local issue (e.g. Deluxe Edition / one-shot)
                    matched_mid = m_issues[0].get("metron_issue_id")
                    matched_num = owned_issues[0].get("number")
                elif m_issues and owned_issues:
                    # Match strictly by exact issue number
                    m_by_num = {float(mi["number"]): mi.get("metron_issue_id")
                                for mi in m_issues if mi.get("number") is not None}
                    for oi in owned_issues:
                        num = oi.get("number")
                        if num is not None and float(num) in m_by_num:
                            matched_mid = m_by_num[float(num)]
                            matched_num = num
                            break

                if matched_mid:
                    d = (detail or metron_client.issue_detail)(matched_mid)
                    for c in d.get("credits") or []:
                        if c.get("name"):
                            creators.append({"role": (c.get("role") or "").lower(), "name": c["name"], "id": c.get("metron_creator_id")})
                    arcs = [a for a in (d.get("arcs") or []) if a]
                    with db._connect(path) as conn:
                        conn.execute("UPDATE issue_status SET metron_issue_id = ? WHERE tracked_series_id = ? AND number = ?",
                                     (matched_mid, series_id, matched_num))
                    asked = True
            except Exception as e:
                logger.info(f"Signals: Metron series fallback skipped for {series_id}: {e}")
    with db._connect(path) as conn:
        # Nothing to ask yet (the issues or their credits haven't landed): the empty answer
        # is dated so it's looked at again tomorrow, not in a month. 4 Kids Walk Into A Bank
        # got its empty row half an hour before its issues arrived and showed nothing for a day.
        stamp = "datetime('now')" if (asked and creators) else f"datetime('now', '-{SIGNAL_TTL_DAYS - EMPTY_RETRY_DAYS} days')"
        conn.execute("INSERT OR REPLACE INTO series_signals (tracked_series_id, creators_json, arcs_json, fetched_at) "
                     f"VALUES (?, ?, ?, {stamp})", (series_id, json.dumps(creators), json.dumps(arcs)))
    return asked


def _signals(path) -> dict[int, dict]:
    ensure_tables(path)
    with db._connect(path) as conn:
        return {r["tracked_series_id"]: {"creators": json.loads(r["creators_json"] or "[]"),
                                         "arcs": json.loads(r["arcs_json"] or "[]"),
                                         "fetched_at": r["fetched_at"]}
                for r in conn.execute("SELECT * FROM series_signals")}


def _needs_ids(sig: dict) -> bool:
    """Signals written before creator ids were kept can't look outward."""
    return any(c.get("id") is None for c in sig.get("creators", []) if _role_weight(c.get("role")) >= 2.0)


def _stale(fetched_at: str | None) -> bool:
    if not fetched_at:
        return True
    try:
        return datetime.strptime(fetched_at, "%Y-%m-%d %H:%M:%S") < datetime.utcnow() - timedelta(days=SIGNAL_TTL_DAYS)
    except ValueError:
        return True


def _older_than(fetched_at: str | None, days: float) -> bool:
    if not fetched_at:
        return True
    try:
        return datetime.strptime(fetched_at, "%Y-%m-%d %H:%M:%S") < datetime.utcnow() - timedelta(days=days)
    except ValueError:
        return True


def _askable(path) -> set[int]:
    """Series with something fill_signals can read: a record row with credits, or an issue
    with a Metron id."""
    from kometa.record import ensure_tables as _rec
    _rec(path)
    with db._connect(path) as conn:
        return ({r[0] for r in conn.execute("SELECT DISTINCT tracked_series_id FROM issue_record WHERE credits_json IS NOT NULL AND credits_json NOT IN ('', '[]')")}
                | {r[0] for r in conn.execute("SELECT DISTINCT tracked_series_id FROM issue_status WHERE metron_issue_id IS NOT NULL")})


def trickle_signals(limit: int = 20, path=None) -> int:
    """Background: fill signals for series that have none (or stale ones).
    Stops at the first Metron refusal — the budget is shared with everything."""
    path = path or DB_PATH
    have = _signals(path)
    askable = _askable(path)
    def due(s):
        g = have.get(s["id"])
        if g is None:
            return True
        # an empty row on a run that now has credits or a Metron issue to ask: ask again now.
        # Missing creator ids are asked about once a DAY, not every run: LOCG credits never
        # carry a Metron id, so the twenty alphabetically-first such runs (Aliens…, Avengers,
        # Batman…) were refilled every half hour and the 150 behind them never came up
        # (Puzzlebox, 2026-10-16: tracked at 16:35, still unfilled at 18:00).
        # An askable empty is re-asked at once (its issues just landed). A run that fills
        # empty every time — an anthology whose creators are all one-issue guests, under
        # the 20% rule — is re-stamped on each try and so sorts to the back of its tier.
        return (_stale(g["fetched_at"]) or (_needs_ids(g) and _older_than(g["fetched_at"], EMPTY_RETRY_DAYS))
                or (not g["creators"] and s["id"] in askable))
    # LOCG-only runs count too: fill_signals reads the record's LOCG credits (or queues the
    # top-up) — before, 99 of them never got a row and showed no Related at all
    todo = [s for s in db.get_all_series(path) if s.get("kind") != "arc"
            and (s.get("metron_series_id") or s.get("locg_series_id")) and due(s)]
    # new rows first, then empties that can be asked — and within a tier the LEAST recently
    # tried first, so a run that keeps coming up due can't starve the ones behind it
    todo.sort(key=lambda s: (s["id"] in have, s["id"] not in askable, (have.get(s["id"]) or {}).get("fetched_at") or ""))
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


def _compute_list_neighbours(path, resolved: list[dict] | None = None) -> dict[int, dict[int, float]]:
    """From the resolved lists — the warm copy, not a second resolve of every
    list (it resolved them all again right after warm_lists had: ~0.35 s of CPU
    every few minutes, all day)."""
    out: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    if resolved is None:
        try:
            resolved = _resolved_lists(path)
        except Exception:
            return out
    for res in resolved:
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
            w = _role_weight(c.get("role"))
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
        w = _role_weight(c.get("role"))
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
    series = _series_with_counts(path)
    if not scores:
        my_s = series.get(series_id) or (db.get_series_by_id(series_id, path) if path else None)
        if my_s:
            my_pub = _pub_key(my_s.get("publisher") or "")
            my_stem = _clean_stem(my_s.get("title", "")).lower()
            if my_pub and my_stem:
                for sid, s in series.items():
                    if sid == series_id or s.get("kind") == "arc":
                        continue
                    cand_pub = _pub_key(s.get("publisher") or "")
                    cand_stem = _clean_stem(s.get("title", "")).lower()
                    if cand_pub == my_pub and cand_stem == my_stem:
                        scores[sid] = 1.5
                        why[sid].append(f"Franchise: {_clean_stem(my_s.get('title', ''))}")
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
            GROUP BY b.tracked_series_id ORDER BY MAX(p.updated_at) DESC LIMIT 20""", (current_reader(), since))]


_lists_cache = {"at": 0.0, "path": None, "value": None}
LISTS_TTL_S = 120


def _series_with_counts(path) -> dict[int, dict]:
    """tracked_series rows WITH owned/missing: the bare row has no counts, so
    every shelf card in a related row read 0/0 (and hid it)."""
    series = {s["id"]: s for s in db.get_all_series(path)}
    for sid, c in db.get_all_series_summaries(path).items():
        if sid in series:
            series[sid].update(owned=c.get("owned") or 0, missing=c.get("missing") or 0)
    return series


def _build_lists(path) -> list[dict]:
    from kometa import readlists
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
    return out


_lists_lock = threading.Lock()


def _refresh_lists(path) -> list[dict]:
    """Build once and SWAP — never empty the cache first. ONE builder at a time:
    a second caller waits for the first instead of starting its own 12 s walk."""
    import time
    with _lists_lock:
        fresh = _build_lists(path)
        _lists_cache.update(at=time.time(), path=path, value=fresh)
        return fresh


def _resolved_lists(path) -> list[dict]:
    """Every reading list resolved ONCE and kept warm: a Because-rows call used
    to resolve all nineteen lists per anchor (114 resolutions, 2–4 s a page).

    Stale-while-revalidate (2026-10-10): an expired copy is still SERVED and
    one background thread rebuilds it. Pages only wait on a cold start. The old
    shape — expire, then whoever asks rebuilds inline, and the warm job blanked
    the cache before rebuilding — put 8–15 s on On Deck and every series page,
    with several requests rebuilding at once and fighting over the disk."""
    import time
    if _lists_cache["value"] is not None and _lists_cache["path"] == path:
        if time.time() - _lists_cache["at"] >= LISTS_TTL_S and not _lists_lock.locked():
            threading.Thread(target=_refresh_lists, args=(path,), daemon=True).start()
        return _lists_cache["value"]
    return _refresh_lists(path)


_warm_sig = {"path": None, "sig": None}


def _lists_fingerprint(path) -> tuple:
    """What a resolved list depends on — the books, the lists, the runs, your
    progress. Unchanged since the last warm-up → nothing to rebuild."""
    import sqlite3

    def q(conn, sql):
        try:
            return tuple(conn.execute(sql).fetchone())
        except sqlite3.OperationalError:
            return None                             # a table this DB doesn't have yet
    with db._connect(path) as conn:
        return tuple(q(conn, sql) for sql in (
            "SELECT COUNT(*), MAX(id), MAX(mtime) FROM books",
            "SELECT COUNT(*), MAX(id) FROM reading_list_items",
            "SELECT COUNT(*), MAX(id) FROM tracked_series",
            "SELECT COUNT(*), MAX(updated_at) FROM read_progress",
            "SELECT COUNT(*), SUM(owned), SUM(ignored), COUNT(covered_by) FROM issue_status"))


def warm_lists(path=None) -> int:
    """Scheduler: keep the resolved lists and the neighbour map warm. Rebuilds
    and swaps; readers keep the old copy until the new one lands. Skipped when
    nothing the lists depend on has changed since the last rebuild."""
    path = path or DB_PATH
    sig = _lists_fingerprint(path)
    if (_warm_sig["path"] == path and _warm_sig["sig"] == sig and _lists_cache["value"] is not None
            and _nb_cache["value"] is not None):
        now = time.time()
        _lists_cache["at"] = now
        _nb_cache["at"] = now
        return len(_lists_cache["value"])
    fresh = _refresh_lists(path)
    _nb_cache.update(at=time.time(), path=path, value=_compute_list_neighbours(path, fresh))
    _warm_sig.update(path=path, sig=sig)
    return len(fresh)


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
            if e["status"] == "owned" or e["position"] < last_pos or e["series"] in seen or not e.get("cover"):
                continue            # no cover, no suggestion (the owner, 2026-10-17)
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
            "SELECT DISTINCT b.tracked_series_id FROM read_progress p JOIN books b ON b.id = p.book_id WHERE p.reader_id = ?", (current_reader(),))}
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


# --- a person's work by what they did on it (2026-10-17) -----------------------------------
# The creator page listed shelf then catalogue, the role in small grey type under each
# card: Javier Rodríguez's own art sat mixed in with books he only coloured. Now it's
# sectioned by role, highest first, and a work sits once, under its highest role.
ROLE_GROUPS = (("writer", "Writer", {"writer", "story", "script", "plot"}),
               ("artist", "Artist", {"artist", "penciller", "pencils", "pencil", "art", "layouts", "breakdowns"}),
               ("colorist", "Colorist", {"colorist", "colors", "colours", "colourist"}),
               ("inker", "Inker", {"inker", "inks", "finishes"}),
               ("cover", "Covers", {"cover", "cover artist", "covers"}),
               ("letterer", "Letterer", {"letterer", "letters", "lettering"}))
_ROLE_WORD = {"writer": "writer", "artist": "artist", "colorist": "colorist", "inker": "inker",
              "cover": "cover", "letterer": "letterer"}
_ALL_ROLE_NAMES = set().union(*(names for _k, _l, names in ROLE_GROUPS))


def role_groups(roles) -> list[str]:
    """The groups a person's roles on one work fall in, highest first; 'other' when none
    of them is a known role (or Metron's role lookups didn't reach this work)."""
    parts = {p.strip() for r in roles or [] for p in re.split(r"\s*[,/&]\s*|\s+and\s+", (r or "").lower()) if p.strip()}
    got = [key for key, _label, names in ROLE_GROUPS if parts & names]
    return got or ["other"]


def creator_sections(items: list[dict]) -> list[dict]:
    """[{role, label, items}] in role order, then 'Other credits'. Each card names only the
    roles the section doesn't ('Also colorist, cover'): the heading says the first."""
    labels = {k: label for k, label, _names in ROLE_GROUPS} | {"other": "Other credits"}
    buckets: dict[str, list] = {k: [] for k in labels}
    for it in items:
        groups = role_groups(it.get("roles"))
        also = [_ROLE_WORD[g] for g in groups[1:] if g in _ROLE_WORD]
        buckets[groups[0]].append(dict(it, why=[f"Also {', '.join(also)}"] if also else []))
    return [{"role": k, "label": labels[k], "items": v} for k, v in buckets.items() if v]


# --- every credit gets its person's Metron id, so every name opens their page ------------------
# 2026-10-17: w0rldtr33 #23 (not out yet) carried LOCG's credits — LOCG people ids, no Metron
# ids — so its names weren't clickable, though #20 credits the same four people with theirs.
# A missing id comes from the shelf's credits by name first; a name the shelf doesn't know is
# asked of Metron once, in the background, and the answer kept (a miss too, for a month).
# A name two people answer to is left alone: a guess opens the wrong person's page.
_IDS_TTL_S = 600
CREATOR_MISS_DAYS = 30
_ids_by_name: dict = {"at": 0.0, "path": None, "map": {}}
_asking: set = set()
_ask_lock = threading.Lock()


def _name_key(name: str) -> str:
    import unicodedata
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", name or "") if not unicodedata.combining(ch))
    return norm_key(plain)


def _ensure_creator_ids(path):
    with db._connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS creator_ids (
            name_key TEXT PRIMARY KEY, creator_id INTEGER, asked_at TEXT DEFAULT (datetime('now')))""")


def creator_ids_by_name(path=None) -> dict[str, int]:
    """name key → Metron creator id: the shelf's credits (series_signals), then names asked of Metron."""
    path = path or DB_PATH
    c = _ids_by_name
    if c["path"] == path and time.time() - c["at"] < _IDS_TTL_S:
        return c["map"]
    seen: dict[str, set] = {}
    for sig in _signals(path).values():
        for cr in sig.get("creators") or []:
            if cr.get("id") and cr.get("name"):
                seen.setdefault(_name_key(cr["name"]), set()).add(int(cr["id"]))
    known = {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}
    _ensure_creator_ids(path)
    with db._connect(path) as conn:
        for k, cid in conn.execute("SELECT name_key, creator_id FROM creator_ids WHERE creator_id IS NOT NULL"):
            if k not in seen:
                known[k] = cid
    c.update(at=time.time(), path=path, map=known)
    return known


def _asked_recently(keys: list[str], path) -> set[str]:
    with db._connect(path) as conn:
        qs = ",".join("?" * len(keys))
        return {r[0] for r in conn.execute(
            f"SELECT name_key FROM creator_ids WHERE name_key IN ({qs}) AND asked_at > datetime('now', ?)",
            (*keys, f"-{CREATOR_MISS_DAYS} days"))}


def resolve_creator_names(names: list[str], path=None, search=None) -> int:
    """Ask Metron for each name's creator id, one call each on the shared throttle; keep what it
    says. Exactly one person by that name (accents and case aside) is an answer; none or several
    is a miss. Stops when Metron rests (nothing kept for the rest: asked again later)."""
    from kometa import metron_client
    path = path or DB_PATH
    search = search or (lambda q: metron_client._get("creator/", name=q).get("results") or [])
    _ensure_creator_ids(path)
    found = 0
    for name in names:
        key = _name_key(name)
        try:
            rows = search(name)
        except metron_client.MetronUnavailable as e:
            logger.info(f"Creator lookups paused: {e}")
            break
        except Exception as e:
            logger.info(f"Creator lookup for {name!r} skipped: {e}")
            continue
        same = {int(r["id"]) for r in rows if r.get("id") and _name_key(r.get("name") or "") == key}
        cid = next(iter(same)) if len(same) == 1 else None
        with db._connect(path) as conn:
            conn.execute("INSERT OR REPLACE INTO creator_ids (name_key, creator_id, asked_at) VALUES (?, ?, datetime('now'))", (key, cid))
        found += cid is not None
    _ids_by_name["at"] = 0.0                       # the next look sees them
    return found


def _resolve_in_background(names: list[str], path):
    with _ask_lock:
        todo = [n for n in names if _name_key(n) not in _asking]
        _asking.update(_name_key(n) for n in todo)
    if not todo:
        return

    def run():
        try:
            resolve_creator_names(todo, path)
        finally:
            with _ask_lock:
                _asking.difference_update(_name_key(n) for n in todo)
    threading.Thread(target=run, name="creator-ids", daemon=True).start()


def with_creator_ids(credits: list[dict], path=None, ask: bool = True) -> list[dict]:
    """The credits, each without a metron_creator_id given its person's. Names nobody has asked
    about go to Metron in the background when `ask` (never for a reader account)."""
    if not credits or all(cr.get("metron_creator_id") for cr in credits):
        return credits
    path = path or DB_PATH
    known = creator_ids_by_name(path)
    out, unknown = [], []
    for cr in credits:
        cid = cr.get("metron_creator_id") or known.get(_name_key(cr.get("name") or ""))
        out.append(dict(cr, metron_creator_id=cid) if cid and not cr.get("metron_creator_id") else cr)
        if not cid and cr.get("name"):
            unknown.append(cr["name"])
    if ask and unknown:
        unknown = list(dict.fromkeys(unknown))
        asked = _asked_recently([_name_key(n) for n in unknown], path)
        fresh = [n for n in unknown if _name_key(n) not in asked]
        if fresh:
            from kometa import metron_client
            if metron_client.configured():
                _resolve_in_background(fresh, path)
    return out


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
            w = _role_weight(c.get("role"))
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
            if wk["metron_series_id"] in have_ids or norm_key(wk["title"]) in have_titles or wk["count"] < 2 \
                    or wk.get("cover_only") or not wk.get("cover"):          # no cover, no suggestion
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
                        "why": [work_label({"roles": roles}, name)] if name else [], "because": [], "score": 0.0,
                        "roles": roles})
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
            "cover": w["cover"], "score": float(w["count"]), "why": [work_label(w, name)] if name else [], "owned": 0, "total": w["count"],
            "roles": w.get("roles") or []}
           for w in works
           if w["metron_series_id"] not in have_ids and norm_key(w["title"]) not in have_titles and w["count"] >= 2
           and (include_cover_only or not w.get("cover_only")) and w.get("cover")]       # no cover, no suggestion
    return out, False


def _top_creators(series_id: int, sig: dict[int, dict], max_creators: int) -> list[tuple[int, str, str, set]]:
    """(id, name, verb, groups here) for the people a 'More from' row is for, in turn: the top
    writer, the top artist, the other writers and artists, then colorists. Ranked by role, a
    co-written book never showed who drew it (Batgirl: Year One: Dixon and Beatty took both
    rows, Marcos Martín had none, 2026-10-17)."""
    people: dict[int, dict] = {}
    for c in sig.get(series_id, {}).get("creators", []):
        if not c.get("id"):
            continue
        e = people.setdefault(c["id"], {"name": c["name"], "weight": 0.0, "groups": set()})
        e["weight"] += _role_weight(c.get("role"))
        e["groups"].update(g for g in role_groups([c.get("role")]) if g != "other")
    by_weight = sorted(people.items(), key=lambda kv: -kv[1]["weight"])
    writers = [kv for kv in by_weight if "writer" in kv[1]["groups"]]
    artists = [kv for kv in by_weight if "artist" in kv[1]["groups"] and kv not in writers]
    colorists = [kv for kv in by_weight if "colorist" in kv[1]["groups"] and kv not in writers and kv not in artists]
    order = writers[:1] + artists[:1] + writers[1:] + artists[1:] + colorists
    verb = lambda g: "wrote" if "writer" in g else "drew" if "artist" in g else "coloured"
    return [(cid, e["name"], verb(e["groups"]), e["groups"]) for cid, e in order[:max_creators]]


def creator_rows(series_id: int, max_rows: int = 4, min_items: int = 4, path=None, cached_only: bool = True):
    """The series page's 'More from {Name}' rows: the writer, the artist, then the rest (see
    _top_creators). Shelf series by them first, then catalogue runs, the work in the role
    they had HERE leading (a row for the book he drew leads with his art). Rows under
    min_items don't exist. → (rows, pending)."""
    path = path or DB_PATH
    sig = _signals(path)
    series = _series_with_counts(path)
    rows, pending = [], False
    for cid, name, verb, here in _top_creators(series_id, sig, max_creators=max_rows + 4):
        # same rule both sides of the row: a cover alone isn't 'more from' anyone
        shelf = [x for x in _creator_shelf(cid, sig, series, exclude=series_id, name=name)
                 if not (x.get("why") and x["why"][0].startswith("Cover:"))]
        if verb == "coloured" and _cached_works(cid, path) is None:
            cat, p = [], False          # a colorist's catalogue isn't fetched for a row (30+ Metron calls): only what's known
        else:
            cat, p = _creator_catalogue(cid, path, series, cached_only, name=name)
        pending = pending or p
        items = sorted(shelf + cat, key=lambda x: role_groups(x.get("roles"))[0] not in here)
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
    series = _series_with_counts(path)
    if not name:
        name = next((c["name"] for s_ in sig.values() for c in s_["creators"] if c.get("id") == creator_id), None)
    shelf = _creator_shelf(creator_id, sig, series, name=name)
    # everything by this person, covers included — labelled 'Cover: Name' like the modal's credits
    cat, pending = _creator_catalogue(creator_id, path, series, cached_only, name=name, include_cover_only=True)
    return {"creator_id": creator_id, "name": name, "shelf": shelf, "catalogue": cat, "pending": pending,
            "sections": creator_sections(shelf + cat)}


def _askable_one(series_id: int, path=None) -> bool:
    """_askable for one series — two indexed lookups, not two table scans per page view."""
    path = path or DB_PATH
    from kometa.record import ensure_tables as _rec
    _rec(path)
    with db._connect(path) as conn:
        return bool(conn.execute("SELECT 1 FROM issue_record WHERE tracked_series_id = ? AND credits_json IS NOT NULL "
                                 "AND credits_json NOT IN ('', '[]') LIMIT 1", (series_id,)).fetchone()
                    or conn.execute("SELECT 1 FROM issue_status WHERE tracked_series_id = ? AND metron_issue_id IS NOT NULL "
                                    "LIMIT 1", (series_id,)).fetchone())


def _added_within(s: dict, days: float) -> bool:
    try:
        return datetime.strptime((s.get("added_at") or "")[:19], "%Y-%m-%d %H:%M:%S") > datetime.utcnow() - timedelta(days=days)
    except ValueError:
        return False


def _safe_fill(series_id: int):
    try:
        fill_signals(series_id, DB_PATH)
    except Exception as e:
        logger.info(f"Related: signals for series {series_id} not available yet: {e}")


# --- API --------------------------------------------------------------------------
def _row(series_id: int) -> list[dict]:
    """The series page's Related row: today's engine, or the phase-1 combiner once the
    owner has seen the evaluation and set config related_engine = 'combined'."""
    if db.get_config(DB_PATH).get("related_engine") == "combined":
        from kometa import related_combined
        try:
            return related_combined.related_combined(series_id, path=DB_PATH)
        except Exception as e:                      # never a blank page over a scoring bug
            logger.warning(f"Related: combined engine failed for {series_id}, using the old one: {e}")
    return related(series_id)


@router.get("/api/series/{series_id}/related")
def api_related(series_id: int):
    s = db.get_series_by_id(series_id, DB_PATH)
    if not s:
        raise HTTPException(404)
    sig = _signals(DB_PATH)
    g = sig.get(series_id)
    # An EMPTY row on a run whose credits are now on disk counts too: Puzzlebox had one
    # (its row was written before its issues landed), so opening its page asked for
    # nothing and said "not pending" — the Related row simply wasn't there (2026-10-16).
    empty_askable = bool(g) and not g["creators"] and _askable_one(series_id, DB_PATH)
    if g is None or _stale(g["fetched_at"]) or _needs_ids(g) or empty_askable:
        threading.Thread(target=lambda: _safe_fill(series_id), daemon=True).start()
    # pending = the clients draw a placeholder row and look again. Not for missing creator
    # ids: LOCG credits never carry them, so that would be a placeholder forever. A run
    # tracked in the last day with no credits yet is pending too — its issues are landing.
    pending = (g is None or _stale(g["fetched_at"]) or empty_askable
               or (not g["creators"] and _added_within(s, days=1)))
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
    return {"related": _row(series_id), "outward": out, "creators": creators,
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

