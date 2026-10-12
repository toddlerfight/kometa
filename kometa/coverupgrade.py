"""A bigger copy of the same cover, shown in place of the smaller one.

The Marvel.com worker (1i-e) keeps print-size covers beside the 600–768px ones
the catalogues gave us — same picture, three or four times the width. Kept is
not shown: the tiles and the issue sheet read the original keys. This is the
bridge: for every stored cover, which bigger stored copy shows the same picture
(dHash within coververify.CLOSE), and the cover routes ask here first.

Linked when a bigger copy lands (marveldc.download_pending) and, for copies that
landed before this existed, by backfill() — idempotent, cheap once done.
"""
import io
import logging
import os

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
SOURCES = ("marveldc",)          # whose bigger copies stand in for the originals


def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS image_upgrade (
            key TEXT PRIMARY KEY, better TEXT NOT NULL, pixels INTEGER, at TEXT DEFAULT (datetime('now')))""")
        # every stand-in looked at, linked or not — so backfill never hashes one twice
        conn.execute("CREATE TABLE IF NOT EXISTS image_upgrade_seen (better TEXT PRIMARY KEY)")


def _size(data: bytes) -> tuple[int, int] | None:
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def link(series_id: int, number: float, better_key: str, data: bytes, path=None) -> list[str]:
    """`better_key` (bytes `data`) stands in for every stored cover of this issue
    that's the same picture and smaller — unless an even bigger stand-in is already
    linked. Returns the keys it now stands in for."""
    from kometa import coververify
    from kometa.marveldc import held_covers
    path = path or DB_PATH
    ensure_tables(path)
    size, h = _size(data), coververify.dhash(data)
    if not size or h is None:
        with db._connect(path) as conn:
            conn.execute("INSERT OR IGNORE INTO image_upgrade_seen (better) VALUES (?)", (better_key,))
        return []
    px = size[0] * size[1]
    linked = []
    with db._connect(path) as conn:
        for c in held_covers(series_id, number, path):
            if c["key"] == better_key or coververify.hamming(h, c["dhash"]) > coververify.CLOSE:
                continue
            if c["width"] * c["height"] >= px:
                continue
            cur = conn.execute("SELECT pixels FROM image_upgrade WHERE key = ?", (c["key"],)).fetchone()
            if cur and (cur["pixels"] or 0) >= px:
                continue
            conn.execute("INSERT OR REPLACE INTO image_upgrade (key, better, pixels) VALUES (?, ?, ?)",
                         (c["key"], better_key, px))
            linked.append(c["key"])
        conn.execute("INSERT OR IGNORE INTO image_upgrade_seen (better) VALUES (?)", (better_key,))
    return linked


def better_path(key: str, path=None) -> str | None:
    """The file of the bigger copy standing in for `key`, if one is linked and on disk."""
    path = path or DB_PATH
    try:
        with db._connect(path) as conn:
            r = conn.execute("""SELECT i.path FROM image_upgrade u JOIN image_record i ON i.key = u.better
                WHERE u.key = ? AND i.path IS NOT NULL""", (key,)).fetchone()
    except Exception:            # table not made yet (first start) — nothing linked
        return None
    return r[0] if r and os.path.exists(r[0]) else None


def backfill(path=None, limit: int = 200) -> int:
    """Link stored stand-ins that predate the links (or whose link was lost). Skips
    any already looked at, so a finished backfill costs one query."""
    path = path or DB_PATH
    ensure_tables(path)
    from kometa import images
    images.ensure_tables(path)
    with db._connect(path) as conn:
        rows = conn.execute(f"""SELECT key, path FROM image_record
            WHERE ({' OR '.join("key LIKE ?" for _ in SOURCES)}) AND path IS NOT NULL
              AND key NOT IN (SELECT better FROM image_upgrade_seen) LIMIT ?""",
                            (*[f"issue/%/{s}%" for s in SOURCES], limit)).fetchall()
    n = 0
    for r in rows:
        parts = r["key"].split("/")
        try:
            sid, number = int(parts[1]), float(parts[2])
            with open(r["path"], "rb") as fh:
                data = fh.read()
        except (ValueError, IndexError, OSError):
            continue
        n += len(link(sid, number, r["key"], data, path))
    if n:
        logger.info(f"Cover upgrades: {n} covers now shown from a bigger copy")
    return n
