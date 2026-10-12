"""A bigger copy of the same cover, shown in place of the smaller one.

The Marvel.com worker (1i-e) keeps print-size covers beside the 600–768px ones
the catalogues gave us — same picture, three or four times the width. Kept is
not shown: the tiles and the issue sheet read the original keys. This is the
bridge: for every stored cover, which bigger stored copy shows the same picture
(coververify.same_image: the same image, only bigger — a textless or
re-dressed printing of the art is NOT the same cover), and the cover routes ask here first.

Linked when a bigger copy lands (marveldc.download_pending) and, for copies that
landed before this existed, by backfill() — idempotent, cheap once done.
"""
import io
import re
import logging
import os

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
SOURCES = ("marveldc",)          # whose bigger copies stand in for the originals


def ensure_tables(path=None):
    # cover_upgrade, not image_upgrade: the first links (2026-10-13 01:47) used the loose
    # 8×8 test and paired textless virgins with logo covers; that table is left unread
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS cover_upgrade (
            key TEXT PRIMARY KEY, better TEXT NOT NULL, pixels INTEGER, at TEXT DEFAULT (datetime('now')))""")
        # every stand-in looked at, linked or not — so backfill never hashes one twice
        conn.execute("CREATE TABLE IF NOT EXISTS cover_upgrade_seen (better TEXT PRIMARY KEY)")


RELINK_FLAG = "coverupgrade_bottom_v1"


def relink_once(path=None) -> bool:
    """Once: forget every link and look again with the bottom-band test — Marvel.com's
    own copy of some runs carries a 'MARVEL.com' footer strip, and nine of those
    stood in for file covers before coververify noticed. Also drops image_upgrade,
    the first (loose 8x8) attempt nobody reads. True when it ran."""
    path = path or DB_PATH
    if db.get_config(path).get(RELINK_FLAG) == "1":
        return False
    ensure_tables(path)
    with db._connect(path) as conn:
        conn.execute("DROP TABLE IF EXISTS image_upgrade")
        conn.execute("DELETE FROM cover_upgrade")
        conn.execute("DELETE FROM cover_upgrade_seen")
    db.set_config({RELINK_FLAG: "1"}, path)
    logger.info("Cover upgrades: links cleared for a re-look with the bottom-band test")
    return True


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
    size, f = _size(data), coververify.fine(data)
    if not size or f is None:
        with db._connect(path) as conn:
            conn.execute("INSERT OR IGNORE INTO cover_upgrade_seen (better) VALUES (?)", (better_key,))
        return []
    px = size[0] * size[1]
    linked = []
    with db._connect(path) as conn:
        for c in held_covers(series_id, number, path):
            if c["key"] == better_key or not coververify.same_image(f, c["fine"]):
                continue
            if c["width"] * c["height"] >= px:
                continue
            cur = conn.execute("SELECT pixels FROM cover_upgrade WHERE key = ?", (c["key"],)).fetchone()
            if cur and (cur["pixels"] or 0) >= px:
                continue
            conn.execute("INSERT OR REPLACE INTO cover_upgrade (key, better, pixels) VALUES (?, ?, ?)",
                         (c["key"], better_key, px))
            linked.append(c["key"])
        conn.execute("INSERT OR IGNORE INTO cover_upgrade_seen (better) VALUES (?)", (better_key,))
    return linked


def better_path(key: str, path=None) -> str | None:
    """The file of the bigger copy standing in for `key`, if one is linked and on disk."""
    path = path or DB_PATH
    try:
        with db._connect(path) as conn:
            r = conn.execute("""SELECT i.path FROM cover_upgrade u JOIN image_record i ON i.key = u.better
                WHERE u.key = ? AND i.path IS NOT NULL""", (key,)).fetchone()
    except Exception:            # table not made yet (first start) — nothing linked
        return None
    return r[0] if r and os.path.exists(r[0]) else None


def backfill(path=None, limit: int = 200) -> int:
    """Link stored stand-ins that predate the links (or whose link was lost). Skips
    any already looked at, so a finished backfill costs one query."""
    path = path or DB_PATH
    ensure_tables(path)
    relink_once(path)
    from kometa import images
    images.ensure_tables(path)
    with db._connect(path) as conn:
        rows = conn.execute(f"""SELECT key, path FROM image_record
            WHERE ({' OR '.join("key LIKE ?" for _ in SOURCES)}) AND path IS NOT NULL
              AND key NOT IN (SELECT better FROM cover_upgrade_seen) LIMIT ?""",
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


MIN_OWN_ENTRY_W = 1000   # Marvel's 553x850 copies are smaller than the catalogue's AND carry the footer


def own_entries(series_id: int, number: float, path=None) -> list[dict]:
    """Marvel.com's variant covers that aren't an exact copy of any catalogue cover
    (those already show as `big` on that cover), as Variants-tab entries of their own.
    Print-size only: the small digital copies wear a 'MARVEL.com' strip and add nothing.
    `large` stays Marvel's URL, so picking one injects it like any catalogue variant."""
    import json
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        try:
            row = conn.execute("SELECT extra_json FROM issue_record WHERE tracked_series_id = ? AND number = ?",
                               (series_id, number)).fetchone()
        except Exception:        # no record table yet — nothing harvested, nothing to add
            return []
        linked = {r[0] for r in conn.execute("SELECT DISTINCT better FROM cover_upgrade WHERE better LIKE ?",
                                             (f"issue/{int(series_id)}/{number:g}/%",))}
    try:
        imgs = (json.loads((row and row["extra_json"]) or "{}").get("marveldc") or {}).get("images") or []
    except ValueError:
        return []
    out = []
    for i in imgs:
        k = i.get("key") or ""
        if (i.get("status") != "kept" or i.get("kind") != "variant" or k in linked
                or (i.get("width") or 0) < MIN_OWN_ENTRY_W or not i.get("url")):
            continue
        vid = k.rsplit("/", 1)[-1]
        out.append({"id": f"marvel-{vid}", "name": f"Marvel.com variant {vid}", "thumb": f"/api/cover-store/{k}?w=400",
                    "large": i["url"], "big": f"/api/cover-store/{k}", "type": "cover", "source": "marvel.com",
                    "local": None})
    return out


def stored_marvel_path(key: str, path=None) -> str | None:
    """A stored Marvel.com copy's file, for the Variants tab. Marvel keys only."""
    if not re.fullmatch(r"issue/\d+/[0-9.]+/marveldc(/[A-Za-z0-9_.-]+)?", key or ""):
        return None
    from kometa import images
    p = images.local_path(key, path or DB_PATH)
    return p if p and os.path.exists(p) else None
