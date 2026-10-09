"""Proposals: a download you confirm before it's placed.

The name-only trade grab (a reading-list gap no catalogue knows) can find
things but can't verify them — there's no issue list to check a file against.
So it doesn't land in the library. It lands in a holding folder, the row goes
'proposed', and Activity shows what came back: the file's own cover, its page
count and size. Confirm moves it onto the shelf under the series the list
entry created; Reject bins it and remembers the release, so the next try buys
something else.
"""
import json
import logging
import os
import re
import shutil

from fastapi import APIRouter, HTTPException, Response

import kometa.db as db
from kometa import sources

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
PROPOSAL_LOCG_SENTINEL = -2          # a trade row searched by NAME, not by a catalogue id


def holding_dir(qid: int) -> str:
    return os.path.join(sources.staging_dir(), "proposals", str(qid))


def propose(qid: int, placed: list[str], source_url: str | None, path=None) -> dict:
    """The download is in the holding folder: record what it is and ask."""
    path = path or DB_PATH
    from kometa.naming import _archive_entry_names, _IMAGE_EXTS
    files = []
    for p in placed:
        names = _archive_entry_names(p) or []
        files.append({"path": p, "size": os.path.getsize(p) if os.path.exists(p) else 0,
                      "pages": sum(1 for n in names if n.lower().endswith(_IMAGE_EXTS))})
    db.set_queue_meta(qid, {"proposal": {"files": files, "source_url": source_url}}, path)
    db.update_queue_state(qid, "proposed", source_url=source_url, path=path)
    logger.info(f"Proposal {qid}: {len(files)} file(s) held for confirmation")
    return {"files": files}


def _row(qid: int, path):
    row = next((q for q in db.get_queue(path) if q["id"] == qid), None)
    if not row or row.get("state") != "proposed":
        raise HTTPException(404, "No proposal waiting on that row")
    return row


def confirm(qid: int, path=None) -> dict:
    """Place the held file(s) in the series folder, named for the series, and
    mark the trade done — the same end state a verified trade reaches."""
    path = path or DB_PATH
    row = _row(qid, path)
    meta = json.loads(row.get("meta_json") or "{}")
    series = db.get_series_by_id(row["tracked_series_id"], path)
    if not series or not series.get("folder_path"):
        raise HTTPException(400, "The series has no folder")
    dest_dir = series["folder_path"]
    os.makedirs(dest_dir, exist_ok=True)
    year = meta.get("year")
    placed = []
    files = (meta.get("proposal") or {}).get("files") or []
    for i, f in enumerate(files):
        src = f["path"]
        if not os.path.exists(src):
            continue
        ext = os.path.splitext(src)[1].lower()
        stem = re.sub(r"\s*\(\d{4}\)\s*$", "", series["title"]).strip()
        suffix = f" {i + 1:02d}" if len(files) > 1 else ""
        dst = os.path.join(dest_dir, f"{stem}{f' ({year})' if year else ''}{suffix}{ext}")
        if os.path.exists(dst):
            dst = os.path.join(dest_dir, f"{stem}{f' ({year})' if year else ''}{suffix} (proposal {qid}){ext}")
        shutil.move(src, dst)
        placed.append(dst)
    shutil.rmtree(holding_dir(qid), ignore_errors=True)
    try:
        from kometa.downloader import force_readable_tree
        force_readable_tree(dest_dir)
    except Exception:
        pass
    db.set_queue_meta(qid, {"proposal": {**(meta.get("proposal") or {}), "placed": placed}}, path)
    db.complete_trade(qid, path)
    try:
        from kometa.sync import rescan_owned
        rescan_owned(db.get_series_by_id(series["id"], path))
        from kometa.shelf import scan_shelf_safe
        scan_shelf_safe()
    except Exception as e:
        logger.info(f"Proposal {qid}: post-confirm rescan skipped: {e}")
    logger.info(f"Proposal {qid} confirmed: {placed}")
    return {"placed": placed, "series_id": series["id"]}


def reject(qid: int, path=None) -> dict:
    """Bin the held file(s), remember the release, let the row be retried."""
    path = path or DB_PATH
    row = _row(qid, path)
    meta = json.loads(row.get("meta_json") or "{}")
    src_url = (meta.get("proposal") or {}).get("source_url") or row.get("source_url")
    if src_url:
        db.add_failed_source(qid, src_url, path)
    shutil.rmtree(holding_dir(qid), ignore_errors=True)
    db.set_queue_meta(qid, {"proposal": None}, path)
    db.update_queue_state(qid, "not_found", error="Rejected — that wasn't it", path=path)
    logger.info(f"Proposal {qid} rejected ({src_url})")
    return {"ok": True}


@router.post("/api/queue/{qid}/confirm")
def api_confirm(qid: int):
    return confirm(qid)


@router.post("/api/queue/{qid}/reject")
def api_reject(qid: int):
    return reject(qid)


@router.get("/api/queue/{qid}/proposal-cover")
def api_proposal_cover(qid: int):
    row = _row(qid, DB_PATH)
    meta = json.loads(row.get("meta_json") or "{}")
    files = (meta.get("proposal") or {}).get("files") or []
    if not files or not os.path.exists(files[0]["path"]):
        raise HTTPException(404)
    from kometa.reader import get_cover_bytes
    try:
        return Response(content=get_cover_bytes(files[0]["path"]), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})
    except Exception:
        raise HTTPException(422)
