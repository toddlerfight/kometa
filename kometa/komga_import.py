"""One-time handover of read progress from Komga (docs/reader-spec.md, shape 1).

Komga knows what you read before Kometa could: 60 finished, 35 in progress at
last count. Its books carry the same /comics/... path our shelf index uses, so
the mapping is exact — no title matching. Each record goes through the normal
stale-rejecting progress write, so a book you've since read further in Kometa
keeps Kometa's progress. Dry run reports the mapping; apply writes it.
"""
import logging
import os
import re

from fastapi import APIRouter

import kometa.db as db
from kometa import sources
from kometa.reader import READER_ID

logger = logging.getLogger(__name__)

router = APIRouter()
DB_PATH = db.DB_PATH
PAGE = 200


def _komga_books(komga, read_status: str):
    page = 0
    while True:
        r = komga._get("/api/v1/books", params={"read_status": read_status, "size": PAGE, "page": page})
        for b in r.get("content", []):
            yield b
        if r.get("last", True) or not r.get("content"):
            return
        page += 1


_YEAR_RE = re.compile(r"\(\d{4}\)")
_NUM_RE = re.compile(r"#\d+(?:\.\d+)?")


def _key(s: str) -> str:
    from kometa.naming import norm_key
    return norm_key(_YEAR_RE.sub(" ", s))


def _renamed_index(path) -> dict:
    """Komga remembers files by the path it last scanned. Folders got renamed
    since ('100 Bullets' → '100 Bullets (1999)', 'Batman- White Knight Presents-
    Harley Quinn (2020)' → 'Batman - White Knight Presents Harley Quinn'), and
    an exact-path handover drops the history on the floor. Second chance: the
    same folder name (years and punctuation aside) + the same issue number; for a
    numberless book (a trade), the same file name. Only a UNIQUE hit counts."""
    by_issue, by_stem = {}, {}
    with db._connect(path) as conn:
        for r in conn.execute("SELECT id, path, number FROM books"):
            folder = os.path.basename(os.path.dirname(r["path"]))
            if r["number"] is not None:
                by_issue.setdefault((_key(folder), float(r["number"])), []).append(r["path"])
            else:
                stem = os.path.splitext(os.path.basename(r["path"]))[0]
                by_stem.setdefault(_key(stem), []).append(r["path"])
    return {"issue": by_issue, "stem": by_stem}


def _renamed_match(url: str, index: dict, path) -> dict | None:
    from kometa.naming import parse_issue_number
    name = os.path.basename(url)
    folder = os.path.basename(os.path.dirname(url))
    n = parse_issue_number(name)
    hits = index["issue"].get((_key(folder), float(n))) if n is not None else None
    if not hits:
        hits = index["stem"].get(_key(_NUM_RE.sub(" ", os.path.splitext(name)[0])))
    return db.get_book_by_path(hits[0], path) if hits and len(hits) == 1 else None


def import_read_history(dry_run: bool = True, komga=None, path=None) -> dict:
    """{matched, written, kept_ours, unmatched: [paths]} — nothing is written on
    a dry run. `komga` is injectable for tests."""
    path = path or DB_PATH
    komga = komga or sources.komga()
    if not komga:
        raise RuntimeError("Komga isn't configured")
    out = {"dry_run": dry_run, "matched": 0, "written": 0, "kept_ours": 0, "unmatched": [], "in_progress": 0, "finished": 0,
           "renamed": []}
    index = None
    for status in ("IN_PROGRESS", "READ"):
        for b in _komga_books(komga, status):
            rp = b.get("readProgress") or {}
            if not rp.get("readDate"):
                continue
            book = db.get_book_by_path(b.get("url") or "", path)
            if not book:
                index = index or _renamed_index(path)
                book = _renamed_match(b.get("url") or "", index, path)
                if not book:
                    out["unmatched"].append(b.get("url"))
                    continue
                out["renamed"].append({"komga": b.get("url"), "now": book["path"]})
            out["matched"] += 1
            out["finished" if rp.get("completed") else "in_progress"] += 1
            if dry_run:
                continue
            page = max(1, int(rp.get("page") or 1))
            ok, _ = db.set_progress(READER_ID, book["id"], page, bool(rp.get("completed")), rp["readDate"], path)
            out["written" if ok else "kept_ours"] += 1
    logger.info(f"Komga history {'dry run' if dry_run else 'import'}: {out['matched']} matched, "
                f"{out['written']} written, {out['kept_ours']} kept ours, {len(out['unmatched'])} unmatched")
    return out


@router.post("/api/reader/import-komga")
def import_komga(dry: int = 1):
    try:
        return import_read_history(dry_run=bool(dry))
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(502, str(e))
