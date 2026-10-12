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
import json
import logging
import os
import threading
import time

from fastapi import APIRouter, HTTPException

import kometa.db as db

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
CLOSE = 10            # bits: at or under = the same picture
FETCH_GAP_S = 0.3     # catalogue images, one at a time
_running: dict[int, bool] = {}


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


def fine(data: bytes) -> tuple[int, int] | None:
    """(whole-cover, top-band) 16×16 hashes."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            g = im.convert("L")
            w, h = g.size
            top = g.crop((0, 0, w, max(1, int(h * TOP_BAND))))
            out = []
            for part in (g, top):
                buf = io.BytesIO()
                part.save(buf, "PNG")
                out.append(dhash(buf.getvalue(), FINE))
    except Exception:
        return None
    return None if None in out else (out[0], out[1])


def same_image(a: tuple[int, int] | None, b: tuple[int, int] | None) -> bool:
    return bool(a and b) and hamming(a[0], b[0]) <= FINE_SAME and hamming(a[1], b[1]) <= FINE_SAME


def _report_path(series_id: int, path=None) -> str:
    return os.path.join(os.path.dirname(path or DB_PATH) or ".", "verify", f"covers-{series_id}.json")


def _file_covers(series_id: int, path) -> dict[float, str]:
    """number → page-1 image the covers trickle already took from the file."""
    out = {}
    with db._connect(path) as conn:
        for r in conn.execute("SELECT key, path FROM image_record WHERE key LIKE ? AND path IS NOT NULL",
                              (f"issue/{series_id}/%/file",)):
            try:
                n = float(r["key"].split("/")[2])
            except (IndexError, ValueError):
                continue
            if os.path.exists(r["path"]):
                out[n] = r["path"]
    return out


def catalogue_covers(series_id: int, metron_ids=(), path=None) -> dict[float, list[str]]:
    """number → catalogue cover URLs: what the record already holds, plus the
    issue lists of any Metron runs named (one run can span two of Metron's —
    The X-Men 1963 #1–141, The Uncanny X-Men 1981 #142–544)."""
    path = path or DB_PATH
    out: dict[float, list[str]] = {}
    for i in db.get_issues_for_series(series_id, path):
        if (i.get("metron_image") or "").startswith("http"):
            out.setdefault(float(i["number"]), []).append(i["metron_image"])
    if metron_ids:
        from kometa import metron_client
        for mid in metron_ids:
            for mi in metron_client.series_issues(int(mid)):
                if mi.get("image"):
                    out.setdefault(float(mi["number"]), []).append(mi["image"])
    return out


def verify(series_id: int, metron_ids=(), path=None, fetch=None, sleep=time.sleep) -> dict:
    path = path or DB_PATH
    files = _file_covers(series_id, path)
    cat_urls = catalogue_covers(series_id, metron_ids, path)
    if fetch is None:
        from kometa.images import _download, best_url
        fetch = lambda u: _download(best_url(u))
    cat: list[tuple[float, int]] = []
    for n, urls in sorted(cat_urls.items()):
        for u in dict.fromkeys(urls):
            try:
                data = fetch(u)
            except Exception:
                data = None
            h = dhash(data) if data else None
            if h is not None:
                cat.append((n, h))
            sleep(FETCH_GAP_S)
    rows = []
    for n, p in sorted(files.items()):
        with open(p, "rb") as fh:
            h = dhash(fh.read())
        if h is None:
            rows.append({"number": n, "status": "unclear", "why": "file cover unreadable"})
            continue
        own = min((hamming(h, ch) for cn, ch in cat if cn == n), default=None)
        best_n, best_d = min(((cn, hamming(h, ch)) for cn, ch in cat), key=lambda x: x[1], default=(None, None))
        if own is not None and own <= CLOSE:
            rows.append({"number": n, "status": "match", "distance": own})
        elif best_d is not None and best_d <= CLOSE:
            rows.append({"number": n, "status": "misnumbered", "looks_like": best_n, "distance": best_d,
                         "own_distance": own})
        else:
            rows.append({"number": n, "status": "unclear", "own_distance": own, "nearest": best_n, "nearest_distance": best_d,
                         "why": "no catalogue cover for this number" if own is None else "no close match"})
    summary = {s: sum(1 for r in rows if r["status"] == s) for s in ("match", "misnumbered", "unclear")}
    report = {"series_id": series_id, "metron_ids": list(metron_ids), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "files": len(files), "catalogue_numbers": len(cat_urls), "summary": summary, "rows": rows}
    out = _report_path(series_id, path)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(report, fh, indent=1)
    return report


@router.post("/api/series/{series_id}/verify-covers")
def start_verify(series_id: int, metron: str = ""):
    """Runs in the app (the one Metron throttle), in the background; GET for the report."""
    if not db.get_series_by_id(series_id, DB_PATH):
        raise HTTPException(404)
    if _running.get(series_id):
        return {"started": False, "running": True}
    ids = [int(x) for x in metron.split(",") if x.strip().isdigit()]

    def run():
        _running[series_id] = True
        try:
            verify(series_id, ids)
        except Exception as e:
            logger.warning(f"Cover check for series {series_id} failed: {e}")
        finally:
            _running[series_id] = False
    threading.Thread(target=run, daemon=True).start()
    return {"started": True}


@router.get("/api/series/{series_id}/verify-covers")
def get_verify(series_id: int):
    p = _report_path(series_id)
    if not os.path.exists(p):
        return {"running": bool(_running.get(series_id)), "report": None}
    with open(p) as fh:
        return {"running": bool(_running.get(series_id)), "report": json.load(fh)}
