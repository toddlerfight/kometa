"""Every series on the shelf is a Kometa series (docs/reader-spec.md, 2026-10-08).

The shelf index (shelf.py) knows every folder. This turns each folder that has
no series yet into one — pull list OFF, so nothing downloads — and tries to
match it to LOCG. Only CONFIDENT matches are linked automatically: a wrong run
brings the wrong issue list and the wrong covers and is hard to notice later,
while an unmatched series is merely missing metadata. Unsure → 'needs_match',
and you pick the run.

The first run covers ~800 folders, so the LOCG side is throttled and runs in
the background; series appear immediately (owned issues straight from disk)
and fill in as they're matched.
"""
import logging
import os
import re
import threading
import time

import kometa.db as db
from kometa.naming import norm_key, _pub_key
from kometa.locg_client import search_series_strict

logger = logging.getLogger(__name__)

DB_PATH = db.DB_PATH

# Seconds between series when a batch does run (tests / one-offs). The NORMAL
# path is the trickle below — not this.
THROTTLE_S = float(os.environ.get("KOMETA_IMPORT_THROTTLE_S", "10"))

# The background matcher trickles: ONE series per tick, daytime only. A first
# cut ran ~360 LOCG calls an hour; LOCG had already started refusing us and
# that would have sealed it. ~40 a day clears ~800 in three weeks — nothing's
# urgent: every series is readable now, matching only adds metadata. Anything
# you want sooner: "Match now" on its page.
TRICKLE_MINUTES = 20
TRICKLE_HOURS = range(8, 22)   # local time

PENDING, AUTO, NEEDS_MATCH, MANUAL = "pending", "auto", "needs_match", "manual"
MAX_FAILURES_IN_A_ROW = 3

_job_lock = threading.Lock()
_YEAR_RE = re.compile(r"\((\d{4})\)")


def _clean_title(folder_name: str) -> str:
    """Folder names carry scars — '- Batman - Lost', 'Alien vs Predator -'."""
    return folder_name.strip().strip("-").strip() or folder_name


def _title_and_year(title: str) -> tuple[str, int | None]:
    m = _YEAR_RE.search(title)
    year = int(m.group(1)) if m else None
    return _YEAR_RE.sub("", title).strip(), year


def _file_years(folder: str) -> list[int]:
    try:
        return sorted(int(y) for f in os.listdir(folder) for y in _YEAR_RE.findall(f) if 1930 <= int(y) <= 2100)
    except OSError:
        return []


def import_new_folders() -> list[int]:
    """Create a (pull-list-off, pending-match) series for every shelf folder
    without one. Owned issues come straight from disk immediately; LOCG comes
    later from the throttled matcher. Returns the new series ids."""
    from kometa.sync import rescan_owned
    new_ids = []
    # One series per title. The shelf has series split across two folders
    # ('Event Horizon- Dark Descent' + 'Event Horizon - Dark Descent', Last Ronin
    # under both IDW and Mirage) — a second folder must not mint a twin series.
    # It stays on the shelf, readable, and is logged for the file-tidy task.
    taken = {norm_key(s["title"]) for s in db.get_all_series(DB_PATH) if s.get("kind") != "arc"}
    for s in db.list_shelf("me", untracked_only=True, path=DB_PATH):
        title = _clean_title(s["title"])
        if norm_key(title) in taken:
            logger.info(f"Shelf import: duplicate folder {s['folder_path']!r} — a series named "
                        f"{title!r} already exists; left on the shelf for the tidy task")
            continue
        taken.add(norm_key(title))
        _, year = _title_and_year(title)
        sid = db.add_series(title=title, publisher=s.get("publisher"), year_began=year,
                            folder_path=s["folder_path"], on_pull_list=False, path=DB_PATH)
        db.set_match_status(sid, PENDING, DB_PATH)
        db.link_shelf_series(s["id"], sid, DB_PATH)
        try:
            rescan_owned(db.get_series_by_id(sid, DB_PATH))
        except Exception as e:
            logger.warning(f"Import: disk scan failed for {title!r}: {e}")
        new_ids.append(sid)
    if new_ids:
        logger.info(f"Shelf import: {len(new_ids)} folders became series (pull list off)")
    return new_ids


def find_confident_match(series: dict, search=search_series_strict) -> int | None:
    """The LOCG series id, or None unless exactly one candidate agrees on title,
    publisher and (when there's any year evidence) year."""
    title, folder_year = _title_and_year(series["title"])
    want = norm_key(title)
    pub = _pub_key(series.get("publisher") or "")
    years = _file_years(series.get("folder_path") or "")
    first_year = folder_year or (years[0] if years else None)
    passing = []
    for r in search(title):
        if r.get("comic"):
            continue
        cand_title, cand_year_in_title = _title_and_year(r.get("title") or "")
        if norm_key(cand_title) != want:
            continue
        if pub and r.get("publisher") and _pub_key(r["publisher"]) != pub:
            continue
        cand_year = r.get("year") or cand_year_in_title
        if first_year and cand_year and abs(int(cand_year) - int(first_year)) > 1:
            continue
        passing.append(r)
    if len(passing) == 1:
        return int(passing[0]["id"])
    # several runs share the name and nothing on disk says which — or none fit
    return None


def match_pending(limit: int | None = None, sleep=time.sleep) -> dict:
    """Throttled: match each pending series, then give it its first full sync.
    One job at a time; a second caller returns immediately."""
    if not _job_lock.acquire(blocking=False):
        return {"skipped": "import already running"}
    from kometa.sync import sync_one, sync_one_guarded
    done = {AUTO: 0, NEEDS_MATCH: 0, "failed": 0}
    failures_in_a_row = 0
    try:
        pending = [s for s in db.get_all_series(DB_PATH) if s.get("match_status") == PENDING]
        for s in pending[:limit] if limit else pending:
            try:
                locg_id = find_confident_match(s)
                failures_in_a_row = 0
            except Exception as e:
                # LOCG didn't answer (2026-10-08: a Cloudflare challenge). That is
                # NOT "no match" — the series stays pending for the next run. Several
                # in a row means LOCG is shut to us: stop, don't burn the list.
                done["failed"] += 1
                failures_in_a_row += 1
                logger.warning(f"Import: LOCG search failed for {s['title']!r}: {e}")
                if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                    logger.warning(f"Import: LOCG failing {failures_in_a_row}x in a row — "
                                   f"pausing matching until the next scan")
                    break
                sleep(THROTTLE_S)
                continue
            if locg_id:
                db.set_locg_series_id(s["id"], locg_id, DB_PATH)
                db.set_match_status(s["id"], AUTO, DB_PATH)
                done[AUTO] += 1
            else:
                db.set_match_status(s["id"], NEEDS_MATCH, DB_PATH)
                done[NEEDS_MATCH] += 1
            sync_one_guarded(db.get_series_by_id(s["id"], DB_PATH), sync_one)
            sleep(THROTTLE_S)
        if pending:
            logger.info(f"Shelf import: matched {done[AUTO]}, {done[NEEDS_MATCH]} need you to pick")
        return done
    finally:
        _job_lock.release()


def import_new_safe():
    """After a shelf scan: new folders become series. Matching is left to the
    trickle — never a batch."""
    try:
        import_new_folders()
    except Exception as e:
        logger.warning(f"Shelf import failed: {e}")


def trickle_tick(now_hour: int | None = None):
    """Scheduler job: match ONE pending series, in daytime, if LOCG is open to us."""
    from kometa.locg_client import locg_paused
    from datetime import datetime
    from kometa.scheduler import TZ
    hour = now_hour if now_hour is not None else datetime.now(TZ).hour
    if hour not in TRICKLE_HOURS or locg_paused():
        return
    try:
        match_pending(limit=1, sleep=lambda s: None)
    except Exception as e:
        logger.warning(f"Import trickle failed: {e}")


def match_one(series_id: int) -> str:
    """'Match now': one series, right away. Returns its new match_status.
    Raises when LOCG doesn't answer — that's not 'no match'."""
    from kometa.sync import sync_one, sync_one_guarded
    s = db.get_series_by_id(series_id, DB_PATH)
    locg_id = find_confident_match(s)
    if locg_id:
        db.set_locg_series_id(series_id, locg_id, DB_PATH)
        db.set_match_status(series_id, AUTO, DB_PATH)
    else:
        db.set_match_status(series_id, NEEDS_MATCH, DB_PATH)
    threading.Thread(target=sync_one_guarded, args=(db.get_series_by_id(series_id, DB_PATH), sync_one),
                     daemon=True).start()
    return AUTO if locg_id else NEEDS_MATCH


def import_in_background():
    threading.Thread(target=import_new_safe, name="shelf-import", daemon=True).start()
