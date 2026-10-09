"""Cover art the record OWNS: bytes on disk, by stable key, kept for good.

The old cover-cache was keyed by URL and filled only when a tile asked — a
catalogue going dark left every unseen cover blank. This store is keyed by
what the image IS (issue/<series>/<number>/main, .../variant/<id>, series/<id>,
trade/<key>), filled ahead of time by a trickle for everything tracked, owned
or on a reading list, and refreshed only when the URL for a key changes.
Catalogue cards (Because, More-from, Trending) are NOT in scope: they keep the
short-lived URL cache.
"""
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

from fastapi import Response

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
COVERS_DIR = os.path.join(os.path.dirname(DB_PATH) or ".", "covers")
FAIL_TTL_HOURS = 24
TRICKLE_LIMIT = 60
TRICKLE_GAP_S = 0.5
TRICKLE_MAX_FAILS = 5
_KEY_RE = re.compile(r"^[A-Za-z0-9_.:\-]+(?:/[A-Za-z0-9_.:\-]+)*$")


def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS image_record (
            key TEXT PRIMARY KEY, path TEXT, url TEXT, bytes INTEGER, fetched_at TEXT, source TEXT, failed_at TEXT)""")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def issue_key(series_id: int, number: float) -> str:
    return f"issue/{int(series_id)}/{number:g}/main"


def variant_key(series_id: int, number: float, variant_id) -> str:
    return f"issue/{int(series_id)}/{number:g}/variant/{re.sub(r'[^A-Za-z0-9_.-]', '_', str(variant_id))}"


def series_key(series_id: int) -> str:
    return f"series/{int(series_id)}"


def trade_key(key: str) -> str:
    return f"trade/{re.sub(r'[^A-Za-z0-9_.:-]', '_', str(key))}"


def file_for(key: str, root: str | None = None) -> str:
    if not _KEY_RE.match(key):
        raise ValueError(f"bad image key {key!r}")
    return os.path.join(root or COVERS_DIR, key + ".jpg")


def get(key: str, path=None) -> dict | None:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT * FROM image_record WHERE key = ?", (key,)).fetchone()
    return dict(r) if r else None


def local_path(key: str, path=None) -> str | None:
    """The file on disk for a key, if the store holds it."""
    r = get(key, path)
    if r and r.get("path") and os.path.exists(r["path"]):
        return r["path"]
    return None


def _download(url: str) -> bytes | None:
    from kometa.thumbnails import _img_session
    r = _img_session.get(url, timeout=20)
    if r.ok and r.content:
        return r.content
    return None


def fetch_image(key: str, url: str | None, source: str, path=None, root: str | None = None, http=None) -> dict | None:
    """Download once, keep forever; re-fetch only when the URL for the key
    changes. A failure is remembered for a day so a dead URL isn't hammered.
    Returns the row, or None when there's nothing on disk for the key."""
    path = path or DB_PATH
    if not url or not url.startswith("http"):
        return None
    row = get(key, path)
    if row and row.get("url") == url and row.get("path") and os.path.exists(row["path"]):
        return row
    if row and row.get("failed_at") and row.get("url") == url:
        try:
            failed = datetime.strptime(row["failed_at"], "%Y-%m-%d %H:%M:%S")
            if datetime.utcnow() - failed < timedelta(hours=FAIL_TTL_HOURS):
                return None
        except ValueError:
            pass
    dest = file_for(key, root)
    try:
        data = (http or _download)(url)
    except Exception as e:
        logger.info(f"Image {key}: fetch failed: {e}")
        data = None
    with db._connect(path) as conn:
        if not data:
            conn.execute("""INSERT INTO image_record (key, path, url, bytes, fetched_at, source, failed_at) VALUES (?, NULL, ?, 0, NULL, ?, ?)
                ON CONFLICT(key) DO UPDATE SET url = excluded.url, source = excluded.source, failed_at = excluded.failed_at""",
                         (key, url, source, _now()))
            return None
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = f"{dest}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        conn.execute("""INSERT OR REPLACE INTO image_record (key, path, url, bytes, fetched_at, source, failed_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)""", (key, dest, url, len(data), _now(), source))
    return get(key, path)


def _ct(data: bytes) -> str:
    from kometa.thumbnails import _img_ct
    return _img_ct(data)


def serve(key: str, url: str | None, source: str, path=None, max_age: int = 2592000) -> Response | None:
    """A Response from the store: disk first, else fetch-and-store, else None so
    the caller falls through to its old chain. X-Kometa-Image says which."""
    row = get(key, path)
    hit = row and row.get("url") == url and row.get("path") and os.path.exists(row["path"])
    if not hit:
        row = fetch_image(key, url, source, path)
        if not row:
            return None
    try:
        data = open(row["path"], "rb").read()
    except OSError:
        return None
    if not data:
        return None
    return Response(content=data, media_type=_ct(data),
                    headers={"Cache-Control": f"public, max-age={max_age}", "X-Kometa-Image": "disk" if hit else "fetched"})


# --- the trickle: everything tracked, owned or listed, ahead of time ------------------
def pending(path=None, limit: int = 200) -> list[tuple[str, str, str]]:
    """(key, url, source) the store doesn't hold yet, owned and pull-list first:
    issue mains, series covers, variants the record knows, trade covers."""
    path = path or DB_PATH
    ensure_tables(path)
    out: list[tuple[str, str, str]] = []
    with db._connect(path) as conn:
        have = {r[0] for r in conn.execute("SELECT key FROM image_record WHERE path IS NOT NULL OR failed_at > datetime('now', '-1 day')")}
        rows = conn.execute("""SELECT i.tracked_series_id AS sid, i.number, i.metron_image
            FROM issue_status i JOIN tracked_series s ON s.id = i.tracked_series_id
            WHERE i.metron_image LIKE 'http%' AND (s.kind IS NULL OR s.kind != 'arc')
            ORDER BY s.on_pull_list DESC, i.owned DESC, i.tracked_series_id, i.number""").fetchall()
        series_done = set()
        for r in rows:
            k = issue_key(r["sid"], r["number"])
            if k not in have:
                out.append((k, r["metron_image"], "metron"))
            sk = series_key(r["sid"])
            if sk not in have and r["sid"] not in series_done:
                series_done.add(r["sid"])
                out.append((sk, r["metron_image"], "metron"))
            if len(out) >= limit:
                return out[:limit]
        try:
            import json
            for r in conn.execute("""SELECT r.tracked_series_id AS sid, r.number, r.covers_json FROM issue_record r
                JOIN tracked_series s ON s.id = r.tracked_series_id WHERE r.covers_json IS NOT NULL AND r.covers_json != '[]'
                ORDER BY s.on_pull_list DESC, r.tracked_series_id, r.number"""):
                for c in json.loads(r["covers_json"] or "[]"):
                    url = c.get("thumb") or c.get("large")
                    if not url or not c.get("id"):
                        continue
                    k = variant_key(r["sid"], r["number"], c["id"])
                    if k not in have:
                        out.append((k, url, c.get("source") or "catalogue"))
                        if len(out) >= limit:
                            return out[:limit]
            for r in conn.execute("SELECT tracked_series_id AS sid, data_json FROM trades_cache"):
                for t in json.loads(r["data_json"] or "[]"):
                    if t.get("cover") and t.get("locg_id") is not None:
                        k = trade_key(f"{r['sid']}-{t['locg_id']}")
                        if k not in have:
                            out.append((k, t["cover"], "trade"))
                            if len(out) >= limit:
                                return out[:limit]
        except Exception as e:
            logger.info(f"Image trickle: extra sources skipped: {e}")
    return out[:limit]


def images_trickle(limit: int = TRICKLE_LIMIT, path=None, http=None, sleep=time.sleep) -> int:
    """Scheduler tick: fetch up to `limit` covers, a short gap between, stopping
    after a run of failures (the CDN is down, not the one URL)."""
    path = path or DB_PATH
    done, fails = 0, 0
    for key, url, source in pending(path, limit=limit):
        row = fetch_image(key, url, source, path, http=http)
        if row:
            done += 1
            fails = 0
        else:
            fails += 1
            if fails >= TRICKLE_MAX_FAILS:
                logger.info("Image trickle: too many failures in a row, stopping this tick")
                break
        sleep(TRICKLE_GAP_S)
    return done


def store_size(path=None) -> dict:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM image_record WHERE path IS NOT NULL").fetchone()
    return {"images": r[0], "bytes": r[1]}
