"""Trades fill what single issues can't.

An issue nobody shares as a single (Fantastic Four #67–70, a point-one like
Secret Avengers #12.1) is usually sitting inside a collected edition that IS
shared. This finds the smallest trade that collects it — across series: the
point-ones live in the Fear Itself trades, not their own run's — and proposes
it in Activity. Nothing downloads until you say so.

  1. What a trade collects. Metron's issue detail for a collected edition lists
     its `reprints` ([{id, issue: 'Fantastic Four (1998) #67'}]). Read once per
     edition, cached in trade_reprints; trade_record.collects_json is filled
     from the same cache.
  2. Which trades collect an issue. Metron has no reverse query, so: its name
     filter is a contains-match — 'Secret Avengers' answers 'Fear Itself: Secret
     Avengers' too — keep the collected types, keep the volumes published within
     a few years after the issue, read their reprints (budgeted, cached), keep
     the ones that reprint it. Results cached in trade_lookup.
  3. The proposal. Smallest edition that covers the most of a series' stuck
     issues (one Unthinkable fills #67–70 and #500), a TPB before an omnibus.
     A download_queue row in state 'suggested' — the worker never picks that
     state up. 'Get this trade' turns it into an ordinary trade grab; the issues
     it fills move to 'via_trade'.
  4. Coverage. An OWNED trade's collects mark those issues covered_by (the
     trade's file): not owned as singles, not missing either. Missing counts,
     sweeps and reading lists honour it.

Metron rules (INSTRUCTIONS.md): the app's one client and its throttle only;
MetronUnavailable stops a pass, the rest waits for the next tick.
"""
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

import kometa.db as db
from kometa.naming import norm_key

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH

DETAIL_BUDGET = 15            # edition details read per pass (Metron's shared throttle paces them)
ROWS_PER_TICK = 4             # stuck issues looked at per scheduled pass
LOOKUP_TTL_DAYS = 14          # a reverse lookup is trusted this long
YEARS_AFTER = 5               # a trade paperback comes out within this many years of what it collects…
YEARS_AFTER_BIG = 15          # …an omnibus or hardcover can be a decade or more later
FORMAT_RANK = {"TPB": 0, "GN": 1, "HC": 2, "Omnibus": 3}
_REPRINT = re.compile(r"^(?P<series>.+?)\s*\((?P<year>\d{4})\)\s*#\s*(?P<num>[\w.½\-]+)\s*$")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS trade_reprints (
            metron_issue_id INTEGER PRIMARY KEY, metron_series_id INTEGER, series_name TEXT, format TEXT,
            number TEXT, title TEXT, store_date TEXT, cover TEXT, reprints_json TEXT, fetched_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS trade_lookup (
            issue_key TEXT PRIMARY KEY, candidates_json TEXT, fetched_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS collection_vols (
            metron_series_id INTEGER PRIMARY KEY, vols_json TEXT, fetched_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS collection_search (
            q TEXT PRIMARY KEY, rows_json TEXT, fetched_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS trade_fallback (
            queue_id INTEGER PRIMARY KEY, state TEXT, proposal_qid INTEGER, rejected_json TEXT, checked_at TEXT)""")


# --- 1. what an edition collects ----------------------------------------------------
def parse_reprint(text: str) -> dict | None:
    """'Fantastic Four (1998) #67' → {series, year, number}."""
    m = _REPRINT.match((text or "").strip())
    if not m:
        return None
    try:
        n = float(m.group("num").replace("½", ".5"))
    except ValueError:
        return None
    return {"series": m.group("series").strip(), "year": int(m.group("year")), "number": n}


def _fmt(series_type) -> str | None:
    from kometa.record import COLLECTED_TYPES
    name = series_type.get("name") if isinstance(series_type, dict) else series_type
    return COLLECTED_TYPES.get(name)


def edition(metron_issue_id: int, path=None, get=None) -> dict:
    """One collected edition's identity and what it reprints — cached forever
    (an edition's contents don't change). Raises MetronUnavailable."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT * FROM trade_reprints WHERE metron_issue_id = ?", (int(metron_issue_id),)).fetchone()
    if r:
        d = dict(r)
        d["reprints"] = json.loads(d.pop("reprints_json") or "[]")
        return d
    if get is None:
        from kometa import metron_client
        raw = metron_client._get(f"issue/{int(metron_issue_id)}/")
    else:
        raw = get(metron_issue_id)
    s = raw.get("series") or {}
    reprints = []
    for rp in raw.get("reprints") or []:
        p = parse_reprint(rp.get("issue") if isinstance(rp, dict) else rp)
        if p:
            reprints.append(dict(p, metron_issue_id=rp.get("id") if isinstance(rp, dict) else None))
    out = {"metron_issue_id": int(metron_issue_id), "metron_series_id": s.get("id"),
           "series_name": re.sub(r"\s*\(\d{4}\)\s*$", "", s.get("name") or ""), "format": _fmt(s.get("series_type")) or "TPB",
           "number": str(raw.get("number") or ""), "title": (raw.get("title") or "").strip() or None,
           "store_date": raw.get("store_date"), "cover": raw.get("image"), "reprints": reprints}
    with db._connect(path) as conn:
        conn.execute("""INSERT OR REPLACE INTO trade_reprints (metron_issue_id, metron_series_id, series_name, format, number,
            title, store_date, cover, reprints_json, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (out["metron_issue_id"], out["metron_series_id"], out["series_name"], out["format"], out["number"],
                      out["title"], out["store_date"], out["cover"], json.dumps(reprints), _now()))
    return out


def ekey(e: dict) -> str:
    """One key for an edition from either catalogue: Metron issue id, or 'l<LOCG id>'."""
    return str(e["metron_issue_id"]) if e.get("metron_issue_id") else f"l{e.get('locg_id')}"


def edition_title(e: dict) -> str:
    """'Fantastic Four Vol. 2: Unthinkable' — Metron numbers a TPB series by volume;
    an LOCG edition carries its own full title."""
    if e.get("display"):
        return e["display"]
    vol = str(e.get("number") or "")
    vol = vol[:-2] if vol.endswith(".0") else vol
    base = e.get("series_name") or "Trade"
    label = f"{base} Vol. {vol}" if vol else base
    return f"{label}: {e['title']}" if e.get("title") else label


def fill_collects(path=None, limit: int = 10, get=None) -> int:
    """trade_record rows from Metron with no collects yet: read their reprints."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("""SELECT tracked_series_id, key, metron_issue_id FROM trade_record
            WHERE metron_issue_id IS NOT NULL
              AND (COALESCE(collects_json, '[]') IN ('', '[]') OR collects_json NOT LIKE '%"series"%')
            LIMIT ?""", (limit,))]                                 # empty, or story titles from before 2026-10-11
    n = 0
    for r in rows:
        e = edition(r["metron_issue_id"], path, get)
        with db._connect(path) as conn:
            conn.execute("UPDATE trade_record SET collects_json = ? WHERE tracked_series_id = ? AND key = ?",
                         (json.dumps(e["reprints"]), r["tracked_series_id"], r["key"]))
        n += 1
    return n


# --- 2. which editions collect an issue -----------------------------------------------
def _base(title: str) -> str:
    return re.sub(r"\s*\(\d{4}\)\s*$", "", title or "").strip()


def issue_key(series_id: int, number: float) -> str:
    return f"{series_id}:{number:g}"


def reprints_issue(e: dict, target: dict) -> bool:
    """Does edition e reprint target {metron_issue_id?, title, year?, number}? An
    exact Metron id wins; else same name (norm_key), year within one, same number."""
    for rp in e.get("reprints") or []:
        if target.get("metron_issue_id") and rp.get("metron_issue_id") == target["metron_issue_id"]:
            return True
        if rp.get("number") == target["number"] and norm_key(rp.get("series") or "") == norm_key(_base(target["title"])):
            if not target.get("year") or not rp.get("year") or abs(int(rp["year"]) - int(target["year"])) <= 1:
                return True
    return False


def find_editions(target: dict, path=None, budget: int = DETAIL_BUDGET, search=None, issues=None, get=None) -> dict:
    """Editions that reprint target. → {candidates: [edition…], complete: bool}
    (complete=False when the budget ran out — the next pass carries on)."""
    path = path or DB_PATH
    ensure_tables(path)
    from kometa import metron_client
    search = search or (lambda q: metron_client._get("series/", name=q).get("results", []))
    issues = issues or metron_client.series_issues
    base = _base(target["title"])
    spent = [0]

    def cached_search(q):
        with db._connect(path) as conn:
            r = conn.execute("SELECT rows_json, fetched_at FROM collection_search WHERE q = ?", (q.lower(),)).fetchone()
        if r and r["fetched_at"] > (datetime.now(timezone.utc) - timedelta(days=LOOKUP_TTL_DAYS)).strftime("%Y-%m-%d"):
            return json.loads(r["rows_json"])
        spent[0] += 1
        rows = [{"id": x["id"], "series": x.get("series") or x.get("name"), "series_type": x.get("series_type"),
                 "year_began": x.get("year_began")} for x in search(q)]
        with db._connect(path) as conn:
            conn.execute("INSERT OR REPLACE INTO collection_search (q, rows_json, fetched_at) VALUES (?, ?, ?)",
                         (q.lower(), json.dumps(rows), _now()))
        return rows

    def cached_vols(sid):
        with db._connect(path) as conn:
            r = conn.execute("SELECT vols_json, fetched_at FROM collection_vols WHERE metron_series_id = ?", (sid,)).fetchone()
        if r and r["fetched_at"] > (datetime.now(timezone.utc) - timedelta(days=LOOKUP_TTL_DAYS)).strftime("%Y-%m-%d"):
            return json.loads(r["vols_json"])
        spent[0] += 1
        vols = [{"number": v.get("number"), "store_date": v.get("store_date"), "metron_issue_id": v.get("metron_issue_id")}
                for v in issues(sid)]
        with db._connect(path) as conn:
            conn.execute("INSERT OR REPLACE INTO collection_vols (metron_series_id, vols_json, fetched_at) VALUES (?, ?, ?)",
                         (sid, json.dumps(vols), _now()))
        return vols
    since = target.get("store_date") or (f"{target['year']}-01-01" if target.get("year") else None)
    def until(fmt):
        if not since:
            return None
        return f"{int(since[:4]) + (YEARS_AFTER_BIG if fmt in ('Omnibus', 'HC') else YEARS_AFTER)}-12-31"
    # the run's own name finds its collections AND every 'Event: <run>' one (contains-match)
    seen, colls = set(), []
    for q in metron_client.title_variants(base):
        for r in cached_search(q):
            fmt = _fmt(r.get("series_type"))
            if not fmt or r["id"] in seen:
                continue
            seen.add(r["id"])
            colls.append({"id": r["id"], "name": r.get("series") or r.get("name"), "format": fmt,
                          "year": r.get("year_began")})
    # collections that began around or after the issue first; an omnibus last
    if target.get("year"):
        colls = [c for c in colls if not c["year"] or c["year"] >= int(target["year"]) - 1]
    colls.sort(key=lambda c: (abs((c["year"] or 9999) - int(target.get("year") or 0)), FORMAT_RANK.get(c["format"], 9)))
    found, complete = [], True
    for c in colls:
        if spent[0] >= budget:
            complete = False
            break
        vols = cached_vols(c["id"])
        for v in vols:
            sd = v.get("store_date")
            if since and sd and (sd < since[:10] or sd > until(c["format"])):
                continue                                   # published before the issue, or long after
            if not v.get("metron_issue_id"):
                continue
            cached = _cached_edition(v["metron_issue_id"], path)
            if cached is None:
                if spent[0] >= budget:
                    complete = False
                    break
                spent[0] += 1
            e = cached or edition(v["metron_issue_id"], path, get)
            if reprints_issue(e, target):
                found.append(e)
        if not complete:
            break
    return {"candidates": found, "complete": complete}


def _cached_edition(metron_issue_id: int, path) -> dict | None:
    with db._connect(path) as conn:
        r = conn.execute("SELECT * FROM trade_reprints WHERE metron_issue_id = ?", (int(metron_issue_id),)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["reprints"] = json.loads(d.pop("reprints_json") or "[]")
    return d


def lookup(series_id: int, number: float, path=None, budget: int = DETAIL_BUDGET, **kw) -> dict:
    """find_editions for one of OUR issues, with the trade_lookup cache in front."""
    path = path or DB_PATH
    ensure_tables(path)
    key = issue_key(series_id, number)
    with db._connect(path) as conn:
        r = conn.execute("SELECT candidates_json, fetched_at FROM trade_lookup WHERE issue_key = ?", (key,)).fetchone()
        s = conn.execute("SELECT title, year_began FROM tracked_series WHERE id = ?", (series_id,)).fetchone()
        i = conn.execute("SELECT store_date, metron_issue_id FROM issue_status WHERE tracked_series_id = ? AND number = ?",
                         (series_id, number)).fetchone()
    if r and r["fetched_at"] and r["fetched_at"] > (datetime.now(timezone.utc) - timedelta(days=LOOKUP_TTL_DAYS)).strftime("%Y-%m-%d"):
        return {"candidates": json.loads(r["candidates_json"] or "[]"), "complete": True, "cached": True}
    if not s:
        return {"candidates": [], "complete": True}
    year = s["year_began"] or (int(i["store_date"][:4]) if i and i["store_date"] else None)
    target = {"title": s["title"], "year": year, "number": float(number),
              "store_date": i["store_date"] if i else None, "metron_issue_id": i["metron_issue_id"] if i else None}
    res = find_editions(target, path, budget, **kw)
    if res["complete"]:
        with db._connect(path) as conn:
            conn.execute("INSERT OR REPLACE INTO trade_lookup (issue_key, candidates_json, fetched_at) VALUES (?, ?, ?)",
                         (key, json.dumps(res["candidates"]), _now()))
    return res


# --- 2b. LOCG's 'Collecting …' line, for what Metron doesn't hold ---------------------
LOCG_PER_PASS = 5             # LOCG requests per pass (detail pages, a search, trade lists), only while LOCG is open
LOCG_RELATED = 2              # related LOCG series ('Fear Itself: Secret Avengers') whose trade lists are read
_COLLECT_AT = re.compile(r"\b(?:collect(?:s|ing|ed)?|reprint(?:s|ing)?)\b\s*:?\s*", re.I)
_GROUP = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9:'’&!.\- ]*?)\s*(?:\((?P<year>\d{4})\))?\s*#?\s*"
                    r"(?P<nums>\d+(?:\.\d+)?(?:\s*(?:[-–]|,|&|\band\b)\s*#?\s*\d+(?:\.\d+)?)*)", re.I)


def parse_collects(desc: str) -> list[dict]:
    """'Collects Fantastic Four (1998) #60-70, 500-502.' →
    [{series: 'Fantastic Four', year: 1998, number: 60.0}, …]."""
    m = _COLLECT_AT.search(desc or "")
    if not m:
        return []
    seg = re.split(r"(?<!\d)\.(?!\d)|\n", desc[m.end():], maxsplit=1)[0]
    out = []
    for g in _GROUP.finditer(seg):
        name = re.sub(r"^(?:and|&|,)\s+", "", g.group("name").strip(" ,;&"), flags=re.I).strip()
        if not name or len(name) < 2:
            continue
        nums = []
        for part in re.split(r"\s*(?:,|&|\band\b)\s*", g.group("nums"), flags=re.I):
            part = part.replace("#", "").strip()
            rng = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", part)
            if rng:
                a, b = int(rng.group(1)), int(rng.group(2))
                if 0 <= b - a <= 200:
                    nums.extend(float(n) for n in range(a, b + 1))
            elif re.match(r"^\d+(?:\.\d+)?$", part):
                nums.append(float(part))
        year = int(g.group("year")) if g.group("year") else None
        out.extend({"series": name.title() if name.isupper() else name, "year": year, "number": n} for n in nums)
    return out


def locg_editions(series_id: int, target: dict, path=None, budget: int = LOCG_PER_PASS, details=None,
                  search=None, trades_of=None) -> dict:
    """LOCG editions whose 'Collecting' line names target: the run's own trades
    first (its list fetched if never fetched), then — across series — the trades
    of related LOCG series a search for the run's name turns up ('Fear Itself:
    Secret Avengers' for Secret Avengers #12.1). → {candidates, complete};
    complete=False while LOCG is shut or the budget ran out."""
    path = path or DB_PATH
    from kometa import locg_client
    shut = details is None and locg_client.locg_paused()
    spent = [0]

    def spend() -> bool:
        if shut or spent[0] >= budget:
            return False
        spent[0] += 1
        return True

    def list_of(lid: int) -> list[dict] | None:
        key = f"locg_trades:{lid}"
        with db._connect(path) as conn:
            r = conn.execute("SELECT rows_json FROM collection_search WHERE q = ?", (key,)).fetchone()
        if r:
            return json.loads(r["rows_json"])
        if not spend():
            return None
        from kometa import sync as _sync
        rows = (trades_of or (lambda x: _sync.select_editions(_sync.get_trades_anon(x))))(lid)
        rows = [{"title": t.get("title"), "locg_id": str(t.get("locg_id") or ""), "format": t.get("format"),
                 "vol": t.get("vol"), "store_date": t.get("store_date"), "cover": t.get("cover")} for t in rows]
        with db._connect(path) as conn:
            conn.execute("INSERT OR REPLACE INTO collection_search (q, rows_json, fetched_at) VALUES (?, ?, ?)",
                         (key, json.dumps(rows), _now()))
        return rows

    def check(trades: list[dict], found: list[dict]) -> bool:
        """Read trades' Collecting lines; False when LOCG ran out before the end."""
        yr = int(target.get("year") or 0)
        trades = [t for t in trades if str(t.get("locg_id") or "").isdigit()]
        trades.sort(key=lambda t: (abs(int((t.get("store_date") or "9999")[:4]) - yr) if yr else 0,
                                   FORMAT_RANK.get(t.get("format"), 9)))
        for t in trades:
            lid = str(t["locg_id"])
            d = db.get_issue_details_cache(lid, path)
            if d is None:
                if not spend():
                    return False
                try:
                    d = (details or locg_client.get_issue_details_anon)(lid)
                except Exception as e:
                    logger.info(f"Trade fill: LOCG details for {lid} skipped: {e}")
                    return False
                db.set_issue_details_cache(lid, d, path)
            e = {"metron_issue_id": None, "locg_id": lid, "series_name": t.get("title"), "title": None,
                 "number": str(t.get("vol") or ""), "format": t.get("format") or "TPB", "store_date": t.get("store_date"),
                 "cover": t.get("cover"), "reprints": parse_collects((d or {}).get("desc") or ""), "display": t.get("title")}
            if reprints_issue(e, target) and all(ekey(x) != ekey(e) for x in found):
                found.append(e)
        return True

    found: list[dict] = []
    # 1. the run's own trades
    cached = db.get_trades(series_id, path)
    own = list(cached["trades"]) if cached else []
    with db._connect(path) as conn:
        row = conn.execute("SELECT locg_series_id FROM tracked_series WHERE id = ?", (series_id,)).fetchone()
    run_lid = row["locg_series_id"] if row else None
    if not own and run_lid:
        got = list_of(int(run_lid))
        if got is None:
            return {"candidates": found, "complete": False}
        own = got
    if not check(own, found):
        return {"candidates": found, "complete": False}
    if found:
        return {"candidates": found, "complete": True}
    # 2. across series: what LOCG calls related to the run's name
    base = _base(target["title"])
    key = f"locg_search:{norm_key(base)}"
    with db._connect(path) as conn:
        r = conn.execute("SELECT rows_json FROM collection_search WHERE q = ?", (key,)).fetchone()
    if r:
        hits = json.loads(r["rows_json"])
    else:
        if not spend():
            return {"candidates": found, "complete": False}
        try:
            hits = (search or locg_client.search_series_strict)(base)
        except Exception as e:
            logger.info(f"Trade fill: LOCG search for {base!r} skipped: {e}")
            return {"candidates": found, "complete": False}
        hits = [{"id": h.get("id"), "title": h.get("title"), "comic": bool(h.get("comic")), "year": h.get("year")} for h in hits]
        with db._connect(path) as conn:
            conn.execute("INSERT OR REPLACE INTO collection_search (q, rows_json, fetched_at) VALUES (?, ?, ?)",
                         (key, json.dumps(hits), _now()))
    want = norm_key(base)
    related = [h for h in hits if want in norm_key(h.get("title") or "") and norm_key(h.get("title") or "") != want
               and h.get("id") != run_lid]
    # an edition LOCG hands back as a comic is a trade candidate itself
    comics = [{"title": h["title"], "locg_id": str(h["id"]), "format": "TPB"} for h in related if h.get("comic")]
    if not check(comics, found):
        return {"candidates": found, "complete": False}
    for h in [h for h in related if not h.get("comic")][:LOCG_RELATED]:
        got = list_of(int(h["id"]))
        if got is None or not check(got, found):
            return {"candidates": found, "complete": False}
    return {"candidates": found, "complete": True}


# --- 3. the proposal -------------------------------------------------------------------
def _span(numbers: list[float]) -> str:
    """[67, 68, 69, 70, 500] → '#67–70, #500'."""
    nums = sorted(numbers)
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        a, b = f"{nums[i]:g}", f"{nums[j]:g}"
        out.append(f"#{a}" if i == j else f"#{a}–{b}")
        i = j + 1
    return ", ".join(out)


def choose(rows: list[dict], cands_by_row: dict[int, list[dict]], rejected: set = frozenset()) -> tuple[dict, list[dict]] | None:
    """The edition covering the most of these stuck rows; then the slimmest
    (fewest reprints); a TPB before an HC before an omnibus."""
    pool: dict[str, dict] = {}
    covers: dict[str, list[dict]] = {}
    rej = {str(x) for x in rejected}
    for r in rows:
        for e in cands_by_row.get(r["id"], []):
            k = ekey(e)
            if k in rej:
                continue
            pool[k] = e
            if r not in covers.setdefault(k, []):
                covers[k].append(r)
    if not pool:
        return None
    best = min(pool.values(), key=lambda e: (-len(covers[ekey(e)]), len(e.get("reprints") or []) or 999,
                                             FORMAT_RANK.get(e.get("format"), 9), e.get("store_date") or "9999"))
    return best, covers[ekey(best)]


def propose(owner_series_id: int, e: dict, rows: list[dict], path=None) -> int | None:
    """A 'suggested' trade row in Activity. Nothing downloads from this state."""
    from kometa.record import metron_key
    path = path or DB_PATH
    nums = [r["issue_number"] for r in rows]
    meta = {"title": e.get("series_name"), "vol": None, "vol_range": None, "cover": e.get("cover"),
            "edition_title": edition_title(e), "pack_url": None, "via": "trade_fallback",
            "fills": [r["id"] for r in rows], "fills_label": _span(nums), "fills_series": owner_series_id,
            "collects": len(e.get("reprints") or []), "format": e.get("format")}
    try:
        n = float(e.get("number") or "")
        meta["vol"] = int(n) if n == int(n) else None
    except ValueError:
        pass
    with db._connect(path) as conn:
        key = metron_key(e["metron_issue_id"]) if e.get("metron_issue_id") else str(e["locg_id"])
        cur = conn.execute("""INSERT INTO download_queue (tracked_series_id, kind, locg_id, meta_json, state)
            VALUES (?, 'trade', ?, ?, 'suggested') ON CONFLICT(tracked_series_id, locg_id) DO NOTHING""",
                           (owner_series_id, key, json.dumps(meta)))
        if not cur.rowcount:
            return None
        qid = cur.lastrowid
        for r in rows:
            conn.execute("INSERT OR REPLACE INTO trade_fallback (queue_id, state, proposal_qid, rejected_json, checked_at) "
                         "VALUES (?, 'proposed', ?, COALESCE((SELECT rejected_json FROM trade_fallback WHERE queue_id = ?), '[]'), ?)",
                         (r["id"], qid, r["id"], _now()))
    logger.info(f"Trade fill: proposed {meta['edition_title']!r} for series {owner_series_id} — fills {meta['fills_label']}")
    return qid


def stuck_rows(path=None, limit: int = ROWS_PER_TICK) -> list[dict]:
    """not_found single issues with no live proposal, oldest look first."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT q.* FROM download_queue q LEFT JOIN trade_fallback f ON f.queue_id = q.id
            WHERE q.state = 'not_found' AND COALESCE(q.kind, 'issue') = 'issue' AND q.issue_number IS NOT NULL
              AND q.issue_number >= 0
              AND (f.queue_id IS NULL OR (f.state IN ('none', 'looking') AND f.checked_at < ?))
            ORDER BY COALESCE(f.checked_at, ''), q.id LIMIT ?""",
                                                     ((datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d"), limit))]


def _target(series_id: int, number: float, path) -> dict:
    with db._connect(path) as conn:
        s = conn.execute("SELECT title, year_began FROM tracked_series WHERE id = ?", (series_id,)).fetchone()
        i = conn.execute("SELECT store_date, metron_issue_id FROM issue_status WHERE tracked_series_id = ? AND number = ?",
                         (series_id, number)).fetchone()
    year = (s["year_began"] if s else None) or (int(i["store_date"][:4]) if i and i["store_date"] else None)
    return {"title": s["title"] if s else "", "year": year, "number": float(number),
            "store_date": i["store_date"] if i else None, "metron_issue_id": i["metron_issue_id"] if i else None}


def run_pass(path=None, limit: int = ROWS_PER_TICK, budget: int = DETAIL_BUDGET, locg_details=None,
             locg_search=None, locg_trades=None, **kw) -> dict:
    """Scheduler: look up a few stuck issues, propose a trade where one exists.
    Rows of the same series are taken together so one edition can fill them all."""
    path = path or DB_PATH
    from kometa import metron_client
    out = {"looked": 0, "proposed": [], "none": 0, "stopped": None}
    first = stuck_rows(path, limit)
    if not first:
        return out
    # widen to every stuck row of the same series: Unthinkable fills #67–70 AND #500
    sids = {r["tracked_series_id"] for r in first}
    rows = [r for r in stuck_rows(path, limit=500) if r["tracked_series_id"] in sids]
    by_series: dict[int, list[dict]] = {}
    for r in rows:
        by_series.setdefault(r["tracked_series_id"], []).append(r)
    for sid, group in by_series.items():
        cands = {}
        try:
            for r in group:
                res = lookup(sid, r["issue_number"], path, budget, **kw)
                out["looked"] += 1
                if not res["complete"]:
                    _mark(r["id"], "looking", path)        # budget ran out: carry on next pass
                    continue
                got = list(res["candidates"])
                if not got:
                    # Metron doesn't hold the edition (its 2003 FF trades, say): the run's
                    # own LOCG trades and their 'Collecting' lines, while LOCG is open
                    lres = locg_editions(sid, _target(sid, r["issue_number"], path), path, details=locg_details,
                                         search=locg_search, trades_of=locg_trades)
                    got = lres["candidates"]
                    if not lres["complete"] and not got:
                        _mark(r["id"], "looking", path)    # LOCG shut or out of budget: ask again next pass
                        continue
                cands[r["id"]] = got
        except metron_client.MetronUnavailable as e:
            out["stopped"] = str(e)
            break
        while True:
            left = [r for r in group if r["id"] in cands and _state(r["id"], path) not in ("proposed",)]
            pick = choose(left, cands, _rejected(left, path))
            if not pick:
                break
            e, covered = pick
            qid = propose(sid, e, covered, path)
            if qid:
                out["proposed"].append({"qid": qid, "title": edition_title(e), "fills": _span([r["issue_number"] for r in covered]),
                                        "series_id": sid})
            else:
                for r in covered:
                    _mark(r["id"], "proposed", path)
        for r in group:
            if r["id"] in cands and _state(r["id"], path) not in ("proposed",):
                _mark(r["id"], "none", path)
                out["none"] += 1
    if out["proposed"] or out["looked"]:
        logger.info(f"Trade fill pass: {out}")
    return out


def _state(queue_id: int, path) -> str | None:
    with db._connect(path) as conn:
        r = conn.execute("SELECT state FROM trade_fallback WHERE queue_id = ?", (queue_id,)).fetchone()
    return r["state"] if r else None


def _rejected(rows: list[dict], path) -> set[int]:
    out = set()
    with db._connect(path) as conn:
        for r in rows:
            x = conn.execute("SELECT rejected_json FROM trade_fallback WHERE queue_id = ?", (r["id"],)).fetchone()
            if x and x["rejected_json"]:
                out.update(json.loads(x["rejected_json"]))
    return out


def _mark(queue_id: int, state: str, path):
    with db._connect(path) as conn:
        conn.execute("INSERT INTO trade_fallback (queue_id, state, rejected_json, checked_at) VALUES (?, ?, '[]', ?) "
                     "ON CONFLICT(queue_id) DO UPDATE SET state = excluded.state, checked_at = excluded.checked_at",
                     (queue_id, state, _now()))


def scheduled_pass() -> dict:
    try:
        return run_pass()
    except Exception as e:
        logger.warning(f"Trade fill pass failed: {e}", exc_info=True)
        return {"error": str(e)}


# --- confirm / decline -------------------------------------------------------------------
def confirm(qid: int, path=None) -> dict:
    """'Get this trade': the suggestion becomes an ordinary trade grab; the issues
    it fills leave not_found for 'via_trade'."""
    kick = path is None                       # the live app starts the worker; a test passes its own db
    path = path or DB_PATH
    with db._connect(path) as conn:
        r = conn.execute("SELECT * FROM download_queue WHERE id = ?", (qid,)).fetchone()
        if not r or r["state"] != "suggested":
            raise HTTPException(404, "Not a suggested trade")
        meta = json.loads(r["meta_json"] or "{}")
        conn.execute("UPDATE download_queue SET state = 'queued', error = NULL, updated_at = datetime('now') WHERE id = ?", (qid,))
        for fid in meta.get("fills") or []:
            conn.execute("UPDATE download_queue SET state = 'via_trade', error = ?, updated_at = datetime('now') "
                         "WHERE id = ? AND state = 'not_found'", (f"Getting it in {meta.get('edition_title')}", fid))
    _teach_owner(r["tracked_series_id"], r["locg_id"], meta, path)
    if not kick:
        return {"queued": qid, "fills": meta.get("fills_label")}
    try:
        from kometa.acquisition import _process_queue
        threading.Thread(target=_process_queue, daemon=True).start()
    except Exception:
        pass
    return {"queued": qid, "fills": meta.get("fills_label")}


def _teach_owner(series_id: int, key: str, meta: dict, path):
    """A cross-series edition ('Fear Itself: Secret Avengers' for Secret Avengers
    #12.1) isn't in the run's own trade list, so the ownership stamp would never
    look for it. Add it — to trade_record and the cached list — before it lands."""
    if not str(key).startswith("m"):
        return
    mid = int(str(key)[1:])
    e = _cached_edition(mid, path) or {}
    t = {"title": meta.get("edition_title"), "edition_title": meta.get("edition_title"), "vol": meta.get("vol"),
         "vol_range": None, "format": meta.get("format") or e.get("format") or "TPB", "cover": meta.get("cover"),
         "metron_issue_id": mid, "metron_series_id": e.get("metron_series_id"), "locg_id": key,
         "store_date": e.get("store_date"), "collects": e.get("reprints") or [], "source": "metron"}
    cached = db.get_trades(series_id, path)
    trades = list(cached["trades"]) if cached else []
    if not any(x.get("metron_issue_id") == mid for x in trades):
        trades.append(t)
        db.set_trades(series_id, trades, path)
    from kometa import record
    record.ensure_tables(path)
    with db._connect(path) as conn:
        conn.execute("""INSERT OR IGNORE INTO trade_record (tracked_series_id, key, title, vol, format, edition_title, cover,
            store_date, collects_json, metron_series_id, metron_issue_id, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'metron', ?)""",
                     (series_id, key, t["title"], t["vol"], t["format"], t["edition_title"], t["cover"], t["store_date"],
                      json.dumps(t["collects"]), t["metron_series_id"], mid, _now()))


def decline(qid: int, path=None) -> dict:
    """'Not this one': drop the suggestion, remember the edition, let the next
    pass offer the next best (or nothing)."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        r = conn.execute("SELECT * FROM download_queue WHERE id = ?", (qid,)).fetchone()
        if not r or r["state"] != "suggested":
            raise HTTPException(404, "Not a suggested trade")
        meta = json.loads(r["meta_json"] or "{}")
        lid = str(r["locg_id"])
        mid = lid[1:] if lid.startswith("m") else f"l{lid}"
        conn.execute("DELETE FROM download_queue WHERE id = ?", (qid,))
        for fid in meta.get("fills") or []:
            x = conn.execute("SELECT rejected_json FROM trade_fallback WHERE queue_id = ?", (fid,)).fetchone()
            rej = {str(v) for v in json.loads(x["rejected_json"] or "[]")} if x else set()
            rej.add(mid)
            conn.execute("INSERT INTO trade_fallback (queue_id, state, rejected_json, checked_at) VALUES (?, 'looking', ?, '2000-01-01') "
                         "ON CONFLICT(queue_id) DO UPDATE SET state = 'looking', rejected_json = excluded.rejected_json, "
                         "checked_at = '2000-01-01'", (fid, json.dumps(sorted(rej))))
    return {"declined": qid}


# --- 4. coverage: an owned trade's issues aren't missing --------------------------------
def refresh_coverage(path=None) -> int:
    """Stamp issue_status.covered_by = the owned trade's file for every issue an
    owned trade collects (exact reprint matches only); clear stale stamps.
    Returns the number of issues covered."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        series = {r["id"]: dict(r) for r in conn.execute("SELECT id, title, year_began, folder_path FROM tracked_series")}
        by_metron = {r["metron_issue_id"]: (r["tracked_series_id"], r["number"]) for r in conn.execute(
            "SELECT tracked_series_id, number, metron_issue_id FROM issue_status WHERE metron_issue_id IS NOT NULL")}
        trades = [dict(r) for r in conn.execute("SELECT tracked_series_id, data_json FROM trades_cache")]
        reprints = {r["metron_issue_id"]: json.loads(r["reprints_json"] or "[]") for r in conn.execute(
            "SELECT metron_issue_id, reprints_json FROM trade_reprints")}
    names: dict[str, list[dict]] = {}
    for s in series.values():
        names.setdefault(norm_key(_base(s["title"])), []).append(s)
    covered: dict[tuple, str] = {}
    for t in trades:
        owner = series.get(t["tracked_series_id"])
        if not owner or not owner.get("folder_path"):
            continue
        for tr in json.loads(t["data_json"] or "[]"):
            if not tr.get("owned") or not tr.get("file") or not tr.get("metron_issue_id"):
                continue
            book = os.path.join(owner["folder_path"], tr["file"])
            for rp in reprints.get(int(tr["metron_issue_id"]), []):
                hit = by_metron.get(rp.get("metron_issue_id")) if rp.get("metron_issue_id") else None
                if not hit:
                    for s in names.get(norm_key(rp.get("series") or ""), []):
                        if not s["year_began"] or not rp.get("year") or abs(s["year_began"] - rp["year"]) <= 1:
                            hit = (s["id"], rp["number"])
                            break
                if hit:
                    covered[(hit[0], float(hit[1]))] = book
    with db._connect(path) as conn:
        conn.execute("UPDATE issue_status SET covered_by = NULL WHERE covered_by IS NOT NULL")
        for (sid, n), book in covered.items():
            conn.execute("UPDATE issue_status SET covered_by = ? WHERE tracked_series_id = ? AND number = ? AND owned = 0",
                         (book, sid, n))
        # a stuck single the trade now covers is done
        for (sid, n), book in covered.items():
            conn.execute("UPDATE download_queue SET state = 'done', error = ? WHERE tracked_series_id = ? AND issue_number = ? "
                         "AND state IN ('not_found', 'via_trade', 'failed')", (f"In {os.path.basename(book)}", sid, n))
    return len(covered)


def covered_book(series_id: int, number: float, path=None) -> str | None:
    with db._connect(path or DB_PATH) as conn:
        r = conn.execute("SELECT covered_by FROM issue_status WHERE tracked_series_id = ? AND number = ?",
                         (series_id, number)).fetchone()
    return r["covered_by"] if r and r["covered_by"] else None


def trickle() -> dict:
    """Scheduler: collects for a few trades, the coverage stamps, then the pass."""
    from kometa import metron_client
    out = {}
    try:
        out["collects"] = fill_collects(limit=5)
    except metron_client.MetronUnavailable as e:
        out["collects_stopped"] = str(e)
    except Exception as e:
        logger.info(f"Trade fill: collects skipped: {e}")
    try:
        out["covered"] = refresh_coverage()
    except Exception as e:
        logger.info(f"Trade fill: coverage skipped: {e}")
    if "collects_stopped" not in out:
        out["pass"] = scheduled_pass()
    return out


# --- API ----------------------------------------------------------------------------------
@router.post("/api/queue/{qid}/get-trade")
def api_confirm(qid: int):
    return confirm(qid)


@router.post("/api/queue/{qid}/not-this-trade")
def api_decline(qid: int):
    return decline(qid)


@router.post("/api/trade-fill/run")
def api_run():
    """Run one pass now (the scheduler does this every 10 minutes)."""
    return run_pass()
