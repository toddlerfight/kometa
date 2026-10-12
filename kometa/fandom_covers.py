"""Fandom cover galleries — the harvest worker (enrichment 1i-d).

The Marvel and DC wikis list every cover of an issue on its page: retailer and
convention exclusives, virgins, printings, mostly 1300–1900px where LOCG stops
at 600. This worker asks for owned Marvel/DC issues' galleries, one request per
issue, through fandom_client's ONE throttle (the same lock, gap, daily cap and
pause the collected-edition lookups use — there is no second Fandom client).

What comes back is METADATA ONLY, into the record's extra:
  extra['fandom'] = {'page': title, 'at': date, 'covers': [{name, url, width, height}]}
                  | {'page': title, 'at': date, 'missing': True}
and an issue isn't asked again for RECHECK_DAYS.

Downloads are off unless Settings' 'Download gallery images' (config
fandom_download = '1') is on: a full gallery store could run to tens of GB on
ginbako's internal disk, and waits for the cover store's move. With it on, the
image trickle queues gallery covers last and hands each one to download() here,
which hashes it against the covers the issue already has: a close match is the
SAME cover at a bigger size ('upgrade_of'), not a new variant.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import kometa.db as db
from kometa import fandom_client
from kometa.polite import enabled

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH

PER_TICK = 10                 # issues per 10-min tick; each costs 1 request (2 for a missing page)
RECHECK_DAYS = 30             # a gallery — or a page that isn't there — isn't asked again within this


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _recent(entry: dict | None) -> bool:
    if not isinstance(entry, dict) or not entry.get("at"):
        return False
    try:
        at = datetime.strptime(str(entry["at"])[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - at < timedelta(days=RECHECK_DAYS)


def run_title(series: dict, extra: dict) -> str:
    """The name the wiki files this issue under. A run spanning several Metron
    series (metron_extra_ids: Uncanny X-Men = X-Men 1963 + The Uncanny X-Men
    1981) takes the run's own name, but only when the record says which run
    the issue came from (Metron's series name in extra); otherwise the title."""
    if (series.get("metron_extra_ids") or "").strip() and extra.get("series_name"):
        return extra["series_name"]
    return series.get("title") or ""


def titles_to_try(name: str, volume, number) -> list[str]:
    """The wikis drop a leading 'The' ('Uncanny X-Men Vol 1 142'; the 'The …'
    title doesn't even redirect, probed 2026-10-12), so that form goes first and
    the 'The' form is only a fallback."""
    out = []
    bare = re.sub(r"^the\s+", "", name.strip(), flags=re.I)
    for n in (bare, name.strip()):
        t = fandom_client.page_title(n, volume, number)
        if n and t not in out:
            out.append(t)
    return out


def candidates(path=None, limit: int = PER_TICK) -> list[dict]:
    """Owned issues in Marvel or DC runs whose gallery hasn't been asked within
    RECHECK_DAYS. Pull list first. The record's extra must exist: that's where
    the answer goes (record.merge_extra writes nowhere else)."""
    path = path or DB_PATH
    from kometa import record
    record.ensure_tables(path)
    with db._connect(path) as conn:
        rows = conn.execute("""
            SELECT s.id AS sid, s.title, s.publisher, s.metron_extra_ids, i.number, r.extra_json
            FROM issue_status i JOIN tracked_series s ON s.id = i.tracked_series_id
            JOIN issue_record r ON r.tracked_series_id = i.tracked_series_id AND r.number = i.number
            WHERE i.owned = 1 AND r.extra_json IS NOT NULL AND (s.kind IS NULL OR s.kind != 'arc')
            ORDER BY s.on_pull_list DESC, s.id, i.number""").fetchall()
    out = []
    for r in rows:
        try:
            extra = json.loads(r["extra_json"] or "{}") or {}
        except (TypeError, ValueError):
            continue
        wiki = fandom_client.wiki_for(r["publisher"] or extra.get("publisher"))
        if not wiki or _recent(extra.get("fandom")):
            continue
        name = run_title(dict(r), extra)
        if not name:
            continue
        vol = extra.get("series_volume")
        out.append({"sid": r["sid"], "number": r["number"], "wiki": wiki,
                    "titles": titles_to_try(name, vol if isinstance(vol, int) and vol > 0 else 1, r["number"])})
        if len(out) >= limit:
            break
    return out


def ask(c: dict, gallery=None) -> dict:
    """One issue's gallery entry for extra['fandom']. Raises FandomUnavailable
    when the wiki is resting or out of today's requests (nothing is recorded)."""
    gallery = gallery or fandom_client.gallery
    for title in c["titles"]:
        covers = gallery(c["wiki"], title)
        if covers is not None:
            return {"page": title, "at": _today(), "covers": covers}
    return {"page": c["titles"][0], "at": _today(), "missing": True}


def tick(limit: int = PER_TICK, path=None, gallery=None) -> int:
    """Scheduler tick: a few owned issues' galleries listed into the record.
    A no-op with the toggle off, today's requests spent or the wiki resting.
    Returns issues answered."""
    path = path or DB_PATH
    if not enabled("fandom_enabled", path):
        return 0
    b = fandom_client.budget()
    if b["left"] <= 0 or b["paused"]:
        return 0
    from kometa import record
    done = 0
    try:
        for c in candidates(path, limit):
            entry = ask(c, gallery)
            record.merge_extra(c["sid"], c["number"], {"fandom": entry}, path)
            done += 1
    except fandom_client.FandomUnavailable as e:
        logger.info(f"Fandom galleries stopped this tick: {e}")
    if done:
        logger.info(f"Fandom: {done} issue galleries listed")
    return done


# --- downloads (only with fandom_download = '1'; the image trickle calls this) -------
def _parse_key(key: str) -> tuple[int, float] | None:
    m = re.match(r"^issue/(\d+)/([^/]+)/fandom/", key)
    if not m:
        return None
    try:
        return int(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def closest_cover(series_id: int, number: float, h: int, path=None) -> str | None:
    """The key of the issue's stored cover that IS this picture (dHash within
    coververify.CLOSE) — the main, a catalogue variant, ComicVine's or PRH's —
    or None. The file's own page 1 and other Fandom images don't count."""
    from kometa import coververify, images
    path = path or DB_PATH
    images.ensure_tables(path)
    prefix = f"issue/{int(series_id)}/{number:g}/"
    best, best_d = None, coververify.CLOSE + 1
    with db._connect(path) as conn:
        rows = conn.execute("SELECT key, path FROM image_record WHERE key LIKE ? AND path IS NOT NULL",
                            (prefix + "%",)).fetchall()
    for r in rows:
        k = r["key"]
        if "/fandom/" in k or k.endswith("/file") or not os.path.exists(r["path"]):
            continue
        try:
            with open(r["path"], "rb") as f:
                other = coververify.dhash(f.read())
        except OSError:
            continue
        if other is None:
            continue
        d = coververify.hamming(h, other)
        if d < best_d:
            best, best_d = k, d
    return best if best_d <= coververify.CLOSE else None


def download(key: str, url: str, path=None, http=None) -> dict | None:
    """One gallery cover: download, hash against what the issue already holds,
    keep it under its fandom key, and note in the gallery entry whether it's an
    upgrade of a cover we have ('upgrade_of': that key) or a new one (None).
    Returns the image row like images.fetch_image, or None on a failed fetch."""
    from kometa import coververify, images, record
    path = path or DB_PATH
    try:
        data = (http or images._download)(url)
    except Exception as e:
        logger.info(f"Fandom image {key}: fetch failed: {e}")
        data = None
    if not data:
        # the store's own failure bookkeeping (a day before the URL is tried again)
        return images.fetch_image(key, url, "fandom", path, http=lambda _u: None)
    where = _parse_key(key)
    h = coververify.dhash(data)
    match = closest_cover(*where, h, path) if where and h is not None else None
    row = images.store_bytes(key, data, url, "fandom", path)
    if where:
        entry = (record.get_issue(*where, path) or {}).get("extra") or {}
        g = entry.get("fandom") or {}
        changed = False
        for c in g.get("covers") or []:
            if images.fandom_key(where[0], where[1], c.get("name")) == key:
                c["upgrade_of"] = match
                changed = True
        if changed:
            record.merge_extra(where[0], where[1], {"fandom": g}, path)
    return row


def status(path=None) -> dict:
    """The harvest card's row: issues with a gallery, pages missing, covers listed."""
    path = path or DB_PATH
    from kometa import images, record
    record.ensure_tables(path)
    images.ensure_tables(path)
    galleries = missing = covers = upgrades = 0
    with db._connect(path) as conn:
        for r in conn.execute("""SELECT extra_json FROM issue_record WHERE extra_json LIKE '%"fandom": {%'"""):
            try:
                g = (json.loads(r[0] or "{}") or {}).get("fandom") or {}
            except (TypeError, ValueError):
                continue
            if g.get("missing"):
                missing += 1
            elif "covers" in g:
                galleries += 1
                covers += len(g["covers"] or [])
                upgrades += sum(1 for c in g["covers"] or [] if c.get("upgrade_of"))
        downloaded = conn.execute("SELECT COUNT(*) FROM image_record WHERE key LIKE 'issue/%/fandom/%' AND path IS NOT NULL").fetchone()[0]
    cfg = db.get_config(path)
    return {"galleries": galleries, "missing": missing, "covers": covers, "upgrades": upgrades,
            "downloaded": downloaded, "budget": fandom_client.budget(), "enabled": enabled("fandom_enabled", path),
            "download": cfg.get("fandom_download", "0") == "1"}
