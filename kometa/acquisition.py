"""Acquisition pipeline — the download state machine.

Pulls queued issues, tries GetComics then falls back to Usenet (newznab search
-> SABnzbd), finalizes completed Usenet jobs, sweeps for missing issues, and
retries on release days. Owns dl_progress, the live progress map the UI polls;
main imports it back for the progress routes.
"""
import os
import logging
import threading
from datetime import date

import kometa.db as db
import kometa.downloader as downloader
from kometa.sources import (
    komga as _komga, sabnzbd as _sabnzbd, usenet_indexers as _usenet_indexers,
    comics_root as _comics_root,
)
from kometa.usenet_client import search_usenet, search_usenet_pack, PACK_THRESHOLD
from kometa.getcomics_client import GetComicsClient, GCRateLimitError
from kometa.downloader import DuplicateIssueError
from kometa.sabnzbd_client import find_comics_in_dir

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH

# Live download progress, keyed by queue id. Shared mutable state: download
# threads (here AND in main's download-from-url route) write it, the queue
# routes read it. Nobody touches this dict directly across a module boundary —
# go through set_progress/clear_progress/get_progress so the contract stays in
# one place: {"done": int, "total": int} while downloading, gone when finished.
_dl_progress: dict[int, dict] = {}


def set_progress(qid: int, done: int, total: int) -> None:
    _dl_progress[qid] = {"done": done, "total": total}


def clear_progress(qid: int) -> None:
    _dl_progress.pop(qid, None)


# Live "where is the search right now" line per queue item — same in-memory
# pattern as _dl_progress. The UI polls it; a 'searching' chip with no detail
# is just a spinner with better posture.
_search_status: dict[int, str] = {}


def set_search_status(qid: int, text: str) -> None:
    _search_status[qid] = text


def get_search_status(qid: int) -> str | None:
    return _search_status.get(qid)


def clear_search_status(qid: int) -> None:
    _search_status.pop(qid, None)


def get_progress(qid: int) -> dict | None:
    return _dl_progress.get(qid)


def _komga_scan():
    komga = _komga()
    if komga:
        komga.scan_library()


# Five call sites spawn this in threads (scheduler tick, manual retries, bulk
# re-search). Two passes racing the same queue rows = double downloads — one
# run at a time, late arrivals skip out and the next tick picks up their rows.
_queue_run_lock = threading.Lock()


def _process_queue():
    if not _queue_run_lock.acquire(blocking=False):
        return
    try:
        _process_queue_locked()
    finally:
        _queue_run_lock.release()


def _process_queue_locked():
    items = db.get_queued_items(DB_PATH)
    if not items:
        return
    gc = GetComicsClient()
    downloaded_urls = set()
    for item in items:
        qid = item["id"]
        db.update_queue_state(qid, "searching", path=DB_PATH)
        try:
            issues = db.get_issues_for_series(item["tracked_series_id"], DB_PATH)
            issue_row = next((i for i in issues if i["number"] == item["issue_number"]), None)
            store_date = issue_row["store_date"] if issue_row else None

            set_search_status(qid, "GetComics…")
            dl_url, hint_filename = gc.search(item["title"], item["issue_number"], store_date, series_year=item.get("year_began"),
                                              status_fn=lambda s, qid=qid: set_search_status(qid, s))
            if not dl_url:
                # GetComics failed — try Usenet indexers before giving up
                indexers = _usenet_indexers()
                sab = _sabnzbd()
                if indexers and sab:
                    set_search_status(qid, "Usenet: " + ", ".join(ix.get("name", "?") for ix in indexers))
                    nzb_url = search_usenet(indexers, item["title"], item["issue_number"])
                    if nzb_url:
                        nzo_id = sab.add_nzb_url(nzb_url, nzb_name=f"{item['title']} #{int(item['issue_number'])}")
                        if nzo_id:
                            db.update_queue_state(qid, "pending_usenet",
                                                  source_url=nzb_url, path=DB_PATH)
                            db.set_sab_nzo_id(qid, nzo_id, path=DB_PATH)
                            logger.info(f"Usenet: submitted nzo_id={nzo_id} for {item['title']} #{int(item['issue_number'])}")
                            continue
                db.update_queue_state(qid, "not_found", error="No result on GetComics or Usenet", path=DB_PATH)
                continue

            if dl_url in downloaded_urls:
                db.update_queue_state(qid, "not_found", error="Pack already downloaded for this series", path=DB_PATH)
                continue
            downloaded_urls.add(dl_url)

            db.update_queue_state(qid, "downloading", source_url=dl_url, path=DB_PATH)
            dest = downloader.download_issue(
                url=dl_url,
                title=item["title"],
                publisher=item["publisher"],
                issue_number=item["issue_number"],
                store_date=store_date,
                hint_filename=hint_filename,
                komga_scan_fn=_komga_scan,
                progress_fn=lambda done, total, qid=qid: set_progress(qid, done, total),
                dest_dir=item.get("folder_path") or None,
                tracked_series_id=item["tracked_series_id"],
                db_path=DB_PATH,
            )
            clear_progress(qid)
            # Mark done + record ownership in one transaction — no crash-gap re-download.
            # folder_path auto-populated so the next sync's folder scan finds the file.
            # komga_book_id stays NULL until next full sync (only needed for thumbnails).
            db.complete_download(
                qid, item["tracked_series_id"], item["issue_number"], store_date,
                filename=dest,
                set_folder_path=os.path.dirname(dest) if not item.get("folder_path") else None,
                path=DB_PATH,
            )
        except GCRateLimitError as e:
            from datetime import datetime, timedelta
            # A rate limit is "not yet", not "failed" — park the job and let the
            # 5-minute queue worker pick it back up after the cooldown. Real 429s
            # bump the attempt counter; gate refusals (no HTTP ever happened)
            # don't — punishing a job for the pipeline being closed is just mean.
            attempts = 0 if e.from_gate else db.bump_rl_attempts(qid, path=DB_PATH)
            if attempts >= 6:
                db.update_queue_state(
                    qid, "failed",
                    error=f"GetComics rate limit persisted across {attempts} retries — giving up",
                    path=DB_PATH)
                logger.warning(f"Queue item {qid}: rate-limit retry cap hit — failed for real")
            else:
                cooldown = max(int(e.retry_after or 0), 15 * 60)
                retry_at = (datetime.utcnow() + timedelta(seconds=cooldown)).strftime("%Y-%m-%d %H:%M:%S")
                db.update_queue_state(
                    qid, "queued",
                    error="Rate limited by GetComics — parked, will retry automatically",
                    retry_after=retry_at, path=DB_PATH)
                logger.info(f"Queue item {qid}: rate limited — parked until {retry_at} UTC")
            break  # we're blocked either way — stop hammering with the rest of the queue
        except DuplicateIssueError as e:
            from datetime import datetime, timedelta
            retry_at = (datetime.utcnow() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
            db.update_queue_state(qid, "queued", error=str(e), retry_after=retry_at, path=DB_PATH)
            logger.info(f"Duplicate detected for queue item {qid} — requeueing, retry after {retry_at}")
        except Exception as e:
            db.update_queue_state(qid, "failed", error=str(e), path=DB_PATH)
        finally:
            clear_search_status(qid)


def _sweep_missing():
    """Queue genuinely-missing released issues — but ONLY for series whose folder we've
    actually inventoried (set + present on disk). That folder gate is the safety rail:
    a series with no/absent folder has unverified ownership (every issue reads not-owned),
    and sweeping it blind is exactly how a fresh instance once queued the entire catalog.
    Runs after sync_one has folder-scanned each series, so `owned` is real before we sweep.
    No folder = no sweep, full stop."""
    # Series we've verified on disk — ownership is trustworthy for these only.
    checked = {s["id"] for s in db.get_all_series(DB_PATH)
               if s.get("folder_path") and os.path.isdir(s["folder_path"])}

    missing_counts = db.get_missing_counts_by_series(DB_PATH)
    pack_submitted: set[int] = set()
    indexers = _usenet_indexers()
    sab = _sabnzbd()

    if indexers and sab:
        for series_id, count in missing_counts.items():
            if series_id not in checked:
                continue
            if count < PACK_THRESHOLD:
                continue
            if db.has_active_pack(series_id, DB_PATH):
                continue
            series = db.get_series_by_id(series_id, DB_PATH)
            if not series:
                continue
            nzb_url = search_usenet_pack(indexers, series["title"])
            if nzb_url:
                nzo_id = sab.add_nzb_url(nzb_url, nzb_name=f"{series['title']} - Pack")
                if nzo_id:
                    db.queue_pack(series_id, nzo_id, nzb_url, DB_PATH)
                    logger.info(f"Pack submitted for {series['title']!r} ({count} missing): {nzo_id}")
                    pack_submitted.add(series_id)

    rows = db.get_missing_for_monitored(DB_PATH)
    for row in rows:
        if row["tracked_series_id"] in checked and row["tracked_series_id"] not in pack_submitted:
            db.queue_issue(row["tracked_series_id"], row["number"], DB_PATH)


def _poll_usenet_jobs():
    """Check SABnzbd for completed pending_usenet queue items and finalize them."""
    from datetime import datetime, timedelta
    sab = _sabnzbd()
    if not sab:
        return
    items = db.get_pending_usenet_items(DB_PATH)
    if not items:
        return

    for item in items:
        qid = item["id"]
        nzo_id = item["sab_nzo_id"]
        result = sab.poll_job(nzo_id)
        status = result["status"]

        if status == "queued":
            # Surface SAB's % through the same progress map the UI polls, so a
            # Kometa-initiated Usenet download is trackable like a GetComics one.
            set_progress(qid, result.get("pct", 0), 100)
            continue

        if status == "completed":
            clear_progress(qid)
            storage = result.get("storage", "")
            logger.info(f"Usenet job {nzo_id} completed — storage: {storage}")
            _finalize_usenet_download(item, qid, storage)

        elif status in ("failed", "unknown"):
            age = datetime.utcnow() - datetime.strptime(item["updated_at"], "%Y-%m-%d %H:%M:%S")
            if status == "unknown" and age < timedelta(hours=4):
                # SABnzbd may have cleaned old queue/history entries — wait a bit
                continue
            clear_progress(qid)
            err = result.get("error") or f"SABnzbd status: {status}"
            logger.warning(f"Usenet job {nzo_id} failed: {err}")
            db.update_queue_state(qid, "failed", error=f"Usenet: {err}", path=DB_PATH)


def _finalize_usenet_download(item: dict, qid: int, storage: str):
    """Move a SABnzbd-completed download into the library and mark it done."""
    import shutil as _shutil
    from kometa.downloader import (
        _issue_num_from_file, _safe, _resolve_dir, _fix_extension,
    )
    issue_number = item["issue_number"]
    title = item["title"]
    publisher = item.get("publisher")
    dest_dir = item.get("folder_path") or _resolve_dir(_comics_root(), publisher or "Unknown", title)

    # Pack sentinel — move every comic in storage to dest_dir, let next sync mark issues
    if issue_number == -1:
        comics = find_comics_in_dir(storage)
        if not comics:
            db.update_queue_state(qid, "failed", error="Usenet pack: no comic files in download", path=DB_PATH)
            return
        os.makedirs(dest_dir, exist_ok=True)
        placed = 0
        for src in comics:
            fname = os.path.basename(src)
            dst = os.path.join(dest_dir, fname)
            if os.path.exists(dst):
                logger.info(f"Pack: skipping {fname} — already in library")
                continue
            _shutil.move(src, dst)
            _fix_extension(dst)
            placed += 1
        logger.info(f"Pack: placed {placed}/{len(comics)} file(s) for {title!r} in {dest_dir}")
        db.update_queue_state(qid, "done", path=DB_PATH)
        if not item.get("folder_path") and placed:
            db.set_folder_path(item["tracked_series_id"], dest_dir, DB_PATH)
        try:
            _komga_scan()
        except Exception as e:
            logger.warning(f"Komga scan after pack placement failed: {e}")
        return

    # SAB reports `storage` as the completed FILE for a single-file download, or the
    # job DIRECTORY for multi-file. find_comics_in_dir walks a directory, so a file
    # path would walk to nothing — scan the parent dir when storage is a file.
    scan_dir = storage if os.path.isdir(storage) else os.path.dirname(storage)
    comics = find_comics_in_dir(scan_dir)
    if not comics:
        db.update_queue_state(qid, "failed", error="Usenet: no comic files found in completed download", path=DB_PATH)
        return

    # If single file, take it. If multiple, find the one matching our issue number.
    target = None
    if len(comics) == 1:
        target = comics[0]
    else:
        for f in comics:
            if _issue_num_from_file(f) == issue_number:
                target = f
                break

    if target is None:
        found = [os.path.basename(f) for f in comics]
        db.update_queue_state(
            qid, "failed",
            error=f"Usenet pack didn't contain #{int(issue_number)} (found: {found})",
            path=DB_PATH,
        )
        return

    # Rename to Kometa format
    ext = os.path.splitext(target)[1].lower()
    num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
    dest_name = f"{_safe(title)} #{int(num_int):03d}{ext}"
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, dest_name)

    if os.path.exists(dest_path):
        db.complete_download(
            qid, item["tracked_series_id"], issue_number, item.get("store_date"),
            filename=dest_path, path=DB_PATH,
        )
        _komga_scan()
        return

    try:
        import shutil
        shutil.move(target, dest_path)
    except Exception as e:
        db.update_queue_state(qid, "failed", error=f"Usenet move failed: {e}", path=DB_PATH)
        return

    logger.info(f"Usenet: placed {dest_path}")
    db.complete_download(
        qid, item["tracked_series_id"], issue_number, item.get("store_date"),
        filename=dest_path,
        set_folder_path=dest_dir if not item.get("folder_path") else None,
        path=DB_PATH,
    )
    try:
        _komga_scan()
    except Exception as e:
        logger.warning(f"Komga scan after usenet placement failed: {e}")


def _release_day_retry():
    """Re-queue this week's release-day issues that weren't found yet — runs Thu AEST (= Wed EDT)."""
    from datetime import timedelta
    # store_dates are US Wednesday; running Thu AEST means today AEST = Wed US + 1 day
    release_dates = {str(date.today()), str(date.today() - timedelta(days=1))}
    rows = db.get_missing_for_monitored(DB_PATH)
    for row in rows:
        if row.get("store_date") in release_dates:
            db.queue_issue(row["tracked_series_id"], row["number"], DB_PATH)
