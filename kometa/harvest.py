"""The library harvest at a glance (docs/library-enrichment-spec.md, 1b/1c).

The harvest isn't one job: it's the trickles that were already there — the
record fill (Metron, LOCG when open), the LOCG variant sweep, the LOCG top-up
queue, the image store. Each keeps its own pace and its own refusals. This is
the one place that adds them up, so 'how far along are we' is a single read
instead of five log greps.
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

from fastapi import APIRouter

import kometa.db as db

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
_IMAGES_TTL_S = 120
_images_cache = {"at": 0.0, "n": None}


def _owned_record(path) -> dict:
    with db._connect(path) as conn:
        rows = conn.execute("""
            SELECT
              SUM(CASE WHEN r.source IN ('metron', 'locg', 'gcd') AND r.fill_state != 'miss' THEN 1 ELSE 0 END) AS catalogue,
              SUM(CASE WHEN r.source IN ('comicinfo', 'komga') THEN 1 ELSE 0 END) AS stopgap,
              SUM(CASE WHEN r.tracked_series_id IS NULL OR r.fill_state = 'miss' THEN 1 ELSE 0 END) AS no_record,
              SUM(CASE WHEN i.metron_issue_id IS NULL AND (i.locg_issue_id IS NULL OR i.locg_issue_id = '') THEN 1 ELSE 0 END) AS no_catalogue_id,
              SUM(CASE WHEN r.variants_at IS NOT NULL THEN 1 ELSE 0 END) AS variant_lists,
              SUM(CASE WHEN r.community_at IS NOT NULL THEN 1 ELSE 0 END) AS community,
              COUNT(*) AS owned
            FROM issue_status i JOIN tracked_series s ON s.id = i.tracked_series_id
            LEFT JOIN issue_record r ON r.tracked_series_id = i.tracked_series_id AND r.number = i.number
            WHERE i.owned = 1 AND (s.kind IS NULL OR s.kind != 'arc')""").fetchone()
    return {k: rows[k] or 0 for k in rows.keys()}


def _images_pending(path) -> int | None:
    """The image store's queue is computed, not stored — cached briefly, it walks
    every variant list the record knows."""
    if time.time() - _images_cache["at"] < _IMAGES_TTL_S:
        return _images_cache["n"]
    try:
        from kometa import images
        n = len(images.pending(path, limit=100_000))
    except Exception:
        n = None
    _images_cache.update(at=time.time(), n=n)
    return n


def progress(path=None) -> dict:
    path = path or DB_PATH
    from kometa import metron_client, record, topup
    out = {"owned_issues": _owned_record(path), "record": record.backlog(path),
           "metron": metron_client.budget(), "images_pending": _images_pending(path)}
    try:
        out["locg"] = topup.status(path)
    except Exception:
        out["locg"] = None
    try:
        out["gcd"] = gcd_status(path)
    except Exception:
        out["gcd"] = None
    # the polite extras (1i): each its own module, throttle and Settings toggle
    from kometa import cbr, fandom_covers, hardcover, marveldc, openlibrary, prh
    for name, mod in (("prh", prh), ("openlibrary", openlibrary), ("cbr", cbr),
                      ("fandom", fandom_covers), ("hardcover", hardcover), ("marveldc", marveldc)):
        try:
            out[name] = mod.status(path)
        except Exception:
            out[name] = None
    return out


@router.get("/api/harvest/progress")
def get_progress():
    return progress()


# --- issue ids for runs matched after their last sync (1d) -----------------------
IDS_PER_TICK = 3


def unfetched_series(path=None, limit: int = IDS_PER_TICK) -> list[dict]:
    """Runs matched to Metron whose issue list was never fetched — linked after
    their last sync, then left for the weekly not-pulled cycle (2026-10-12: 27
    runs, 517 owned issues, Hellblazer among them, with no issue ids at all).
    Most owned issues first."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        ids = [r[0] for r in conn.execute("""
            SELECT s.id FROM tracked_series s JOIN issue_status i ON i.tracked_series_id = s.id
            WHERE s.metron_series_id IS NOT NULL AND s.metron_fetched_at IS NULL
              AND (s.kind IS NULL OR s.kind != 'arc') AND i.owned = 1
            GROUP BY s.id ORDER BY COUNT(*) DESC, s.id LIMIT ?""", (limit,))]
    return [s for s in (db.get_series_by_id(i, path) for i in ids) if s]


def issue_ids_tick(limit: int = IDS_PER_TICK, path=None, sync=None) -> int:
    """Scheduler tick (harvest traffic): sync a few of those runs through the one
    sync path — its Metron list carries the ids, its folder scan keeps ownership
    honest. Stops at Metron's first refusal (the harvest floor included)."""
    from kometa import metron_client
    from kometa.sync import sync_one_guarded
    done = 0
    for s in unfetched_series(path, limit):
        if not metron_client.configured() or metron_client.harvest_share_spent():
            break
        (sync or sync_one_guarded)(s)
        done += 1
    return done


# --- runs that may span more than one Metron series (db.metron_series_ids) -------
def split_candidates(path=None, min_unmatched: int = 25, min_outside: int = 3) -> dict:
    """Local data only, no catalogue calls. 'outside': matched, but owned issues
    run past the Metron run's numbers (Fables #150–162 beyond a run that ends at
    #149) — a continuation under another Metron series, or a wrong match.
    'unmatched': no Metron run and a long one on the shelf (Fantastic Four
    #61–611). Suggestions for a person to confirm; nothing is linked from here."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT s.id, s.title, s.publisher, s.metron_series_id, s.metron_extra_ids,
                   SUM(i.owned) AS owned,
                   SUM(CASE WHEN i.owned = 1 AND i.metron_issue_id IS NULL THEN 1 ELSE 0 END) AS owned_no_id,
                   MIN(CASE WHEN i.owned = 1 THEN i.number END) AS owned_lo, MAX(CASE WHEN i.owned = 1 THEN i.number END) AS owned_hi,
                   MIN(CASE WHEN i.metron_issue_id IS NOT NULL THEN i.number END) AS metron_lo,
                   MAX(CASE WHEN i.metron_issue_id IS NOT NULL THEN i.number END) AS metron_hi
            FROM tracked_series s JOIN issue_status i ON i.tracked_series_id = s.id
            WHERE s.kind IS NULL OR s.kind != 'arc'
            GROUP BY s.id HAVING owned > 0""")]
    outside = [r for r in rows if r["metron_series_id"] and r["metron_lo"] is not None and r["owned_no_id"] >= min_outside
               and (r["owned_lo"] < r["metron_lo"] or r["owned_hi"] > r["metron_hi"])]
    unmatched = [r for r in rows if not r["metron_series_id"] and r["owned"] >= min_unmatched]
    return {"outside": sorted(outside, key=lambda r: -r["owned_no_id"]),
            "unmatched": sorted(unmatched, key=lambda r: -r["owned"])}


@router.get("/api/harvest/split-candidates")
def get_split_candidates():
    return split_candidates()


# --- ComicVine's full-size covers (1f) ----------------------------------------------
CV_BATCH = 100


def cv_cover_candidates(path=None, limit: int = CV_BATCH) -> list[dict]:
    """Owned issues whose record carries a ComicVine id (from Metron) and that
    ComicVine hasn't been asked about yet. Pull list first, then the rest."""
    path = path or DB_PATH
    from kometa import record
    record.ensure_tables(path)
    with db._connect(path) as conn:
        rows = conn.execute("""
            SELECT r.tracked_series_id AS sid, r.number, r.cv_issue_id, r.extra_json
            FROM issue_record r JOIN issue_status i ON i.tracked_series_id = r.tracked_series_id AND i.number = r.number
            JOIN tracked_series s ON s.id = r.tracked_series_id
            WHERE i.owned = 1 AND r.cv_issue_id IS NOT NULL AND r.cv_issue_id != '' AND r.extra_json IS NOT NULL
              AND (s.kind IS NULL OR s.kind != 'arc')
            ORDER BY s.on_pull_list DESC, r.tracked_series_id, r.number""").fetchall()
    out = []
    for r in rows:
        extra = json.loads(r["extra_json"] or "{}")
        if "cv_original" in extra:              # asked already (None = ComicVine had no cover)
            continue
        out.append({"sid": r["sid"], "number": r["number"], "cv_id": str(r["cv_issue_id"])})
        if len(out) >= limit:
            break
    return out


def cv_covers_tick(limit: int = CV_BATCH, path=None, client=None) -> int:
    """Scheduler tick: ONE ComicVine request for up to 100 owned issues' original
    cover URLs, into the record's extra; the image store's trickle downloads them
    (key issue/<sid>/<n>/cv). Nothing on screen changes: an owned issue's tile is
    its own file's page 1 — these are for the archive, and for issues not owned."""
    path = path or DB_PATH
    cfg = db.get_config(path)
    if cfg.get("comicvine_enabled", "1") == "0" or not (cfg.get("cv_api_key") or "").strip():
        return 0
    todo = cv_cover_candidates(path, limit)
    if not todo:
        return 0
    from kometa import record
    if client is None:
        from kometa.comicvine_client import ComicVineClient
        client = ComicVineClient(cfg["cv_api_key"].strip())
    try:
        found = client.get_issue_originals([t["cv_id"] for t in todo])
    except Exception as e:
        logger.info(f"ComicVine covers: stopped this tick: {e}")
        return 0
    for t in todo:
        record.merge_extra(t["sid"], t["number"], {"cv_original": found.get(t["cv_id"])}, path)
    logger.info(f"ComicVine covers: {len(found)} of {len(todo)} issues have an original")
    return len(found)


# --- the Grand Comics Database: the back catalogue Metron hasn't got (1g) ---------
GCD_LINK_PER_TICK = 3
GCD_FILL_BUDGET = 12                # GCD requests per tick (series lists + issues): ~1,700 a day at 10 min
GCD_SERIES_TTL_DAYS = 30            # a run's issue list is asked again after this
GCD_CANDIDATES_KEPT = 8
_YEAR_RE = re.compile(r"\((\d{4})\)")


@db.once_per_db
def ensure_gcd_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        # one fetch of a run's issue list serves every owned issue in it for a month
        conn.execute("""CREATE TABLE IF NOT EXISTS gcd_series_cache (
            gcd_series_id INTEGER PRIMARY KEY, detail_json TEXT, fetched_at TEXT)""")


def _gcd_on(path) -> bool:
    cfg = db.get_config(path)
    return cfg.get("gcd_enabled", "1") != "0" and bool((cfg.get("gcd_user") or "").strip() and cfg.get("gcd_pass"))


def _gcd_name(name: str) -> str:
    """The comparison key: norm_key, without a leading 'The' or a trailing ', The'."""
    from kometa.naming import norm_key
    raw = _YEAR_RE.sub("", name or "").strip()
    trailing_the = bool(re.search(r",\s*the$", raw, re.I))
    k = norm_key(raw)
    if trailing_the and k.endswith(" the"):
        k = k[:-4].strip()
    return re.sub(r"^the ", "", k)


def _gcd_queries(title: str) -> list[str]:
    """What to search GCD for: the folder title without its '(2011)' and leading
    'The' (GCD's name search is a contains match, so 'Adventures of Tintin' finds
    'The Adventures of Tintin'), with a folder's '- ' put back to ': ' first —
    'Dune- House Atreides' is 'Dune: House Atreides' everywhere but a filesystem."""
    base = re.sub(r"^\s*the\s+", "", _YEAR_RE.sub("", title or ""), flags=re.I).strip(" -")
    colon = re.sub(r"\s*-\s+", ": ", base)
    return [q for q in dict.fromkeys([colon, base]) if q]


def _year_hint(s: dict) -> int | None:
    import os
    for text in (s.get("title") or "", os.path.basename((s.get("folder_path") or "").rstrip("/"))):
        m = _YEAR_RE.search(text)
        if m:
            return int(m.group(1))
    return None


# words a publisher's name carries that say nothing about which publisher it is
_PUB_NOISE = {"comics", "comic", "publishing", "publications", "entertainment", "press", "books", "studios",
              "studio", "inc", "ltd", "llc", "group", "the", "and", "media", "editions", "edition", "co"}


def _pub_words(name: str | None) -> set[str]:
    from kometa.naming import norm_key
    return {w for w in norm_key(name or "").split() if w not in _PUB_NOISE and len(w) > 1}


def _file_year(series_id: int, path) -> int | None:
    """The earliest plausible year in the run's own file names. Weak on purpose:
    a pack can stamp one year on everything (Uncanny X-Men's '(2009)')."""
    with db._connect(path) as conn:
        names = [r[0] for r in conn.execute("SELECT path FROM books WHERE tracked_series_id = ?", (series_id,))]
    years = [int(y) for p in names for y in re.findall(r"\((\d{4})\)", os.path.basename(p)) if 1930 <= int(y) <= 2100]
    return min(years) if years else None


def gcd_tiebreak(s: dict, cands: list[dict], path, client) -> int | None:
    """When more than one GCD run fits, use what the shelf already says: the
    publisher folder the run lives in, then the years in its file names. Only
    narrows — a link happens only if exactly ONE candidate survives every check."""
    pool = [c for c in cands if c.get("language") == "en"] or cands
    folder_pub = (s.get("folder_path") or "").replace("\\", "/").split("/")
    folder_pub = folder_pub[2] if len(folder_pub) > 3 else s.get("publisher")
    want = _pub_words(folder_pub) | _pub_words(s.get("publisher"))
    if want:
        named = []
        for c in pool:
            pid = c.get("publisher_id")
            if pid and _pub_words(client.publisher_name(pid)) & want:
                named.append(c)
        if named:
            pool = named
    if len(pool) > 1:
        year = _file_year(s["id"], path)
        if year:
            fits = [c for c in pool if _year_fits(c, year)]
            if fits:
                pool = fits
    return pool[0]["gcd_id"] if len(pool) == 1 else None


def _year_fits(c: dict, year: int | None) -> bool:
    if year is None:
        return True
    began = c.get("year_began")
    if not began:
        return False
    return began - 1 <= year <= (c.get("year_ended") or began) + 1


def gcd_match(s: dict, hi: float | None, results: list[dict]) -> tuple[str, int | None, list[dict]]:
    """('linked', id, []) only for exactly ONE candidate with the same name
    (allowing a leading 'The'), in English, with at least as many issues as the
    highest number we own (when that's a sensible whole number) and a start year
    that fits the folder's '(YYYY)' if it has one. More than one such → 'ambiguous'
    with them; none such but a same-name run exists (another language, too
    short, another year) → 'ambiguous' with those, for a person to judge.
    Nothing of that name → 'none'. Never a guess."""
    want, year = _gcd_name(s.get("title")), _year_hint(s)
    named = [c for c in results if c.get("gcd_id") and _gcd_name(c.get("name")) == want]
    need = int(hi) if hi is not None and hi == int(hi) and 1 <= hi <= 1500 else None
    plausible = [c for c in named if c.get("language") == "en" and _year_fits(c, year)
                 and (need is None or (c.get("issues") or 0) >= need)]
    if len(plausible) == 1:
        return "linked", plausible[0]["gcd_id"], []
    pool = plausible or named
    if not pool:
        return "none", None, []
    pool = sorted(pool, key=lambda c: (c.get("language") != "en",
                                       abs((c.get("year_began") or 0) - (year or c.get("year_began") or 0)),
                                       -(c.get("issues") or 0)))
    keep = ("gcd_id", "name", "year_began", "year_ended", "issues", "language", "country", "publishing_format", "publisher_id")
    return "ambiguous", None, [{k: c.get(k) for k in keep} for c in pool[:GCD_CANDIDATES_KEPT]]


def gcd_link_candidates(path=None, limit: int = GCD_LINK_PER_TICK) -> list[dict]:
    """Owned runs Metron has no series for and GCD hasn't been asked about.
    Most owned issues first. A run you've said has no run at all (the 'none'
    button) isn't asked."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT s.id, MAX(i.number) AS hi FROM tracked_series s JOIN issue_status i ON i.tracked_series_id = s.id
            WHERE s.metron_series_id IS NULL AND s.gcd_link IS NULL AND (s.kind IS NULL OR s.kind != 'arc')
              AND i.owned = 1 AND COALESCE(s.match_status, '') != 'pending'   -- the Metron matcher goes first
              AND NOT (COALESCE(s.match_status, '') = 'manual' AND COALESCE(s.metron_link, '') = 'none'
                       AND COALESCE(s.locg_link, '') = 'none' AND s.locg_series_id IS NULL)
            GROUP BY s.id ORDER BY COUNT(*) DESC, s.id LIMIT ?""", (limit,))]
    out = []
    for r in rows:
        s = db.get_series_by_id(r["id"], path)
        if s:
            s["_owned_hi"] = r["hi"]
            out.append(s)
    return out


def gcd_link_tick(limit: int = GCD_LINK_PER_TICK, path=None, client=None) -> int:
    """Scheduler tick: a few Metron-less runs searched on GCD by title, linked
    only on the conservative rule (gcd_match). Stops at GCD's first refusal or
    the day's cap, leaving the run unasked for next time."""
    path = path or DB_PATH
    if not _gcd_on(path):
        return 0
    if client is None:
        from kometa import gcd_client as client
    from kometa.gcd_client import GCDUnavailable
    linked = 0
    # runs left 'ambiguous' get the shelf's tie-breaks again (cheap: publisher names are
    # looked up once, file years are local) — a new file or a newer rule can settle them
    with db._connect(path) as conn:
        waiting = [r[0] for r in conn.execute("SELECT id FROM tracked_series WHERE gcd_link = 'ambiguous'")]
    for sid in waiting:
        s = db.get_series_by_id(sid, path)
        cands = json.loads((s or {}).get("gcd_candidates_json") or "[]")
        if len(cands) < 2:
            continue
        try:
            gid = gcd_tiebreak(s, cands, path, client)
        except GCDUnavailable as e:
            logger.info(f"GCD tie-break stopped this tick: {e}")
            return linked
        if gid:
            db.set_gcd_link(sid, "linked", gid, [], path)
            logger.info(f"GCD: {s['title']!r} → linked {gid} (tie-break)")
            linked += 1
    for s in gcd_link_candidates(path, limit):
        try:
            results = []
            for q in _gcd_queries(s["title"]):
                results = client.search_series(q)
                if results:
                    break
        except GCDUnavailable as e:
            logger.info(f"GCD linking stopped this tick: {e}")
            break
        outcome, gid, cands = gcd_match(s, s.get("_owned_hi"), results)
        if outcome == "ambiguous" and len(cands) > 1:
            try:
                gid = gcd_tiebreak(s, cands, path, client)
            except GCDUnavailable as e:
                logger.info(f"GCD linking stopped this tick: {e}")
                break
            if gid:
                outcome, cands = "linked", []
        db.set_gcd_link(s["id"], outcome, gid, cands, path)
        logger.info(f"GCD: {s['title']!r} → {outcome}{f' {gid}' if gid else ''}")
        linked += outcome == "linked"
    return linked


def _gcd_series(gid: int, path, client, spend) -> dict | None:
    """The run's issue list, from the month-long cache or one request."""
    ensure_gcd_tables(path)
    from kometa.record import _age_days, _now
    with db._connect(path) as conn:
        r = conn.execute("SELECT detail_json, fetched_at FROM gcd_series_cache WHERE gcd_series_id = ?", (gid,)).fetchone()
    if r and _age_days(r["fetched_at"]) < GCD_SERIES_TTL_DAYS:
        return json.loads(r["detail_json"] or "null")
    if not spend():
        return None
    d = client.series_detail(gid)
    with db._connect(path) as conn:
        conn.execute("INSERT OR REPLACE INTO gcd_series_cache (gcd_series_id, detail_json, fetched_at) VALUES (?, ?, ?)",
                     (gid, json.dumps(d), _now()))
    return d


def gcd_fill_candidates(path=None) -> list[dict]:
    """Owned issues in GCD-linked runs that Metron has no id for, whose record is
    missing, a miss, covers-only, or a stopgap (the file's own tags). A GCD row
    is done — a GCD miss is asked again after the record's miss TTL."""
    path = path or DB_PATH
    from kometa import record
    record.ensure_tables(path)
    cutoff = (datetime.utcnow() - timedelta(days=record.RECORD_MISS_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT i.tracked_series_id, i.number, i.metron_issue_id, i.locg_issue_id, i.store_date, i.owned,
                   s.gcd_series_id
            FROM issue_status i JOIN tracked_series s ON s.id = i.tracked_series_id
            LEFT JOIN issue_record r ON r.tracked_series_id = i.tracked_series_id AND r.number = i.number
            WHERE s.gcd_link = 'linked' AND s.gcd_series_id IS NOT NULL AND i.owned = 1 AND i.metron_issue_id IS NULL
              AND (s.kind IS NULL OR s.kind != 'arc')
              AND (r.tracked_series_id IS NULL OR r.fill_state IN ('miss', 'variants') OR r.source IN ('comicinfo', 'komga'))
              AND NOT (COALESCE(r.source, '') = 'gcd' AND r.fetched_at >= ?)
            ORDER BY s.on_pull_list DESC, i.tracked_series_id, i.number""", (cutoff,))]


def gcd_fill_tick(budget: int = GCD_FILL_BUDGET, path=None, client=None) -> int:
    """Scheduler tick: GCD's issue detail (credits, story synopses, cast, dates,
    price, cover) for owned issues Metron can't answer, into the record as
    source 'gcd'. budget counts every GCD request, the run's list included.
    Metron takes the row back the moment the issue gains a Metron id
    (record.pending_issues)."""
    path = path or DB_PATH
    if not _gcd_on(path):
        return 0
    if client is None:
        from kometa import gcd_client as client
    from kometa import record
    from kometa.gcd_client import GCDUnavailable
    left = [budget]

    def spend() -> bool:
        if left[0] <= 0:
            return False
        left[0] -= 1
        return True

    filled, maps = 0, {}
    try:
        for c in gcd_fill_candidates(path):
            gid = c["gcd_series_id"]
            if gid not in maps:
                d = _gcd_series(gid, path, client, spend)
                if d is None and left[0] <= 0:
                    break                                   # out of requests before the list came
                maps[gid] = {}
                for it in (d or {}).get("issues_list") or []:
                    if it.get("number") is not None and it.get("gcd_issue_id"):
                        maps[gid].setdefault(float(it["number"]), it["gcd_issue_id"])
            iid = maps[gid].get(float(c["number"]))
            if not iid:
                continue                                    # GCD's run hasn't this number: costs nothing
            if not spend():
                break
            found = client.issue_detail(iid)
            sid, n = c["tracked_series_id"], c["number"]
            issue = {k: c[k] for k in ("metron_issue_id", "locg_issue_id", "store_date")}
            row = record.get_issue(sid, n, path)
            if row and row.get("source") in ("metron", "locg") and row.get("fill_state") in ("full", "partial"):
                continue                                    # a catalogue answered while we waited
            if found is None:
                record.write_issue(sid, n, {}, "gcd", "miss", issue, path)
                continue
            record.write_issue(sid, n, found, "gcd", "partial", issue, path)
            filled += 1
    except GCDUnavailable as e:
        logger.info(f"GCD fill stopped this tick: {e}")
    if filled:
        logger.info(f"GCD: {filled} issues filled")
    return filled


def gcd_status(path=None) -> dict:
    path = path or DB_PATH
    from kometa import record
    record.ensure_tables(path)
    with db._connect(path) as conn:
        links = {r[0]: r[1] for r in conn.execute(
            "SELECT gcd_link, COUNT(*) FROM tracked_series WHERE gcd_link IS NOT NULL GROUP BY gcd_link")}
        filled = conn.execute("SELECT COUNT(*) FROM issue_record WHERE source = 'gcd' AND fill_state != 'miss'").fetchone()[0]
    out = {"linked": links.get("linked", 0), "ambiguous": links.get("ambiguous", 0), "none": links.get("none", 0),
           "filled": filled}
    try:
        from kometa import gcd_client
        out["budget"] = gcd_client.budget()
    except Exception:
        out["budget"] = None
    return out


def gcd_candidates(path=None) -> list[dict]:
    """Runs GCD couldn't place on its own, with what it offered — for a person to
    pick (PATCH /api/series/{id}/locg {"gcd_id": N}, or 0 for 'not on GCD')."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT s.id, s.title, s.publisher, s.folder_path, s.gcd_candidates_json,
                   SUM(CASE WHEN i.owned = 1 THEN 1 ELSE 0 END) AS owned
            FROM tracked_series s LEFT JOIN issue_status i ON i.tracked_series_id = s.id
            WHERE s.gcd_link = 'ambiguous' GROUP BY s.id ORDER BY owned DESC, s.title""")]
    for r in rows:
        r["candidates"] = json.loads(r.pop("gcd_candidates_json") or "[]")
    return rows


@router.get("/api/harvest/gcd-candidates")
def get_gcd_candidates():
    return gcd_candidates()
