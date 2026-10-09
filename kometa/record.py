"""The local catalogue record: what Kometa knows about each series and issue,
kept here, filled from the catalogues, read by everything.

Tonight's lesson (2026-10-09): creators lived in one cache, trades in another,
covers in a third, each filled on a different trigger, each empty until
something poked it, all at the mercy of Metron's thirty-a-minute and LOCG's
cookie. One record instead. Metron fills first (ids, dates, pages, ISBNs),
LOCG second (the runs Metron hasn't got — names, no ids), and every row says
where it came from and when. Filled by a background trickle for the backlog,
picked up for new issues as sync writes them, refreshed on interaction once
it's old. Nothing asks a catalogue at request time.
"""
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
RECORD_TTL_DAYS = 90          # a full row is refreshed on interaction once this old
RECORD_MISS_TTL_DAYS = 7      # a row with nothing to ask is asked again after this
TRICKLE_LIMIT = 25            # per tick, under Metron's 30/min with the other jobs
TRICKLE_MINUTES = 10


def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS issue_record (
            tracked_series_id INTEGER NOT NULL, number REAL NOT NULL,
            desc TEXT, credits_json TEXT, arcs_json TEXT, covers_json TEXT,
            store_date TEXT, cover_date TEXT, page_count INTEGER, price TEXT, isbn TEXT,
            metron_issue_id INTEGER, locg_issue_id TEXT, cv_issue_id TEXT,
            source TEXT, fetched_at TEXT, fill_state TEXT,
            PRIMARY KEY (tracked_series_id, number))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS series_record (
            tracked_series_id INTEGER PRIMARY KEY,
            desc TEXT, publisher TEXT, year_began INTEGER, year_end INTEGER, series_type TEXT, issue_count INTEGER,
            metron_series_id INTEGER, locg_series_id INTEGER, cv_volume_id TEXT,
            source TEXT, fetched_at TEXT, fill_state TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issue_record_state ON issue_record(fill_state, fetched_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS trade_record (
            tracked_series_id INTEGER NOT NULL, key TEXT NOT NULL,
            title TEXT, vol INTEGER, vol_range_json TEXT, format TEXT, edition_title TEXT, subtitle TEXT,
            cover TEXT, store_date TEXT, page_count INTEGER, isbn TEXT, price TEXT, desc TEXT, collects_json TEXT,
            metron_series_id INTEGER, metron_issue_id INTEGER, locg_id TEXT,
            source TEXT, fetched_at TEXT,
            PRIMARY KEY (tracked_series_id, key))""")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _age_days(fetched_at: str | None) -> float:
    if not fetched_at:
        return 1e9
    try:
        return (datetime.utcnow() - datetime.strptime(fetched_at[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400
    except ValueError:
        return 1e9


def _row(r) -> dict | None:
    if not r:
        return None
    d = dict(r)
    for k in ("credits", "arcs", "covers"):
        try:
            d[k] = json.loads(d.pop(f"{k}_json") or "[]")
        except (TypeError, ValueError):
            d[k] = []
    return d


def get_issue(series_id: int, number: float, path=None) -> dict | None:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        return _row(conn.execute("SELECT * FROM issue_record WHERE tracked_series_id = ? AND number = ?",
                                 (series_id, number)).fetchone())


def issues_for_series(series_id: int, path=None) -> list[dict]:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        return [_row(r) for r in conn.execute(
            "SELECT * FROM issue_record WHERE tracked_series_id = ? ORDER BY number", (series_id,))]


def get_series(series_id: int, path=None) -> dict | None:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT * FROM series_record WHERE tracked_series_id = ?", (series_id,)).fetchone()
        return dict(r) if r else None


def write_issue(series_id: int, number: float, data: dict, source: str | None, state: str, issue: dict | None = None,
                path=None) -> dict:
    """Store what a catalogue said about one issue. data uses the issue_meta /
    metron_client.issue_detail shape: desc, credits, covers, arcs, store_date,
    cover_date, page_count, price, isbn."""
    path = path or DB_PATH
    ensure_tables(path)
    issue = issue or {}
    with db._connect(path) as conn:
        conn.execute("""INSERT OR REPLACE INTO issue_record
            (tracked_series_id, number, desc, credits_json, arcs_json, covers_json, store_date, cover_date, page_count,
             price, isbn, metron_issue_id, locg_issue_id, cv_issue_id, source, fetched_at, fill_state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (series_id, number, data.get("desc") or "", json.dumps(data.get("credits") or []),
                      json.dumps(data.get("arcs") or []), json.dumps(data.get("covers") or []),
                      data.get("store_date") or issue.get("store_date"), data.get("cover_date"), data.get("page_count"),
                      data.get("price"), data.get("isbn"), issue.get("metron_issue_id"), issue.get("locg_issue_id"),
                      issue.get("cv_issue_id"), source, _now(), state))
    return get_issue(series_id, number, path)


def _fresh(row: dict | None) -> bool:
    """True when the row answers for now: full/partial within TTL, or a miss
    younger than the miss TTL."""
    if not row:
        return False
    age = _age_days(row.get("fetched_at"))
    if row.get("fill_state") == "miss":
        return age < RECORD_MISS_TTL_DAYS
    return age < RECORD_TTL_DAYS


def fill_issue(series_id: int, number: float, path=None, force: bool = False, issue: dict | None = None,
               metron=None, locg=None, locg_open=None) -> dict | None:
    """One issue into the record. Metron when the issue carries a Metron id;
    else LOCG, only while LOCG is open to us; else a 'miss' that waits a week.
    Returns the row, or None when the only source was shut (try again later).
    Raises metron_client.MetronUnavailable so a trickle stops at the first refusal."""
    path = path or DB_PATH
    existing = get_issue(series_id, number, path)
    if existing and not force and _fresh(existing):
        return existing
    if issue is None:
        issue = next((i for i in db.get_issues_for_series(series_id, path) if i["number"] == number), None)
        if issue is None:
            return None
    from kometa import issue_meta
    if issue.get("metron_issue_id"):
        from kometa import metron_client
        found = (metron or issue_meta._metron)(issue, path)
        if found is None:
            raise metron_client.MetronUnavailable("Metron didn't answer")   # keep the row for next time
        return write_issue(series_id, number, found, "metron", "full", issue, path)
    if issue.get("locg_issue_id"):
        from kometa import locg_client
        is_open = (locg_open if locg_open is not None else (lambda: not locg_client.locg_paused()))()
        if not is_open:
            return None                                        # not a miss: the door was shut
        try:
            found = (locg or (lambda i, p: issue_meta._locg(i, p, False)))(issue, path)
        except Exception as e:
            logger.info(f"Record: LOCG details for issue {issue.get('locg_issue_id')} skipped: {e}")
            return None
        if found is None:
            return None
        return write_issue(series_id, number, found, "locg", "partial", issue, path)
    return write_issue(series_id, number, {}, None, "miss", issue, path)


def fill_series(series_id: int, path=None, force: bool = False, detail=None) -> dict | None:
    """The series' own facts: Metron's when linked, else what tracked_series holds."""
    path = path or DB_PATH
    ensure_tables(path)
    existing = get_series(series_id, path)
    if existing and not force and _age_days(existing.get("fetched_at")) < RECORD_TTL_DAYS:
        return existing
    s = db.get_series_by_id(series_id, path)
    if not s:
        return None
    facts = {"desc": None, "publisher": s.get("publisher"), "year_began": s.get("year_began"), "year_end": None,
             "series_type": s.get("metron_type"), "issue_count": None}
    source, state = ("locg" if s.get("locg_series_id") else None), ("partial" if s.get("locg_series_id") else "miss")
    if s.get("metron_series_id"):
        from kometa import metron_client
        d = (detail or metron_client.series_detail)(s["metron_series_id"])
        facts.update(publisher=d.get("publisher") or facts["publisher"], year_began=d.get("year") or facts["year_began"],
                     year_end=d.get("year_end"), series_type=d.get("type") or facts["series_type"],
                     issue_count=d.get("issue_count"), desc=d.get("desc"))
        source, state = "metron", "full"
    with db._connect(path) as conn:
        conn.execute("""INSERT OR REPLACE INTO series_record (tracked_series_id, desc, publisher, year_began, year_end,
            series_type, issue_count, metron_series_id, locg_series_id, cv_volume_id, source, fetched_at, fill_state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (series_id, facts["desc"], facts["publisher"], facts["year_began"], facts["year_end"],
                      facts["series_type"], facts["issue_count"], s.get("metron_series_id"), s.get("locg_series_id"),
                      s.get("cv_volume_id"), source, _now(), state))
    return get_series(series_id, path)


# --- the trickle: oldest-unfilled first, pull-list series first ------------------------
def pending_issues(path=None, limit: int = 200) -> list[dict]:
    """Issues with no record, or a miss older than its TTL. Pull-list series lead."""
    path = path or DB_PATH
    ensure_tables(path)
    cutoff = (datetime.utcnow() - timedelta(days=RECORD_MISS_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT i.tracked_series_id, i.number, i.metron_issue_id, i.locg_issue_id, i.store_date, i.owned
            FROM issue_status i JOIN tracked_series s ON s.id = i.tracked_series_id
            LEFT JOIN issue_record r ON r.tracked_series_id = i.tracked_series_id AND r.number = i.number
            WHERE (s.kind IS NULL OR s.kind != 'arc')
              AND (r.tracked_series_id IS NULL OR (r.fill_state = 'miss' AND r.fetched_at < ?))
            ORDER BY s.on_pull_list DESC, i.owned DESC, i.tracked_series_id, i.number
            LIMIT ?""", (cutoff, limit))]


def trickle(limit: int = TRICKLE_LIMIT, path=None) -> int:
    """Scheduler tick: fill up to `limit` issues, stop at Metron's first refusal.
    LOCG-only issues are skipped while LOCG is shut and don't count. Also fills
    a few series records so the series facts land alongside."""
    path = path or DB_PATH
    from kometa import metron_client
    done = 0
    for cand in pending_issues(path, limit=limit * 4):
        if done >= limit:
            break
        issue = dict(cand)
        try:
            row = fill_issue(cand["tracked_series_id"], cand["number"], path, issue=issue)
        except metron_client.MetronUnavailable as e:
            logger.info(f"Record trickle paused: {e}")
            break
        except Exception as e:
            logger.info(f"Record trickle skipped {cand['tracked_series_id']}#{cand['number']}: {e}")
            continue
        if row is not None and row.get("fill_state") != "miss":
            done += 1
    try:
        with db._connect(path) as conn:
            missing = [r[0] for r in conn.execute("""SELECT s.id FROM tracked_series s LEFT JOIN series_record r ON r.tracked_series_id = s.id
                WHERE (s.kind IS NULL OR s.kind != 'arc') AND r.tracked_series_id IS NULL ORDER BY s.on_pull_list DESC, s.id LIMIT 5""")]
        for sid in missing:
            fill_series(sid, path)
    except metron_client.MetronUnavailable:
        pass
    except Exception as e:
        logger.info(f"Record trickle: series facts skipped: {e}")
    return done


def backlog(path=None) -> dict:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM issue_status i JOIN tracked_series s ON s.id = i.tracked_series_id WHERE s.kind IS NULL OR s.kind != 'arc'").fetchone()[0]
        filled = conn.execute("SELECT COUNT(*) FROM issue_record WHERE fill_state != 'miss'").fetchone()[0]
        miss = conn.execute("SELECT COUNT(*) FROM issue_record WHERE fill_state = 'miss'").fetchone()[0]
    return {"issues": total, "filled": filled, "miss": miss, "pending": max(0, total - filled - miss)}


# --- the interaction path: the issue modal -------------------------------------------
def details(issue: dict, path=None) -> dict:
    """What the modal's Details tab shows, from the record. Absent → today's
    resolver answers AND fills the record; old → answer now, refresh behind."""
    path = path or DB_PATH
    sid, number = issue["tracked_series_id"], issue["number"]
    row = get_issue(sid, number, path)
    if row and row.get("fill_state") in ("full", "partial"):
        if _age_days(row.get("fetched_at")) >= RECORD_TTL_DAYS:
            threading.Thread(target=lambda: _safe_refresh(sid, number, path), daemon=True).start()
        return {"desc": row.get("desc") or "", "credits": row.get("credits") or [], "source": row.get("source"),
                "arcs": row.get("arcs") or [], "store_date": row.get("store_date"), "record": True}
    from kometa import issue_meta
    d = issue_meta.details_for(issue, path)
    if d.get("source"):
        state = "full" if d["source"].startswith("metron") else "partial"
        write_issue(sid, number, d, d["source"].split("+")[0], state, issue, path)
    return {"desc": d.get("desc", ""), "credits": d.get("credits", []), "source": d.get("source"),
            "arcs": d.get("arcs") or [], "store_date": d.get("store_date") or issue.get("store_date"), "record": False}


def _safe_refresh(sid: int, number: float, path):
    try:
        fill_issue(sid, number, path, force=True)
    except Exception as e:
        logger.info(f"Record refresh for {sid}#{number} skipped: {e}")


# --- trades: collected editions, Metron first, LOCG second --------------------------
# Metron files a run's collected editions as SIBLING series ('East of West TPB
# (2013)', type Trade Paperback) whose issues are the volumes. LOCG lists them
# under the run itself, by name only. Both land here as one list per series,
# written to trades_cache in the shape the Trades tab, the shelf page, tidy and
# refresh_trades_owned already read — so none of them change.
COLLECTED_TYPES = {"Trade Paperback": "TPB", "Hardcover": "HC", "Omnibus": "Omnibus", "Graphic Novel": "GN"}
TRADE_DETAIL_BUDGET = 20        # issue details fetched per fill, for subtitle / pages / ISBN
TRADES_TRICKLE_LIMIT = 5
_FORMAT_SUFFIX_RE = re.compile(r"\s+(?:tpb|tp|hc|omnibus|gn|ogn|compendium|deluxe(?: edition)?|library edition|"
                               r"absolute|hardcover|trade paperback|collected edition|book)$", re.I)


def metron_key(metron_issue_id: int) -> str:
    """The queue/tab key for a Metron-sourced trade. The Trades tab, the download
    endpoint and download_queue all key on a TEXT locg_id; LOCG's are digits,
    the pack and proposal sentinels are -1 / -2, so 'm<id>' can collide with none."""
    return f"m{int(metron_issue_id)}"


def _base_name(name: str) -> str:
    t = re.sub(r"\s*\(\d{4}\)\s*$", "", name or "").strip()
    prev = None
    while prev != t:
        prev, t = t, _FORMAT_SUFFIX_RE.sub("", t).strip(" :-")
    return t


def collected_siblings(series: dict, search=None) -> list[dict]:
    """Metron series that are this run's collected editions: same base name,
    a collected type, begun no earlier than the year before the run."""
    from kometa import metron_client
    from kometa.naming import norm_key
    want = norm_key(_base_name(series["title"]))
    year = series.get("year_began")
    raw = search or (lambda q: metron_client._get("series/", name=q).get("results", []))
    out, seen = [], set()
    for q in metron_client.title_variants(_base_name(series["title"])):
        for r in raw(q):
            st = r.get("series_type")
            fmt = COLLECTED_TYPES.get(st.get("name") if isinstance(st, dict) else st)
            if not fmt or r["id"] in seen:
                continue
            if norm_key(_base_name(r.get("series") or r.get("name") or "")) != want:
                continue
            if year and r.get("year_began") and r["year_began"] < year - 1:
                continue
            seen.add(r["id"])
            out.append({"id": r["id"], "name": r.get("series") or r.get("name"), "format": fmt,
                        "year": r.get("year_began"), "issue_count": r.get("issue_count")})
    return out


def _metron_trades(series: dict, search=None, issues=None, detail=None, budget: int = TRADE_DETAIL_BUDGET) -> list[dict]:
    from kometa import metron_client
    issues = issues or metron_client.series_issues
    detail = detail or (lambda iid: metron_client._get(f"issue/{int(iid)}/"))
    base = _base_name(series["title"])
    out, looked = [], 0
    for sib in collected_siblings(series, search):
        for i in issues(sib["id"]):
            n = i.get("number")
            vol = int(n) if n is not None and float(n) == int(n) else None
            t = {"format": sib["format"], "vol": vol, "vol_range": None, "is_variant": False,
                 "cover": i.get("image"), "store_date": i.get("store_date"),
                 "metron_series_id": sib["id"], "metron_issue_id": i.get("metron_issue_id"),
                 "locg_id": metron_key(i["metron_issue_id"]) if i.get("metron_issue_id") else None,
                 "subtitle": None, "page_count": None, "isbn": None, "price": None, "desc": None, "collects": [],
                 "source": "metron"}
            if i.get("metron_issue_id") and looked < budget:
                looked += 1
                try:
                    d = detail(i["metron_issue_id"])
                    t["subtitle"] = (d.get("title") or "").strip() or None
                    t["page_count"] = d.get("page")
                    t["isbn"] = d.get("isbn") or None
                    t["price"] = d.get("price")
                    t["desc"] = d.get("desc") or None
                    t["collects"] = [x for x in (d.get("name") or []) if isinstance(x, str)]
                except metron_client.MetronUnavailable:
                    raise
                except Exception as e:
                    logger.info(f"Trade detail for {base!r} vol {vol} skipped: {e}")
            label = f"{base} Vol. {vol}" if vol is not None else base
            t["title"] = f"{label}: {t['subtitle']} {sib['format']}" if t["subtitle"] else f"{label} {sib['format']}"
            t["edition_title"] = t["title"]
            out.append(t)
    return out


def _merge_locg(trades: list[dict], locg: list[dict]) -> list[dict]:
    """An edition both catalogues know keeps the Metron row and gains the LOCG id;
    the rest of LOCG's list is appended as is."""
    by = {(t.get("vol"), t.get("format")): t for t in trades if t.get("vol") is not None}
    for l in locg:
        hit = by.get((l.get("vol"), l.get("format"))) if l.get("vol") is not None else None
        if hit:
            hit["locg_trade_id"] = l.get("locg_id")
            hit["cover"] = hit.get("cover") or l.get("cover")
        else:
            trades.append(dict(l, source="locg"))
    return trades


def fill_trades(series, force: bool = False, path=None, books=None, search=None, issues=None, detail=None,
                locg_fetch=None, locg_open=None, enrich=None) -> list[dict]:
    """The series' collected editions into trade_record AND trades_cache (enriched
    with owned/file, the two stored facts). Metron first; LOCG only when open.
    Raises MetronUnavailable so a trickle can stop; any LOCG trouble is logged."""
    path = path or DB_PATH
    ensure_tables(path)
    if isinstance(series, int):
        series = db.get_series_by_id(series, path)
    if not series:
        return []
    from kometa import locg_client
    trades: list[dict] = []
    if series.get("metron_series_id"):
        trades = _metron_trades(series, search=search, issues=issues, detail=detail)
    locg_id = series.get("locg_series_id")
    if locg_id:
        is_open = (locg_open if locg_open is not None else (lambda: not locg_client.locg_paused()))()
        if is_open:
            try:
                # through sync's names, not locg_client's: that's the seam the LOCG
                # budget tests (and any caller counting LOCG traffic) hook
                from kometa import sync as _sync
                fetch = locg_fetch or (lambda lid: _sync.select_editions(_sync.get_trades_anon(lid)))
                trades = _merge_locg(trades, fetch(locg_id))
            except Exception as e:
                logger.info(f"Trades: LOCG skipped for {series.get('title')!r}: {e}")
    if not trades and not force:
        cached = db.get_trades(series["id"], path)
        if cached and cached["trades"]:
            return cached["trades"]
    from kometa.sync import enrich_trades
    (enrich or enrich_trades)(series, trades, books=books)
    with db._connect(path) as conn:
        for t in trades:
            key = metron_key(t["metron_issue_id"]) if t.get("metron_issue_id") else f"l:{t.get('locg_id')}"
            conn.execute("""INSERT OR REPLACE INTO trade_record (tracked_series_id, key, title, vol, vol_range_json, format,
                edition_title, subtitle, cover, store_date, page_count, isbn, price, desc, collects_json,
                metron_series_id, metron_issue_id, locg_id, source, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (series["id"], key, t.get("title"), t.get("vol"), json.dumps(t.get("vol_range")), t.get("format"),
                          t.get("edition_title"), t.get("subtitle"), t.get("cover"), t.get("store_date"), t.get("page_count"),
                          t.get("isbn"), t.get("price"), t.get("desc"), json.dumps(t.get("collects") or []),
                          t.get("metron_series_id"), t.get("metron_issue_id"),
                          str(t.get("locg_trade_id") or (t.get("locg_id") if t.get("source") == "locg" else "") or "") or None,
                          t.get("source") or "locg", _now()))
    db.set_trades(series["id"], trades, path)
    return trades


def trade_details(key: str, path=None) -> dict | None:
    """The trade modal's Details for a Metron-sourced edition: description and
    what it collects, from the record. None for an LOCG key (the live scrape)."""
    path = path or DB_PATH
    if not str(key).startswith("m"):
        return None
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT * FROM trade_record WHERE key = ? LIMIT 1", (key,)).fetchone()
    if not r:
        return {"desc": "", "credits": [], "source": "metron"}
    collects = json.loads(r["collects_json"] or "[]")
    desc = r["desc"] or ""
    if collects:
        desc = (desc + "\n\n" if desc else "") + "Collects: " + "; ".join(collects)
    bits = [b for b in (f"{r['page_count']} pages" if r["page_count"] else None, f"ISBN {r['isbn']}" if r["isbn"] else None,
                        f"Released {r['store_date']}" if r["store_date"] else None) if b]
    if bits:
        desc = (desc + "\n\n" if desc else "") + " · ".join(bits)
    return {"desc": desc, "credits": [], "source": "metron", "store_date": r["store_date"]}


def pending_trade_series(path=None, limit: int = 50) -> list[dict]:
    """Series with a catalogue id and no trades list yet, pull list first."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT s.* FROM tracked_series s LEFT JOIN trades_cache c ON c.tracked_series_id = s.id
            WHERE (s.kind IS NULL OR s.kind != 'arc') AND c.tracked_series_id IS NULL
              AND (s.metron_series_id IS NOT NULL OR s.locg_series_id IS NOT NULL)
            ORDER BY s.on_pull_list DESC, s.id LIMIT ?""", (limit,))]


def trades_trickle(limit: int = TRADES_TRICKLE_LIMIT, path=None) -> int:
    """Scheduler tick: a few series' trade lists, stopping at Metron's first refusal."""
    path = path or DB_PATH
    from kometa import metron_client
    done = 0
    for s in pending_trade_series(path, limit=limit * 3):
        if done >= limit:
            break
        try:
            fill_trades(s, path=path)
            done += 1
        except metron_client.MetronUnavailable as e:
            logger.info(f"Trades trickle paused: {e}")
            break
        except Exception as e:
            logger.info(f"Trades trickle skipped {s.get('title')!r}: {e}")
    return done
