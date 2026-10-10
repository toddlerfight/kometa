"""Harvest what Komga still knows before it goes — Komga retirement, step 1.

Read-only against Komga. For every tracked series with a Komga id: the series'
summary, publisher, genres and tags; each book's summary, authors (with roles),
release date and ISBN; and the series poster if someone uploaded one in Komga
(a GENERATED thumbnail is just page 1 again — we make those ourselves). All of
it lands in the local record ONLY where no catalogue has answered: a Metron or
LOCG row is never overwritten, a ComicInfo row from the file is.

    python -m kometa.harvest_komga        # one-off, prints counts
    POST /api/komga/harvest               # the same, in a thread
"""
import json
import logging
import threading
from datetime import datetime, timezone

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
_PRIORITY = {None: 0, "": 0, "comicinfo": 1, "komga": 1, "locg": 3, "metron": 4}
_state = {"running": False, "last": None}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _open_for(row: dict | None, source: str) -> bool:
    """A row written by a lesser source (or a miss, or nothing) may be replaced."""
    if row is None or row.get("fill_state") == "miss":
        return True
    return _PRIORITY.get(row.get("source"), 2) < _PRIORITY.get(source, 1)


def _poster(komga, komga_series_id: str) -> bytes | None:
    """The selected series thumbnail, only when a person put it there."""
    try:
        thumbs = komga.get_series_thumbnails(komga_series_id)
    except Exception as e:
        logger.info(f"Komga thumbnails for {komga_series_id} skipped: {e}")
        return None
    pick = next((t for t in thumbs if t.get("selected") and (t.get("type") or "").upper() in ("USER_UPLOADED", "SIDECAR")), None)
    if not pick:
        return None
    try:
        return komga.get_series_thumbnail_bytes(komga_series_id, pick["id"])
    except Exception as e:
        logger.info(f"Komga poster for {komga_series_id} skipped: {e}")
        return None


def harvest_series(series: dict, komga, path=None) -> dict:
    """One series: facts into series_record, its books into issue_record, the
    poster into the store. Returns counts."""
    from kometa import record, covers
    from kometa.naming import parse_issue_number
    path = path or DB_PATH
    out = {"series": 0, "issues": 0, "posters": 0}
    kid = series.get("komga_series_id")
    if not kid:
        return out
    try:
        ks = komga.get_series(kid)
    except Exception as e:
        logger.info(f"Komga series {kid} skipped: {e}")
        return out
    meta = ks.get("metadata") or {}
    record.ensure_tables(path)
    existing = record.get_series(series["id"], path)
    if _open_for(existing, "komga") and (meta.get("summary") or meta.get("publisher") or meta.get("genres") or meta.get("tags")):
        with db._connect(path) as conn:
            conn.execute("""INSERT OR REPLACE INTO series_record (tracked_series_id, desc, publisher, year_began, year_end,
                series_type, issue_count, metron_series_id, locg_series_id, cv_volume_id, source, fetched_at, fill_state,
                genres_json, tags_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'komga', ?, 'partial', ?, ?)""",
                         (series["id"], meta.get("summary") or (existing or {}).get("desc"),
                          meta.get("publisher") or series.get("publisher"), series.get("year_began"), None,
                          series.get("metron_type"), None, series.get("metron_series_id"), series.get("locg_series_id"),
                          series.get("cv_volume_id"), _now(),
                          json.dumps(sorted(meta.get("genres") or [])), json.dumps(sorted(meta.get("tags") or []))))
        out["series"] += 1
    # the poster, if a person set one
    data = _poster(komga, kid)
    if data:
        try:
            covers.store_bytes(covers.series_komga_key(series["id"]), data, "komga", path, note="poster")
            out["posters"] += 1
        except OSError as e:
            logger.info(f"Komga poster for {series.get('title')!r} not kept: {e}")
    # the books
    try:
        books = komga.get_books(kid)
    except Exception as e:
        logger.info(f"Komga books for {series.get('title')!r} skipped: {e}")
        return out
    issues = {i["number"]: i for i in db.get_issues_for_series(series["id"], path)}
    for b in books:
        if b.get("deleted"):
            continue
        n = parse_issue_number(b.get("name") or "", series.get("title") or "")
        if n is None or n not in issues:
            continue
        m = b.get("metadata") or {}
        if not (m.get("summary") or m.get("authors") or m.get("releaseDate") or m.get("isbn")):
            continue
        row = record.get_issue(series["id"], n, path)
        if not _open_for(row, "komga"):
            continue
        credits = [{"role": (a.get("role") or "").lower(), "name": a.get("name"), "metron_creator_id": None}
                   for a in (m.get("authors") or []) if a.get("name")]
        record.write_issue(series["id"], n, {"desc": m.get("summary") or "", "credits": credits, "arcs": [], "covers": [],
                                              "store_date": m.get("releaseDate") or issues[n].get("store_date"),
                                              "page_count": (b.get("media") or {}).get("pagesCount"),
                                              "isbn": m.get("isbn") or None},
                           "komga", "partial", issues[n], path)
        out["issues"] += 1
    return out


def harvest(path=None, komga=None, limit: int | None = None) -> dict:
    """Every series with a Komga id. Read-only against Komga."""
    path = path or DB_PATH
    if komga is None:
        from kometa.sources import komga as _komga
        komga = _komga()
    totals = {"series_seen": 0, "series": 0, "issues": 0, "posters": 0}
    if not komga:
        totals["error"] = "Komga isn't configured"
        return totals
    rows = [s for s in db.get_all_series(path) if s.get("komga_series_id") and s.get("kind") != "arc"]
    if limit:
        rows = rows[:limit]
    for s in rows:
        totals["series_seen"] += 1
        out = harvest_series(s, komga, path)
        for k in ("series", "issues", "posters"):
            totals[k] += out[k]
    logger.info(f"Komga harvest: {totals}")
    return totals


def harvest_in_background(path=None) -> dict:
    if _state["running"]:
        return {"running": True, "last": _state["last"]}
    _state["running"] = True

    def run():
        try:
            _state["last"] = harvest(path)
        except Exception as e:
            _state["last"] = {"error": str(e)}
            logger.warning(f"Komga harvest failed: {e}")
        finally:
            _state["running"] = False
    threading.Thread(target=run, name="komga-harvest", daemon=True).start()
    return {"running": True, "last": _state["last"]}


def status() -> dict:
    return {"running": _state["running"], "last": _state["last"]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps(harvest(), indent=1))
