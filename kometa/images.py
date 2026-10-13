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
EXT_TTL_HOURS = 24
# Hosts whose images the catalogue cards carry; anything else is refused, this
# is a cover proxy, not an open one.
EXT_HOSTS = ("static.metron.cloud", "metron.cloud", "s3.amazonaws.com", "comicvine.gamespot.com")
_KEY_RE = re.compile(r"^[A-Za-z0-9_.:\-]+(?:/[A-Za-z0-9_.:\-]+)*$")
# LOCG's S3 covers come in small-/medium-/large-; large- (600px) is the top of
# the ladder (original-/xlarge- are 403). The store was keeping 170px medium-
# because that's what the catalogue happened to hand over.
_LOCG_S3_SMALL = re.compile(r"^(https?://s3\.amazonaws\.com/comicgeeks/comics/covers/)(?:small|medium)-")


def best_url(url: str | None) -> str | None:
    """The biggest version of a cover URL the host will hand out. One rule, so
    the trickle, the serve path and the re-fetch check never disagree about
    which URL a key should hold (a disagreement = the same cover downloaded
    twice a day, forever)."""
    if not url:
        return url
    return _LOCG_S3_SMALL.sub(r"\1large-", url)


def best_cover_url(cover: dict) -> str | None:
    """A catalogue cover dict ({thumb, large, ...}): large first. Thumb used to
    win, which is how variants ended up stored at thumbnail size."""
    return best_url(cover.get("large") or cover.get("thumb"))


@db.once_per_db
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


def cv_key(series_id: int, number: float) -> str:
    """ComicVine's full-size original of the main cover (enrichment 1f) — kept
    beside the catalogue's 600–768px main, not over it."""
    return f"{issue_key(series_id, number).rsplit('/', 1)[0]}/cv"


def prh_key(series_id: int, number: float) -> str:
    """Penguin Random House's print-res main cover (enrichment 1i), converted to
    JPEG on arrival — beside the catalogue main and ComicVine's, not over them."""
    return f"{issue_key(series_id, number).rsplit('/', 1)[0]}/prh"


def fandom_key(series_id: int, number: float, name: str) -> str:
    """One cover from an issue's Fandom gallery (enrichment 1i-d), by its file
    name on the wiki — beside the catalogue's variants, never over them."""
    safe = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "cover"))).strip("_.")[:120] or "cover"
    return f"{issue_key(series_id, number).rsplit('/', 1)[0]}/fandom/{safe}"


def marveldc_key(series_id: int, number: float, name: str | None = None) -> str:
    """Marvel.com's print-size cover (enrichment 1i-e): the main at .../marveldc,
    a variant the issue page lists at .../marveldc/<name> — beside every other
    source's copy, never over it."""
    base = f"{issue_key(series_id, number).rsplit('/', 1)[0]}/marveldc"
    if name is None:
        return base
    safe = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))).strip("_.")[:120] or "cover"
    return f"{base}/{safe}"


def isbn_key(isbn: str, source: str) -> str:
    """A book's cover by ISBN (Open Library): one ISBN can be a trade on several
    runs' shelves, so the image belongs to the ISBN, not to a series."""
    return f"isbn/{re.sub(r'[^0-9Xx]', '', str(isbn))}/{source}"


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


def fetch_image(key: str, url: str | None, source: str, path=None, root: str | None = None, http=None,
                force: bool = False) -> dict | None:
    """Download once, keep forever; re-fetch only when the URL for the key
    changes (or `force`). A failure is remembered for a day so a dead URL isn't
    hammered. Returns the row, or None when there's nothing on disk for the key.

    A failed re-fetch never costs the file already on disk: the row keeps its
    path, takes the new URL plus a failed_at, and comes back with failed_at set
    so the caller can tell. The old cover keeps serving until the new URL works."""
    path = path or DB_PATH
    if not url or not url.startswith("http"):
        return None
    row = get(key, path)
    on_disk = bool(row and row.get("path") and os.path.exists(row["path"]))
    if row and not force and row.get("url") == url and on_disk and not row.get("failed_at"):
        return row
    if row and row.get("failed_at") and row.get("url") == url:
        try:
            failed = datetime.strptime(row["failed_at"], "%Y-%m-%d %H:%M:%S")
            if datetime.utcnow() - failed < timedelta(hours=FAIL_TTL_HOURS):
                return row if on_disk else None
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
            return get(key, path) if on_disk else None
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = f"{dest}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        conn.execute("""INSERT OR REPLACE INTO image_record (key, path, url, bytes, fetched_at, source, failed_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)""", (key, dest, url, len(data), _now(), source))
    return get(key, path)


def store_bytes(key: str, data: bytes, url: str, source: str, path=None, root: str | None = None) -> dict | None:
    """Keep bytes a worker fetched itself (through its own source's throttle —
    PRH's TIFFs, Open Library's covers) under a key, the same way fetch_image
    does after its download. Not for the trickle: those sources mustn't be
    fetched by the trickle's 0.5 s image pace."""
    path = path or DB_PATH
    ensure_tables(path)
    if not data:
        return None
    dest = file_for(key, root)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = f"{dest}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    with db._connect(path) as conn:
        conn.execute("""INSERT OR REPLACE INTO image_record (key, path, url, bytes, fetched_at, source, failed_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)""", (key, dest, url, len(data), _now(), source))
    return get(key, path)


def _ct(data: bytes) -> str:
    from kometa.thumbnails import _img_ct
    return _img_ct(data)


def serve(key: str, url: str | None, source: str, path=None, max_age: int = 86400) -> Response | None:
    """A Response from the store: disk first, else fetch-and-store, else None so
    the caller falls through to its old chain. X-Kometa-Image says which.
    The URL goes through best_url first, or a tile asking with the catalogue's
    medium- URL would undo the trickle's large- upgrade on every view."""
    from kometa import coverupgrade
    better = coverupgrade.better_path(key, path)            # a bigger copy of the same cover (Marvel.com, 1i-e)
    if better:
        try:
            data = open(better, "rb").read()
        except OSError:
            data = b""
        if data:
            return Response(content=data, media_type=_ct(data),
                            headers={"Cache-Control": f"public, max-age={max_age}, stale-while-revalidate=604800",
                                     "X-Kometa-Image": "upgrade"})
    url = best_url(url)
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
                    headers={"Cache-Control": f"public, max-age={max_age}, stale-while-revalidate=604800", "X-Kometa-Image": "disk" if hit else "fetched"})


# --- catalogue covers: a day on disk, then gone ----------------------------------------
def ext_allowed(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except Exception:
        return False
    return u.scheme in ("http", "https") and (u.hostname or "") in EXT_HOSTS


def ext_key(url: str) -> str:
    import hashlib
    return "ext/" + hashlib.sha1(url.encode()).hexdigest()


def _fresh(row: dict | None, hours: int) -> bool:
    if not row or not row.get("fetched_at") or not row.get("path") or not os.path.exists(row["path"]):
        return False
    try:
        return datetime.utcnow() - datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S") < timedelta(hours=hours)
    except ValueError:
        return False


def serve_external(url: str, path=None, http=None) -> Response | None:
    """A catalogue card's cover (a series NOT on the shelf): cached on disk for
    EXT_TTL_HOURS so a row of nine stops loading cover by cover, then dropped —
    never the forever rule the record's own covers get. None when the fetch
    fails; the caller 404s."""
    path = path or DB_PATH
    key = ext_key(url)
    row = get(key, path)
    hit = _fresh(row, EXT_TTL_HOURS)
    if not hit:
        row = fetch_image(key, url, "ext", path, http=http, force=True)
        if not row:
            return None
    try:
        data = open(row["path"], "rb").read()
    except OSError:
        return None
    if not data:
        return None
    return Response(content=data, media_type=_ct(data),
                    headers={"Cache-Control": f"public, max-age={EXT_TTL_HOURS * 3600}", "X-Kometa-Image": "disk" if hit else "fetched"})


def prune_external(path=None, hours: int = EXT_TTL_HOURS) -> int:
    """Drop ext/ entries older than the TTL — file and row. Returns how many."""
    path = path or DB_PATH
    ensure_tables(path)
    n = 0
    with db._connect(path) as conn:
        rows = conn.execute("SELECT key, path FROM image_record WHERE key LIKE 'ext/%' AND "
                            "(fetched_at IS NULL OR fetched_at < datetime('now', ?))", (f"-{int(hours)} hours",)).fetchall()
        for r in rows:
            if r["path"]:
                try:
                    os.remove(r["path"])
                except OSError:
                    pass
            conn.execute("DELETE FROM image_record WHERE key = ?", (r["key"],))
            n += 1
    return n


# --- the trickle: everything tracked, owned or listed, ahead of time ------------------
def _wants(rows: dict, key: str, url: str) -> bool:
    """Does the trickle owe this key a fetch? Yes when nothing's stored, or the
    stored URL isn't today's best (the 170px LOCG covers, thumb-first variants),
    unless that best URL failed within the day: a dead large- isn't hammered,
    and the small file it failed to replace keeps serving meanwhile."""
    r = rows.get(key)
    if not r:
        return True
    if r["recent_fail"] and r["url"] == url:
        return False
    return not (r["path"] and r["url"] == url and not r["failed_at"])


def pending(path=None, limit: int = 200) -> list[tuple[str, str, str]]:
    """(key, url, source) the store doesn't hold at its best URL yet, owned and
    pull-list first: issue mains, series covers, variants the record knows,
    trade covers. A key held at a smaller URL comes back for the upgrade."""
    path = path or DB_PATH
    ensure_tables(path)
    out: list[tuple[str, str, str]] = []
    with db._connect(path) as conn:
        # every row, not just the held keys: a row existing no longer means
        # done, its url has to be the best one too
        have = {r["key"]: r for r in conn.execute(
            """SELECT key, url, path, failed_at, COALESCE(failed_at > datetime('now', '-1 day'), 0) AS recent_fail
               FROM image_record WHERE key LIKE 'issue/%' OR key LIKE 'series/%' OR key LIKE 'trade/%'""")}
        rows = conn.execute("""SELECT i.tracked_series_id AS sid, i.number, i.metron_image
            FROM issue_status i JOIN tracked_series s ON s.id = i.tracked_series_id
            WHERE i.metron_image LIKE 'http%' AND (s.kind IS NULL OR s.kind != 'arc')
            ORDER BY s.on_pull_list DESC, i.owned DESC, i.tracked_series_id, i.number""").fetchall()
        series_done = set()
        for r in rows:
            url = best_url(r["metron_image"])
            k = issue_key(r["sid"], r["number"])
            if _wants(have, k, url):
                out.append((k, url, "metron"))
            # the series card is its first issue's cover: judged once per series,
            # on that first URL, or every later issue would re-queue it
            if r["sid"] not in series_done:
                series_done.add(r["sid"])
                sk = series_key(r["sid"])
                if _wants(have, sk, url):
                    out.append((sk, url, "metron"))
            if len(out) >= limit:
                return out[:limit]
        try:
            import json
            for r in conn.execute("""SELECT r.tracked_series_id AS sid, r.number, r.covers_json FROM issue_record r
                JOIN tracked_series s ON s.id = r.tracked_series_id WHERE r.covers_json IS NOT NULL AND r.covers_json != '[]'
                ORDER BY s.on_pull_list DESC, r.tracked_series_id, r.number"""):
                for c in json.loads(r["covers_json"] or "[]"):
                    url = best_cover_url(c)
                    if not url or not c.get("id"):
                        continue
                    k = variant_key(r["sid"], r["number"], c["id"])
                    if _wants(have, k, url):
                        out.append((k, url, c.get("source") or "catalogue"))
                        if len(out) >= limit:
                            return out[:limit]
            for r in conn.execute("SELECT tracked_series_id AS sid, data_json FROM trades_cache"):
                for t in json.loads(r["data_json"] or "[]"):
                    if t.get("cover") and t.get("locg_id") is not None:
                        url = best_url(t["cover"])
                        k = trade_key(f"{r['sid']}-{t['locg_id']}")
                        if _wants(have, k, url):
                            out.append((k, url, "trade"))
                            if len(out) >= limit:
                                return out[:limit]
            # Fandom galleries (1i-d): only with Settings' 'Download gallery images' on.
            # Ahead of ComicVine (2026-10-13): a gallery copy replaces a cover you SEE,
            # ComicVine's original never shows for an owned issue — same pace either way
            if db.get_config(path).get("fandom_download", "0") == "1":
                for r in conn.execute("""SELECT r.tracked_series_id AS sid, r.number, r.extra_json FROM issue_record r
                    JOIN tracked_series s ON s.id = r.tracked_series_id
                    WHERE r.extra_json LIKE '%"fandom": {%' ORDER BY s.on_pull_list DESC, r.tracked_series_id, r.number"""):
                    g = json.loads(r["extra_json"] or "{}").get("fandom") or {}
                    for c in g.get("covers") or []:
                        url = c.get("url")
                        k = fandom_key(r["sid"], r["number"], c.get("name"))
                        if url and _wants(have, k, url):
                            out.append((k, url, "fandom"))
                            if len(out) >= limit:
                                return out[:limit]
            # ComicVine's originals (1f): last — the biggest files, and never on screen
            # for an owned issue (its tile is the file's own page 1)
            for r in conn.execute("""SELECT tracked_series_id AS sid, number, extra_json FROM issue_record
                WHERE extra_json LIKE '%"cv_original": "http%' ORDER BY tracked_series_id, number"""):
                url = json.loads(r["extra_json"] or "{}").get("cv_original")
                k = cv_key(r["sid"], r["number"])
                if url and _wants(have, k, url):
                    out.append((k, url, "comicvine"))
                    if len(out) >= limit:
                        return out[:limit]
        except Exception as e:
            logger.info(f"Image trickle: extra sources skipped: {e}")
    return out[:limit]


def images_trickle(limit: int = TRICKLE_LIMIT, path=None, http=None, sleep=time.sleep) -> int:
    """Scheduler tick: fetch up to `limit` covers, a short gap between, stopping
    after a run of failures (the CDN is down, not the one URL)."""
    path = path or DB_PATH
    try:
        pruned = prune_external(path)
        if pruned:
            logger.info(f"Image trickle: dropped {pruned} day-old catalogue cover(s)")
    except Exception as e:
        logger.info(f"Image trickle: prune skipped: {e}")
    done, fails = 0, 0
    from kometa.activity import yield_to_reader
    for key, url, source in pending(path, limit=limit):
        yield_to_reader(sleep)
        if source == "fandom":
            # hashed against the issue's covers before it's kept: a match is the
            # same cover at a bigger size, not one more variant
            from kometa import fandom_covers
            row = fandom_covers.download(key, url, path, http=http)
        else:
            row = fetch_image(key, url, source, path, http=http)
        # a failed upgrade hands back the old row (file kept): still a failure
        # as far as "is the CDN down" goes
        if row and not row.get("failed_at"):
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
