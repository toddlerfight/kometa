"""Covers (and a way in) for the community picks (kometa/cbr_people.py).

ComicBookRoundup gives a recommended run as a page path, a name and a year —
no cover, no catalogue id — so 'Because you're reading X' was a row of blank
tiles that all said 'not here', including runs sitting on the shelf under a
slightly different name. This puts a face on each pick:

  - locally, at once: a Kometa series with the same name and year (±1) — its
    own cover, and the card opens it (yours if it's owned)
  - else Metron, in the background (harvest share, a few a tick): the series
    with the same name and year, the publisher to break a tie; its first
    issue's cover

Only picks that were actually SHOWN are asked about (enrich() files them as
'wanted') — never ComicBookRoundup's whole catalogue.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
PER_TICK = 6                     # series a tick: up to ~3 Metron calls each
RETRY_DAYS = 7                   # a miss is asked again after a week — or at once when its year changes
CROWDED = 100                    # a search that fills its page may have cut off the run: look again by year
_ready: set = set()


def ensure_tables(path=None):
    path = path or DB_PATH
    if path in _ready:
        return
    with db._connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS community_match (
            series_path TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'wanted',
            name TEXT, year INTEGER, publisher TEXT,
            metron_series_id INTEGER, series_id INTEGER, cover TEXT, checked_at TEXT)""")
    _ready.add(path)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _norm(t: str | None) -> str:
    t = re.sub(r"\(\d{4}\)", "", (t or "").lower())
    t = re.sub(r"^the\s+", "", t.replace("&", "and"))
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _local(conn, name: str, year: int | None) -> dict | None:
    """A Kometa series with this name and a start within a year: owned or tracked."""
    want = _norm(name)
    best = None
    for r in conn.execute("SELECT id, title, year_began, metron_series_id FROM tracked_series"):
        if _norm(r["title"]) != want:
            continue
        y = r["year_began"] or (int(m.group(1)) if (m := re.search(r"\((\d{4})\)", r["title"] or "")) else None)
        if year is None or y is None or abs(int(y) - int(year)) <= 1:
            best = r
            if y is not None and year is not None and int(y) == int(year):
                break
    if not best:
        return None
    return {"series_id": best["id"], "metron_series_id": best["metron_series_id"],
            "cover": f"/api/series/{best['id']}/thumbnail"}


def _ask_again(m: dict, it: dict) -> bool:
    """A miss is worth another look when the pick's year has changed, or after a week."""
    if it.get("year") != m.get("year"):
        return True
    try:
        return datetime.strptime((m.get("checked_at") or "")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) \
            < datetime.now(timezone.utc) - timedelta(days=RETRY_DAYS)
    except ValueError:
        return True


def _owned_ids(conn) -> set[int]:
    ids = {r[0] for r in conn.execute("SELECT DISTINCT tracked_series_id FROM issue_status WHERE owned = 1")}
    return ids | {r[0] for r in conn.execute("SELECT DISTINCT tracked_series_id FROM books WHERE tracked_series_id IS NOT NULL")}


def enrich(items: list[dict], path=None) -> list[dict]:
    """Add cover / series_id / owned to each pick. Local matches land now; the
    rest are filed as wanted for the Metron tick. Never a network call here."""
    path = path or DB_PATH
    ensure_tables(path)
    if not items:
        return items
    with db._connect(path) as conn:
        known = {r["series_path"]: dict(r) for r in conn.execute(
            f"SELECT * FROM community_match WHERE series_path IN ({','.join('?' * len(items))})",
            [it["series_path"] for it in items])}
        owned = _owned_ids(conn)
        for it in items:
            m = known.get(it["series_path"])
            if m and m["state"] == "none" and _ask_again(m, it):
                # Immortal Hulk was asked under 1970 (an epoch date, since fixed) and stayed blank
                conn.execute("""UPDATE community_match SET state = 'wanted', name = ?, year = ?, publisher = ?
                                WHERE series_path = ?""", (it.get("name"), it.get("year"), it.get("publisher"), it["series_path"]))
                m = {**m, "state": "wanted"}
            if not m or m["state"] == "wanted":
                loc = _local(conn, it.get("name") or "", it.get("year"))
                if loc:
                    m = {**loc, "state": "linked"}
                    conn.execute("""INSERT INTO community_match (series_path, state, name, year, publisher, metron_series_id,
                        series_id, cover, checked_at) VALUES (?, 'linked', ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(series_path) DO UPDATE SET state='linked', metron_series_id=excluded.metron_series_id,
                        series_id=excluded.series_id, cover=excluded.cover, checked_at=excluded.checked_at""",
                                 (it["series_path"], it.get("name"), it.get("year"), it.get("publisher"),
                                  loc["metron_series_id"], loc["series_id"], loc["cover"], _now()))
                elif not m:
                    conn.execute("""INSERT OR IGNORE INTO community_match (series_path, state, name, year, publisher)
                                    VALUES (?, 'wanted', ?, ?, ?)""",
                                 (it["series_path"], it.get("name"), it.get("year"), it.get("publisher")))
            if m and m.get("state") == "linked" and not m.get("series_id") and m.get("metron_series_id"):
                # matched to the catalogue back when nobody tracked it, and since asked for (Want to
                # read tracks it, 2026-10-24): it's a run of yours now — the tile opens it, not the ask
                t = conn.execute("SELECT id FROM tracked_series WHERE metron_series_id = ? AND kind != 'arc'",
                                 (m["metron_series_id"],)).fetchone()
                if t:
                    m = {**m, "series_id": t[0]}
                    conn.execute("UPDATE community_match SET series_id = ? WHERE series_path = ?", (t[0], it["series_path"]))
            if m and m.get("state") == "linked":
                it["cover"] = m.get("cover")
                it["metron_series_id"] = m.get("metron_series_id")
                it["series_id"] = m.get("series_id")
                it["owned"] = bool(m.get("series_id") and m["series_id"] in owned)
    return items


_PUB_WORDS = {"dc-comics": "dc", "marvel-comics": "marvel", "image-comics": "image", "boom-studios": "boom",
              "dark-horse-comics": "dark horse", "idw-publishing": "idw", "dynamite-entertainment": "dynamite"}


def _pick(rows: list[dict], name: str, year: int | None, publisher: str | None) -> dict | None:
    want, pub = _norm(name), (_PUB_WORDS.get(publisher or "") or (publisher or "").replace("-", " ")).lower()
    same = [r for r in rows if _norm(r.get("title")) == want]
    if year is not None:
        exact = [r for r in same if r.get("year") == year]
        near = exact or [r for r in same if r.get("year") and abs(int(r["year"]) - int(year)) <= 1]
    else:
        near = same
    if len(near) > 1 and pub:
        near = [r for r in near if pub.split()[0] in (r.get("publisher") or "").lower()] or near
    return near[0] if len(near) == 1 or (near and year is not None) else None


def tick(limit: int = PER_TICK, path=None, client=None) -> int:
    """Scheduler (harvest share): match a few wanted picks to Metron. Stops at the
    harvest floor or Metron's first refusal, like every harvest tick."""
    from kometa import metron_client
    client = client or metron_client
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM community_match WHERE state = 'wanted' ORDER BY rowid LIMIT ?", (limit,))]
    done = 0
    for r in rows:
        if not client.configured() or client.harvest_share_spent():
            break
        try:
            rows = client.search_series(r["name"] or "")
            hit = _pick(rows, r["name"] or "", r["year"], r["publisher"])
            if not hit and r["year"] and len(rows) >= CROWDED:
                # the page filled with other runs (X-Men: 100 spin-offs, no 2019 X-Men): ask by year
                for y in (r["year"], r["year"] - 1, r["year"] + 1):
                    hit = _pick(client.search_series(r["name"] or "", year=y), r["name"] or "", r["year"], r["publisher"])
                    if hit:
                        break
            cover = None
            if hit:
                first = client._get("issue/", series_id=hit["id"]).get("results", [])
                first = sorted(first, key=lambda i: (client._num(i.get("number")) or 9e9))
                cover = next((i.get("image") for i in first if i.get("image")), None)
        except client.MetronUnavailable as e:
            logger.info(f"Community covers paused: {e}")
            break
        with db._connect(path) as conn:
            sid = None
            if hit:
                s = conn.execute("SELECT id FROM tracked_series WHERE metron_series_id = ?", (hit["id"],)).fetchone()
                sid = s[0] if s else None
            conn.execute("""UPDATE community_match SET state = ?, metron_series_id = ?, series_id = ?, cover = ?, checked_at = ?
                            WHERE series_path = ?""",
                         ("linked" if hit else "none", hit and hit["id"], sid,
                          f"/api/series/{sid}/thumbnail" if sid else cover, _now(), r["series_path"]))
        done += 1
    if done:
        logger.info(f"Community covers: {done} pick(s) looked up")
    return done
