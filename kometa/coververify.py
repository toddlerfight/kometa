"""Which issue is this file, really? Ask the cover.

A file name is somebody's claim. The Uncanny X-Men folder (2026-10-12) had 413
files stamped '(2009)' beside numbers that ran from #1 to #414 — either the
year or the number was lying, and the name alone can't say which. The cover can:
page 1 of the file against the catalogue's cover for the issue it claims to be.

Fingerprints are 64-bit difference hashes (dHash) — the same picture at another
size or JPEG quality lands a few bits apart; two different covers land ~30 apart.
Per file:
  match       its cover is the catalogue's cover for its own number
  misnumbered its cover is clearly ANOTHER issue's — that number is suggested
  unclear     no close match anywhere: a variant cover, a missing catalogue
              image, or a cover page the scanner skipped. A person looks.
Read-only: nothing is renamed, nothing in the record changes.
"""
import io
import logging

from fastapi import APIRouter

import kometa.db as db

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
CLOSE = 10            # bits: at or under = the same picture
FETCH_GAP_S = 0.3     # catalogue images, one at a time


def dhash(data: bytes, size: int = 8) -> int | None:
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            g = im.convert("L").resize((size + 1, size), Image.LANCZOS)
            px = list(g.getdata())
    except Exception:
        return None
    bits = 0
    for row in range(size):
        for col in range(size):
            left, right = px[row * (size + 1) + col], px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# "The same image, only bigger" — stricter than CLOSE, which only says "this file is
# that issue". Measured 2026-10-13 on 38 Marvel.com pairs: a true resize scores 0–3
# on a 16×16 hash of the whole cover AND of its top band (where the logo sits); a
# textless virgin vs its logo cover, a quote-and-credits printing vs the clean
# solicit, or a neighbouring variant all scored 20+ on one or the other.
FINE = 16
FINE_SAME = 8
TOP_BAND = 0.22
# and the bottom: Marvel.com's own copies of some runs carry a white 'Title (2025) #2 /
# MARVEL.com' strip along the foot. Measured 2026-10-13: true resizes 0–8 on the
# bottom 8%, the strip 15–42. Whole and top bands don't notice it at all.
BOTTOM_BAND = 0.08
BOTTOM_SAME = 10


def fine(data: bytes) -> tuple[int, int, int] | None:
    """(whole-cover, top-band, bottom-band) 16×16 hashes."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            g = im.convert("L")
            w, h = g.size
            top = g.crop((0, 0, w, max(1, int(h * TOP_BAND))))
            bottom = g.crop((0, min(h - 1, int(h * (1 - BOTTOM_BAND))), w, h))
            out = []
            for part in (g, top, bottom):
                buf = io.BytesIO()
                part.save(buf, "PNG")
                out.append(dhash(buf.getvalue(), FINE))
    except Exception:
        return None
    return None if None in out else tuple(out)


def same_image(a: tuple | None, b: tuple | None) -> bool:
    return (bool(a and b) and hamming(a[0], b[0]) <= FINE_SAME and hamming(a[1], b[1]) <= FINE_SAME
            and hamming(a[2], b[2]) <= BOTTOM_SAME)



