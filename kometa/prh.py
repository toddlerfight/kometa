"""Penguin Random House's cover CDN — print-quality covers by barcode (enrichment 1i).

PRH distributes Marvel, IDW, BOOM! and Dark Horse, and its image server hands
out the print TIFF (1400–2000 px wide, 18–45 MB) for a 17-digit UPC (12 digits
plus the 5-digit add-on) or a 13-digit ISBN, no key needed
(docs/research/comic-sources-2026-10-12.md §1.2). Two catches shape this module:

- A code PRH doesn't have still answers 200 — with a placeholder image. So the
  small JPEG (/cover/{code}, ~13 KB) is fetched first and hashed against the
  placeholder (itself fetched once with a bogus code and remembered); only a
  real cover earns the 45 MB TIFF. HEAD is no help: the TIFF path says
  image/tiff with no length for the placeholder too (probed 2026-10-12).
- PRH's terms forbid robots. Using it anyway is the user's call (2026-10-12),
  on the condition that it's slow: one request every 2 s, 300 a day, and the
  TIFF is turned into a JPEG on arrival — never kept.

The UPC comes from Metron (issue_record.extra 'upc'); add-ons are never
invented — the cover/printing convention isn't reliable. What was asked is
written into extra 'prh' (the TIFF URL, or None) so nothing is asked twice.
"""
import hashlib
import io
import json
import logging
import re

import kometa.db as db
from kometa.polite import Limiter, SourceUnavailable, enabled

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH

BASE = "https://images.penguinrandomhouse.com/cover/"
BOGUS_CODE = "00000000000000000"       # 17 zeros: answers with the placeholder, for good
PER_TICK = 6                           # requests per 10-min tick; the daily cap stops the day long before 144 ticks
MIN_WIDTH = 900                        # a 'print' cover narrower than this isn't worth keeping over Metron's 768
MAX_BYTES = 80 * 1024 * 1024           # the biggest TIFF seen was 45 MB
JPEG_QUALITY = 92
CONFIG_KEY = "prh_placeholder_md5"

limiter = Limiter("PRH", min_interval_s=2.0, daily_cap=300, timeout_s=90,
                  refused_pause_s=6 * 3600)
_placeholder = {"md5": None}


def code_for(extra: dict | None, isbn: str | None) -> str | None:
    """The key PRH files a cover under: a 17-digit UPC, else a 13-digit ISBN.
    A 12-digit UPC without its add-on is no key at all (PRH would hand back
    the placeholder, or worse, another printing's cover)."""
    upc = re.sub(r"\D", "", str((extra or {}).get("upc") or ""))
    if len(upc) == 17:
        return upc
    digits = re.sub(r"\D", "", str(isbn or ""))
    if len(digits) == 13 and digits[:3] in ("978", "979"):
        return digits
    return None


def placeholder_md5(path=None) -> str:
    """The placeholder's hash: config first, else one request with the bogus
    code (through the throttle) and it's remembered for good."""
    if _placeholder["md5"]:
        return _placeholder["md5"]
    path = path or DB_PATH
    stored = (db.get_config(path).get(CONFIG_KEY) or "").strip()
    if stored:
        _placeholder["md5"] = stored
        return stored
    r = limiter.get(BASE + BOGUS_CODE)
    if r.status_code != 200 or not r.content:
        raise SourceUnavailable(f"PRH placeholder answered {r.status_code}")
    md5 = hashlib.md5(r.content).hexdigest()
    db.set_config({CONFIG_KEY: md5}, path)
    _placeholder["md5"] = md5
    return md5


def to_jpeg(data: bytes) -> tuple[bytes, int, int] | None:
    """A TIFF (or anything Pillow reads) → a full-resolution JPEG. CMYK print
    files go through their embedded ICC profile to sRGB when there is one — the
    naive conversion washes the colours out. None when it isn't an image."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        im.seek(0)
        im.load()
    except Exception as e:
        logger.info(f"PRH: not an image ({e.__class__.__name__})")
        return None
    if im.mode == "CMYK" and im.info.get("icc_profile"):
        try:
            from PIL import ImageCms
            src = ImageCms.ImageCmsProfile(io.BytesIO(im.info["icc_profile"]))
            im = ImageCms.profileToProfile(im, src, ImageCms.createProfile("sRGB"), outputMode="RGB")
        except Exception:
            im = im.convert("RGB")
    elif im.mode != "RGB":
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue(), im.width, im.height


def _body(r) -> bytes | None:
    """A streamed body, refused past MAX_BYTES."""
    buf, n = io.BytesIO(), 0
    for chunk in r.iter_content(1 << 20):
        n += len(chunk)
        if n > MAX_BYTES:
            r.close()
            return None
        buf.write(chunk)
    return buf.getvalue()


def candidates(path=None, limit: int = 50) -> list[dict]:
    """Owned issues whose record carries a PRH-able code and that PRH hasn't
    been asked about. Pull list first, then the rest. Extra must exist: the ask
    is remembered there (record.merge_extra writes nowhere else)."""
    path = path or DB_PATH
    from kometa import record
    record.ensure_tables(path)
    with db._connect(path) as conn:
        rows = conn.execute("""
            SELECT r.tracked_series_id AS sid, r.number, r.isbn, r.extra_json
            FROM issue_record r JOIN issue_status i ON i.tracked_series_id = r.tracked_series_id AND i.number = r.number
            JOIN tracked_series s ON s.id = r.tracked_series_id
            WHERE i.owned = 1 AND r.extra_json IS NOT NULL AND (s.kind IS NULL OR s.kind != 'arc')
              AND (r.extra_json LIKE '%"upc": "%' OR COALESCE(r.isbn, '') != '')
            ORDER BY s.on_pull_list DESC, r.tracked_series_id, r.number""").fetchall()
    out = []
    for r in rows:
        extra = json.loads(r["extra_json"] or "{}")
        if "prh" in extra:
            continue                            # asked already (None = PRH has no cover)
        code = code_for(extra, r["isbn"])
        if code:
            out.append({"sid": r["sid"], "number": r["number"], "code": code})
            if len(out) >= limit:
                break
    return out


def fetch_cover(code: str, path=None, spend=lambda: True) -> tuple[str | None, bytes | None]:
    """(tif_url, jpeg) for a real cover; (None, None) when PRH hasn't got it.
    Raises SourceUnavailable when PRH is resting/refusing or the tick's
    requests ran out mid-way (nothing is recorded then — asked again later)."""
    ph = placeholder_md5(path)
    if not spend():
        raise SourceUnavailable("PRH: this tick's requests are spent")
    small = limiter.get(BASE + code)
    if small.status_code == 404 or not small.content:
        return None, None
    if small.status_code != 200:
        raise SourceUnavailable(f"PRH answered {small.status_code}")
    if hashlib.md5(small.content).hexdigest() == ph:
        return None, None
    if not spend():
        raise SourceUnavailable("PRH: this tick's requests are spent")
    from kometa.activity import yield_to_reader
    yield_to_reader(limiter.sleep)                   # 45 MB doesn't go down the pipe while someone reads
    tif_url = f"{BASE}tif/{code}"
    big = limiter.get(tif_url, stream=True)
    if big.status_code != 200:
        return None, None
    data = _body(big)
    if not data:
        logger.info(f"PRH {code}: TIFF over {MAX_BYTES // 2**20} MB, skipped")
        return None, None
    if hashlib.md5(data).hexdigest() == ph:
        return None, None
    conv = to_jpeg(data)
    if not conv:
        return None, None
    jpeg, w, h = conv
    if w < MIN_WIDTH:
        logger.info(f"PRH {code}: only {w}px wide, not kept")
        return None, None
    logger.info(f"PRH {code}: {w}x{h}, {len(data) // 2**20} MB TIFF → {len(jpeg) // 1024} KB JPEG")
    return tif_url, jpeg


def tick(budget: int = PER_TICK, path=None) -> int:
    """Scheduler tick: owned issues' print covers, a few requests at a time,
    into the image store at issue/<sid>/<n>/prh. A no-op with the Settings
    toggle off, a spent day or a pause. Returns covers stored."""
    path = path or DB_PATH
    if not enabled("prh_enabled", path) or limiter.spent():
        return 0
    from kometa import images, record
    left = [budget]

    def spend() -> bool:
        if left[0] <= 0:
            return False
        left[0] -= 1
        return True

    stored = 0
    try:
        for c in candidates(path, limit=budget):
            url, jpeg = fetch_cover(c["code"], path, spend)
            if jpeg:
                images.store_bytes(images.prh_key(c["sid"], c["number"]), jpeg, url, "prh", path)
                stored += 1
            record.merge_extra(c["sid"], c["number"], {"prh": url if jpeg else None}, path)
    except SourceUnavailable as e:
        logger.info(f"PRH covers stopped this tick: {e}")
    if stored:
        logger.info(f"PRH: {stored} print covers stored")
    return stored


def status(path=None) -> dict:
    path = path or DB_PATH
    from kometa import record
    record.ensure_tables(path)
    with db._connect(path) as conn:
        found = conn.execute("""SELECT COUNT(*) FROM issue_record WHERE extra_json LIKE '%"prh": "http%'""").fetchone()[0]
        none = conn.execute("""SELECT COUNT(*) FROM issue_record WHERE extra_json LIKE '%"prh": null%'""").fetchone()[0]
    return {"found": found, "none": none, "budget": limiter.budget(), "enabled": enabled("prh_enabled", path)}
