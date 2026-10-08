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
TRICKLE_HOURS = range(0, 24)   # around the clock — Metron is an API with a daily budget, not a site we tiptoe past
METRON_PER_TICK = 20

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


def _confident(rows, series: dict) -> int | None:
    row = _confident_row(rows, series)
    return int(row["id"]) if row else None


def _confident_row(rows, series: dict) -> dict | None:
    """Exactly one candidate agreeing on title, publisher and (when there's any
    year evidence) year — that row, else None. Shared by Metron and LOCG matching."""
    title, folder_year = _title_and_year(series["title"])
    from kometa.metron_client import title_variants
    wants = {norm_key(t) for t in title_variants(title)}
    pub = _pub_key(series.get("publisher") or "")
    years = _file_years(series.get("folder_path") or "")
    first_year = folder_year or (years[0] if years else None)
    passing = []
    for r in rows:
        if r.get("comic"):
            continue
        cand_title, cand_year_in_title = _title_and_year(r.get("title") or "")
        if norm_key(cand_title) not in wants:
            continue
        if pub and r.get("publisher") and _pub_key(r["publisher"]) != pub:
            continue
        cand_year = r.get("year") or cand_year_in_title
        if first_year and cand_year and abs(int(cand_year) - int(first_year)) > 1:
            continue
        passing.append(r)
    if len(passing) == 1:
        return passing[0]
    # several runs share the name and nothing on disk says which — or none fit
    return None


def find_confident_match(series: dict, search=search_series_strict) -> int | None:
    """LOCG: the series id, or None. Searches the title as written and with
    folder-style ' - ' as ': '. Raises if LOCG doesn't answer."""
    from kometa.metron_client import title_variants
    title, _ = _title_and_year(series["title"])
    rows, seen = [], set()
    for q in title_variants(title):
        for r in search(q):
            if r["id"] not in seen:
                seen.add(r["id"])
                rows.append(r)
    return _remember(series, "locg", _confident_row(rows, series))


def find_metron_match(series: dict, search=None) -> int | None:
    """Metron: the series id, or None. Raises MetronUnavailable if it doesn't answer."""
    from kometa import metron_client
    return _remember(series, "metron", _confident_row((search or metron_client.search_series)(series["title"]), series))


def _remember(series: dict, source: str, row: dict | None) -> int | None:
    """Keep WHAT matched on the series dict so 'Match now' can say it back to
    you ('Matched to Batman: Damned, DC 2018') instead of a bare status."""
    if not row:
        return None
    series["_matched"] = {"source": source, "id": int(row["id"]), "title": row.get("title"),
                          "publisher": row.get("publisher"), "year": row.get("year"),
                          "issue_count": row.get("issue_count")}
    return int(row["id"])


def _match(series: dict) -> tuple[int | None, int | None]:
    """(metron_id, locg_id). Metron first — it's the API built for this; LOCG
    only when Metron has no confident answer (or isn't configured). Raises when
    a source that's needed didn't answer: that's 'try later', not 'no match'."""
    from kometa import metron_client
    from kometa.locg_client import locg_paused
    title, _ = _title_and_year(series["title"])
    if len(re.sub(r"\W", "", title)) < 3 or not re.search(r"[A-Za-z]", title):
        # '01', '06', '08' (a RoboCop rip filed by volume number): nothing any
        # catalogue can be asked. Straight to Needs matching — you name it.
        return None, None
    if metron_client.configured():
        mid = find_metron_match(series)
        if mid:
            return mid, None
        if locg_paused():
            # Metron answered and wasn't sure; LOCG is shut. Asking LOCG would
            # raise, and three of those in a row used to abort the whole tick on
            # the SAME three series every time — 782 pending, matched 0. Hand it
            # to you instead: Needs matching, with Metron candidates to pick from.
            return None, None
    return None, find_confident_match(series)


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
                metron_id, locg_id = _match(s)
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
            if metron_id or locg_id:
                if metron_id:
                    db.set_metron_series_id(s["id"], metron_id, DB_PATH)
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
    from kometa import metron_client
    metron = metron_client.configured()
    if hour not in TRICKLE_HOURS or (locg_paused() and not metron):
        return
    try:
        # Metron is an API built for this: a handful per tick (~200 a day, well
        # inside its 5,000) clears the backlog in days. LOCG-only: one per tick.
        match_pending(limit=METRON_PER_TICK if metron else 1, sleep=lambda s: None)
        if metron:
            link_metron_existing()
            link_locg_existing(limit=1)        # no-op while LOCG is paused
    except Exception as e:
        logger.warning(f"Import trickle failed: {e}")


def link_metron_existing(limit: int = METRON_PER_TICK) -> int:
    """Series you added the old way (LOCG-matched) gain a Metron link, pull list
    first — that's where nearly all the LOCG traffic was. Confident-only, same
    rule. A series Metron can't place confidently is remembered ('none') so it
    isn't asked again every tick."""
    from kometa import metron_client
    if not metron_client.configured():
        return 0
    todo = [s for s in db.get_all_series(DB_PATH)
            if s.get("kind") != "arc" and not s.get("metron_series_id")
            and s.get("match_status") in (None, AUTO, MANUAL) and s.get("metron_link") is None]
    todo.sort(key=lambda s: (not s.get("on_pull_list"), s["title"].lower()))
    linked = 0
    for s in todo[:limit]:
        try:
            mid = find_metron_match(s)
        except metron_client.MetronUnavailable as e:
            logger.info(f"Metron link paused: {e}")
            break
        if mid:
            db.set_metron_series_id(s["id"], mid, DB_PATH)
            linked += 1
        db.set_metron_link(s["id"], "linked" if mid else "none", DB_PATH)
    return linked


def link_locg_existing(limit: int = 1) -> int:
    """The mirror of link_metron_existing: a Metron-matched series gains its LOCG
    id — confident rule, pull list first, ONE per tick (LOCG manners) and only
    while LOCG is open to us. Why bother: LOCG's variant list (reprints, retailer
    and event exclusives) is what the Variants tab merges in on top of Metron's.
    'none' is remembered so a series isn't asked about every tick."""
    from kometa.locg_client import locg_paused, LocgPaused
    if locg_paused():
        return 0
    todo = [s for s in db.get_all_series(DB_PATH)
            if s.get("kind") != "arc" and s.get("metron_series_id") and not s.get("locg_series_id")
            and s.get("locg_link") is None]
    todo.sort(key=lambda s: (not s.get("on_pull_list"), s["title"].lower()))
    linked = 0
    for s in todo[:limit]:
        try:
            lid = find_confident_match(s)
        except LocgPaused:
            break
        except Exception as e:
            logger.info(f"LOCG link skipped for {s['title']!r}: {e}")
            break
        if lid:
            db.set_locg_series_id(s["id"], lid, DB_PATH)
            linked += 1
        db.set_locg_link(s["id"], "linked" if lid else "none", DB_PATH)
    return linked


def match_one(series_id: int) -> dict:
    """'Match now': one series, right away. Returns {match_status, matched} —
    matched says WHICH run (source, title, publisher, year) so the UI can tell
    you, not just that something happened. Raises when a source that was needed
    didn't answer — that's not 'no match'."""
    from kometa.sync import sync_one, sync_one_guarded
    s = db.get_series_by_id(series_id, DB_PATH)
    metron_id, locg_id = _match(s)
    if metron_id:
        db.set_metron_series_id(series_id, metron_id, DB_PATH)
    if locg_id:
        db.set_locg_series_id(series_id, locg_id, DB_PATH)
    status = AUTO if (metron_id or locg_id) else NEEDS_MATCH
    db.set_match_status(series_id, status, DB_PATH)
    threading.Thread(target=sync_one_guarded, args=(db.get_series_by_id(series_id, DB_PATH), sync_one),
                     daemon=True).start()
    return {"match_status": status, "matched": s.get("_matched")}


def import_in_background():
    threading.Thread(target=import_new_safe, name="shelf-import", daemon=True).start()
