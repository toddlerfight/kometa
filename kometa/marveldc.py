"""What's left of the Marvel.com cover scraper (enrichment 1i-e, retired 2026-10-15:
every candidate failed the UPC match; the scraper core went 2026-10-29). Kept: the
night-window helpers cbr_people runs on, and held_covers for coverupgrade — the covers
the scraper did fetch still sit in image_record under .../marveldc and still show."""
import io
import os
import re

import kometa.db as db

DB_PATH = db.DB_PATH


# --- the night window ------------------------------------------------------------------
def parse_hours(spec: str | None) -> tuple[int, int]:
    """'23-7' → (23, 7). Anything unreadable falls back to the default window."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", str(spec or ""))
    if m and 0 <= int(m[1]) <= 23 and 0 <= int(m[2]) <= 23:
        return int(m[1]), int(m[2])
    return 0, 0                               # DEFAULT_HOURS: all day


def valid_hours(spec: str | None) -> bool:
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", str(spec or ""))
    return bool(m and 0 <= int(m[1]) <= 23 and 0 <= int(m[2]) <= 23)


def in_window(hour: int, start: int, end: int) -> bool:
    """Start inclusive, end exclusive; wraps midnight when start > end. start == end = all day."""
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


# --- the covers it did bring back ------------------------------------------------------
def held_covers(series_id: int, number: float, path=None) -> list[dict]:
    """The issue's stored covers from every OTHER source: {key, dhash, width, height}."""
    from kometa import coververify, images
    from PIL import Image
    path = path or DB_PATH
    images.ensure_tables(path)
    prefix = f"issue/{int(series_id)}/{images.num(number)}/"
    with db._connect(path) as conn:
        rows = conn.execute("SELECT key, path FROM image_record WHERE key LIKE ? AND path IS NOT NULL",
                            (prefix + "%",)).fetchall()
    out = []
    for r in rows:
        k = r["key"]
        if k.startswith(prefix + "marveldc") or not os.path.exists(r["path"]):
            continue
        try:
            with open(r["path"], "rb") as f:
                data = f.read()
            with Image.open(io.BytesIO(data)) as im:
                w, h = im.size
        except Exception:
            continue
        dh = coververify.dhash(data)
        if dh is not None:
            out.append({"key": k, "dhash": dh, "fine": coververify.fine(data), "width": w, "height": h})
    return out
