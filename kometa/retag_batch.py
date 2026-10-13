"""The full-library ComicInfo rewrite, as a batch (docs/library-enrichment-spec.md,
step 3f, "Rewrite mechanics"). kometa/retag.py is the engine — build, verify, swap,
the journal — and NOTHING about its safety changes here. This module only decides
where the bytes are read from, where the builds wait, and when each phase runs.

    /retag/source/<rel>   a snapshot of the library (Voot II's mirror → Catbus)
    /retag/build/<id>.cbz the staged builds, verified and waiting for the user's OK

Three phases, each started by hand, each refusing unless config retag_batch_armed = '1':

- queue: every book in a tracked series (retag.enqueue). A row that's already
  swapped/skipped goes back on only when the XML it would get today differs.
- build: N files at once, read from the snapshot when its copy is the NAS file
  (same size, same mtime, and for a CBZ the same page list in the central
  directory), else off the NAS — counted. Stops at 'verified'. Nothing is written
  to /comics; the NAS is only stat'ed (and a CBZ's central directory read).
- swap: one file at a time through retag.swap, untouched — the stale check, the
  temp copy beside the original, fsync, read-back verify, os.replace, the books
  row and page-cache carry-over, the .cbr to _trash. Yields to the reader.

One phase at a time (retag.JOB_LOCK, which the scheduler tick takes too), in a
background thread; GET /api/retag/batch-report says what it's doing.
"""
import logging
import os
import random
import shutil
import threading
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Response

import kometa.db as db
from kometa import comicinfo, retag

logger = logging.getLogger(__name__)
router = APIRouter()

DB_PATH = db.DB_PATH
BATCH_ROOT = "/retag"               # bind of /Volumes/Catbus/kometa/retag; may not exist at all
BUILD_WORKERS = 4                   # CPU + a local SSD; the NAS isn't in this loop
MIN_FREE_BYTES = 20 << 30           # stop building before Catbus is full, not at it
MTIME_SLACK_S = 2.0                 # the snapshot came through SMB → Voot II → Catbus;
                                    # sub-second mtime precision doesn't survive every hop
FALLBACK_SAMPLE = 25                # after this many builds in a run...
                                    # ...more NAS reads than snapshot reads = the snapshot is
                                    # wrong (stale, wrong folder): stop, don't read the library
                                    # over the wire file by file
REPORT_LIST_MAX = 200


class BatchRefused(RuntimeError):
    """The phase won't start (or can't go on): a message for the user, not a file's fault."""


def source_dir() -> str:
    return os.path.join(BATCH_ROOT, "source")


def build_dir() -> str:
    return os.path.join(BATCH_ROOT, "build")


def armed(path=None) -> bool:
    return db.get_config(path or DB_PATH).get("retag_batch_armed", "0") == "1"


def ensure_tables(path=None):
    path = path or DB_PATH
    retag.ensure_tables(path)
    with db._connect(path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(retag_journal)")]
        if "build_source" not in cols:
            # where the build read its bytes: 'snapshot' (Catbus) or 'nas' (the fallback)
            conn.execute("ALTER TABLE retag_journal ADD COLUMN build_source TEXT")


# --- progress (one phase at a time) ----------------------------------------------------
_STOP = threading.Event()
_status_lock = threading.Lock()
_status: dict = {"phase": None}


def _status_set(**kw) -> None:
    with _status_lock:
        _status.update(kw)


def _bump(key: str, n: int = 1) -> None:
    with _status_lock:
        counts = _status.setdefault("counts", {})
        counts[key] = counts.get(key, 0) + n


def status() -> dict:
    with _status_lock:
        out = dict(_status)
        out["counts"] = dict(out.get("counts") or {})
    out["running"] = out.get("phase") is not None and out.get("finished_at") is None
    out["stop_requested"] = _STOP.is_set()
    return out


def running() -> bool:
    return status()["running"]


def stop() -> dict:
    """Finish the file(s) in hand, then stop. Safe any time; a no-op when idle."""
    _STOP.set()
    return status()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def start(phase: str, fn, *args, **kw) -> dict:
    """Run fn in a background thread holding retag.JOB_LOCK. Refuses if any retag
    job (a phase, or the scheduler tick) already holds it."""
    if not retag.JOB_LOCK.acquire(blocking=False):
        raise BatchRefused("a retag job is already running — stop it or wait")
    _STOP.clear()
    with _status_lock:
        _status.clear()
        _status.update(phase=phase, started_at=_now(), finished_at=None, counts={}, done=0, total=None,
                       stopped=None, error=None)

    def run():
        try:
            out = fn(*args, **kw)
            _status_set(result=out, stopped=(out or {}).get("stopped") if isinstance(out, dict) else None)
        except Exception as e:                                  # the thread is the last line: say it, don't vanish
            logger.exception(f"Retag batch {phase} died")
            _status_set(error=f"{e.__class__.__name__}: {e}")
        finally:
            _status_set(finished_at=_now())
            retag.JOB_LOCK.release()

    threading.Thread(target=run, name=f"retag-batch-{phase}", daemon=True).start()
    return status()


# --- scope: the user's go ---------------------------------------------------------------
_STOPGAP = ("comicinfo", "komga")


def _plan(path) -> list[dict]:
    """Every book in a tracked series, what the queue would do with it, and what
    its record is made of (harvest._owned_record's catalogue/stopgap split)."""
    ensure_tables(path)
    from kometa import record
    record.ensure_tables(path)
    with db._connect(path) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT b.id AS book_id, b.number, r.source, r.fill_state, j.state, j.xml_digest, j.page_count
            FROM books b
            LEFT JOIN issue_record r ON r.tracked_series_id = b.tracked_series_id AND r.number = b.number
            LEFT JOIN retag_journal j ON j.book_id = b.id
            WHERE b.tracked_series_id IS NOT NULL ORDER BY b.path""")]
    for r in rows:
        if r["number"] is None:
            r["data"] = "trade"                     # tagged from the trade list, no issue record
        elif r["source"] in retag._CATALOGUE and r["fill_state"] != "miss":
            r["data"] = "catalogue"
        elif r["source"] in _STOPGAP:
            r["data"] = "stopgap"
        else:
            r["data"] = "no_record"
        st = r["state"]
        if st in ("pending", "built", "verified"):
            r["action"] = "in_flight"               # already on the list: left alone
        elif st in retag.FINAL:
            # the test batch's 17: back on only if today's XML differs (requeue_changed's
            # comparison, without its one-week wait — this is the user's go)
            book = db.get_book(r["book_id"], path)
            xml = comicinfo.for_book(book, path, r["page_count"]) if book else None
            r["action"] = "changed" if xml is not None and comicinfo.digest(xml) != r["xml_digest"] else "same"
        else:
            r["action"] = "queue"                   # never seen, failed or stale
    return rows


def dry_count(path=None) -> dict:
    """What batch_queue WOULD do. Reads the DB only — not a file is stat'ed, so a
    book whose file has gone is counted here and dropped by the real queue."""
    path = path or DB_PATH
    rows = _plan(path)
    out = {"tracked_books": len(rows), "would_queue": 0, "would_requeue_changed": 0, "in_flight": 0,
           "unchanged_done": 0, "by_data": {}, "queued_by_data": {}}
    key = {"queue": "would_queue", "changed": "would_requeue_changed", "in_flight": "in_flight", "same": "unchanged_done"}
    for r in rows:
        out[key[r["action"]]] += 1
        out["by_data"][r["data"]] = out["by_data"].get(r["data"], 0) + 1
        if r["action"] in ("queue", "changed"):
            out["queued_by_data"][r["data"]] = out["queued_by_data"].get(r["data"], 0) + 1
    return out


def batch_queue(path=None) -> dict:
    """Step 3f's go: every tracked book on the list (retag.enqueue — a row in
    flight is left alone, a fresh size/mtime stamped on the rest)."""
    path = path or DB_PATH
    rows = [r for r in _plan(path) if r["action"] in ("queue", "changed")]
    _status_set(total=len(rows))
    n = 0
    for i, r in enumerate(rows):
        if _STOP.is_set():
            return {"queued": n, "stopped": "stop requested"}
        n += retag.enqueue([r["book_id"]], "run" if r["action"] == "queue" else "changed", path)
        _status_set(done=i + 1)
        _bump("queued" if r["action"] == "queue" else "requeued_changed")
    return {"queued": n, "stopped": None}


# --- build ---------------------------------------------------------------------------
def check_source() -> None:
    src = source_dir()
    if not os.path.isdir(src):
        raise BatchRefused(f"{src} doesn't exist — is the Catbus snapshot bind-mounted?")
    with os.scandir(src) as it:
        if next(it, None) is None:
            raise BatchRefused(f"{src} is empty — refusing to read the whole library over the network")


def _central_pages(p: str) -> str:
    """The page list as the zip's central directory states it — the tail of the
    file, not the pages. Over NFS that's a few KB, not the book."""
    with zipfile.ZipFile(p) as zf:
        return retag.pages_digest([(i.filename, i.CRC, i.file_size) for i in zf.infolist() if not i.is_dir()])


def _snapshot_for(nas_path: str, root: str, row: dict) -> str | None:
    """The snapshot's copy of the file, or None (read the NAS). A copy counts when
    its size is the journal's, its mtime within MTIME_SLACK_S of the journal's, and —
    for a zip — its central directory lists the same pages (name, crc, size, order)
    as the NAS file's: the size+mtime coincidence a stale copy can't fake."""
    rel = os.path.relpath(nas_path, root)
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    snap = os.path.join(source_dir(), rel)
    try:
        st = os.stat(snap)
    except OSError:
        return None
    if st.st_size != row["old_size"] or abs(st.st_mtime - row["old_mtime"]) > MTIME_SLACK_S:
        return None
    try:
        if retag._is_rar(snap):
            return snap                 # no cheap index to compare; size+mtime it is
        return snap if _central_pages(snap) == _central_pages(nas_path) else None
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError):
        return None                     # let the NAS build say what's wrong with it


def _build_task(book: dict, row: dict, root: str, path, bdir: str):
    """One file, in a worker thread: build from the snapshot (or the NAS), check
    the NAS original is still the journal's, verify the staged build. Touches the
    DB only to read the record."""
    nas = book["path"]
    snap = _snapshot_for(nas, root, row)
    if snap:
        st = os.stat(snap)
        # build_one's own check, pointed at the snapshot: it mustn't change mid-read
        built = retag.build_one({**book, "path": snap}, bdir, {"old_size": st.st_size, "old_mtime": st.st_mtime}, path)
    else:
        built = retag.build_one(book, bdir, row, path)
    source = "snapshot" if snap else "nas"
    if built.get("skipped"):
        return "skipped", built, source
    try:
        retag._check_unchanged(nas, row)          # the NAS file is still the one the journal stamped
        retag.verify(built["staged"], built)
    except BaseException:
        _rm(built["staged"])
        raise
    return "verified", built, source


def _rm(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass


def _rows(path, states) -> list[dict]:
    ensure_tables(path)
    with db._connect(path) as conn:
        q = ",".join("?" * len(states))
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM retag_journal WHERE state IN ({q}) ORDER BY path", states)]


def batch_build(path=None, root: str | None = None, workers: int = BUILD_WORKERS) -> dict:
    """Build + verify every queued file into /retag/build, BUILD_WORKERS at a time.
    Stops at 'verified'. Resumable: verified rows with their build on disk are
    skipped, one whose build is gone is rebuilt, failed/stale rows wait for a
    re-queue. Writes nothing under /comics."""
    from kometa import sources
    path = path or DB_PATH
    root = root or sources.comics_root()
    check_source()
    bdir = build_dir()
    os.makedirs(bdir, exist_ok=True)
    ensure_tables(path)
    todo = _rows(path, ("pending", "built", "verified"))
    _status_set(total=len(todo))
    seen = {"snapshot": 0, "nas": 0}
    stopped, streak, done = None, 0, 0
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="retag-build")
    inflight: dict = {}

    def settle(fut) -> None:
        nonlocal streak, stopped, done
        bid, attempts, nas_path = inflight.pop(fut)
        staged = os.path.join(bdir, f"{bid}.cbz")
        state, err = None, None
        try:
            state, built, source = fut.result()
            seen[source] += 1
            if state == "skipped":
                retag._set(bid, path, state="skipped", error=built["skipped"], xml_digest=built.get("xml_digest"),
                           build_source=source)
            else:
                retag._set(bid, path, state="verified", old_pages_digest=built["pages_digest"],
                           old_entries_digest=built["entries_digest"], xml_digest=built["xml_digest"],
                           page_count=built["page_count"], new_path=retag._new_path_for(nas_path),
                           attempts=attempts + 1, error=None, build_source=source)
        except retag.SourceChanged as e:
            state, err = "stale", str(e)
        except retag.RetagError as e:
            state, err = "failed", str(e)
        except retag._PER_FILE as e:
            state, err = "failed", f"{e.__class__.__name__}: {e}"
        except RuntimeError as e:
            if e.__class__ is not RuntimeError:
                stopped = stopped or f"book {bid}: {e.__class__.__name__}: {e}"
                retag._note_stop(bid, path, stopped)
            else:                                   # downloader's 'no extractor could open'
                state, err = "failed", str(e)
        except Exception as e:
            stopped = stopped or f"book {bid}: {e.__class__.__name__}: {e}"
            retag._note_stop(bid, path, stopped)
        if err is not None:
            _rm(staged)
            if state == "stale":
                retag._set(bid, path, state="stale", error=err[:500])
            else:
                retag._fail(bid, db.get_book(bid, path), path, err)
        done += 1
        _status_set(done=done, from_snapshot=seen["snapshot"], from_nas=seen["nas"])
        if state:
            _bump(state)
        if state == "failed":
            streak += 1
            if streak >= retag.MAX_CONSECUTIVE_FAILURES:
                stopped = stopped or f"{streak} failures in a row — stopping to look"
        elif state:
            streak = 0
        if not stopped and seen["snapshot"] + seen["nas"] >= FALLBACK_SAMPLE and seen["nas"] > seen["snapshot"]:
            stopped = (f"{seen['nas']} of {seen['snapshot'] + seen['nas']} builds fell back to the NAS — "
                       f"the snapshot doesn't match the library; check {source_dir()}")

    try:
        for row in todo:
            if stopped or _STOP.is_set() or _activity_stopping():
                break
            while len(inflight) >= workers:
                for fut in wait(list(inflight), return_when=FIRST_COMPLETED).done:
                    settle(fut)
            if stopped or _STOP.is_set():
                break
            bid = row["book_id"]
            staged = os.path.join(bdir, f"{bid}.cbz")
            book = db.get_book(bid, path)
            if not book:
                retag._set(bid, path, state="failed", error="no such book")
                _bump("failed")
                continue
            if row["state"] == "verified":
                if os.path.isfile(staged):
                    done += 1
                    _bump("already_verified")
                    _status_set(done=done)
                    continue
                try:
                    unchanged = retag._stat_pair(row["path"]) == (row["old_size"], row["old_mtime"])
                except OSError:
                    unchanged = False
                if not unchanged:
                    # build gone AND the original moved on: maybe a swap that crashed after
                    # its rename — swap() knows how to finish or refuse that; leave it to it
                    done += 1
                    _bump("left_for_swap")
                    _status_set(done=done)
                    continue
            if not os.path.exists(book["path"]):
                retag._fail(bid, book, path, "the file is gone")
                _bump("failed")
                continue
            if row.get("queued_for") == "arrival" and not retag._due(row, book, path):
                continue                            # waits for its catalogue data, as the tick does
            # nothing's built yet (or the build is gone): stamp the NAS file as it is NOW —
            # the swap's safety check compares against this
            row = retag._start(book, path)
            if (free := _free_bytes()) is not None and free < MIN_FREE_BYTES + 2 * (row["old_size"] or 0):
                stopped = f"{BATCH_ROOT} is nearly full ({free >> 30} GB free) — stopping"
                break
            fut = pool.submit(_build_task, book, row, root, path, bdir)
            inflight[fut] = (bid, row.get("attempts") or 0, book["path"])
        while inflight:
            for fut in wait(list(inflight), return_when=FIRST_COMPLETED).done:
                settle(fut)
    finally:
        pool.shutdown(wait=True)
    if not stopped and _STOP.is_set():
        stopped = "stop requested"
    if stopped:
        logger.warning(f"Retag batch build stopped: {stopped}")
    return {"from_snapshot": seen["snapshot"], "from_nas": seen["nas"], "stopped": stopped}


def _activity_stopping() -> bool:
    from kometa import activity
    return activity.STOPPING.is_set()


def _free_bytes() -> int | None:
    try:
        return shutil.disk_usage(BATCH_ROOT).free
    except OSError:
        return None


# --- swap ------------------------------------------------------------------------------
def batch_swap(path=None, root: str | None = None, sleep=time.sleep, yield_to_reader=None,
               lock_timeout: float = retag.LOCK_TIMEOUT_S) -> dict:
    """Every verified row onto the NAS, one at a time, through retag.swap exactly as
    the standing job does it. An original that changed since the journal stamped it
    is marked 'stale' and left alone. The staged build goes once its swap is done."""
    from kometa import activity, sources
    path = path or DB_PATH
    root = root or sources.comics_root()
    yield_to_reader = yield_to_reader or activity.yield_to_reader
    bdir = build_dir()
    if not os.path.isdir(bdir):
        # no builds dir = every row would read as 'build gone' and drop back to pending
        raise BatchRefused(f"{bdir} doesn't exist — is the Catbus snapshot bind-mounted?")
    ensure_tables(path)
    todo = _rows(path, ("verified",))
    _status_set(total=len(todo))
    stopped, streak = None, 0
    for i, row in enumerate(todo):
        if _STOP.is_set():
            stopped = "stop requested"
            break
        if activity.STOPPING.is_set():
            stopped = "app shutting down"
            break
        if i:
            yield_to_reader(sleep=sleep)
        bid = row["book_id"]
        staged = os.path.join(bdir, f"{bid}.cbz")
        book = db.get_book(bid, path)
        state = None
        try:
            if not book:
                raise retag.RetagError("no such book")
            retag.swap(book, row, staged, root, path, lock_timeout)
            state = "swapped"
            _rm(staged)
        except retag.StagedMissing:
            retag._set(bid, path, state="pending")          # the build phase makes it again
            state = "unbuilt"
        except retag.SourceChanged as e:
            retag._set(bid, path, state="stale", error=str(e)[:500])
            _rm(staged)
            state = "stale"
        except retag.RetagError as e:
            retag._fail(bid, book, path, str(e))
            _rm(staged)
            state = "failed"
        except retag._PER_FILE as e:
            retag._fail(bid, book, path, f"{e.__class__.__name__}: {e}")
            state = "failed"
        except RuntimeError as e:
            if isinstance(e, retag.PostSwapError) or e.__class__ is not RuntimeError:
                stopped = f"book {bid}: {e.__class__.__name__}: {e}"
                retag._note_stop(bid, path, stopped)
                break
            retag._fail(bid, book, path, str(e))
            state = "failed"
        except Exception as e:                              # LibraryBusy and the unknown: the row stays verified
            stopped = f"book {bid}: {e.__class__.__name__}: {e}"
            retag._note_stop(bid, path, stopped)
            break
        _bump(state)
        _status_set(done=i + 1)
        if state == "failed":
            streak += 1
            if streak >= retag.MAX_CONSECUTIVE_FAILURES:
                stopped = f"{streak} failures in a row — stopping to look"
                break
        else:
            streak = 0
    _sweep_swapped(bdir, path)
    if stopped:
        logger.warning(f"Retag batch swap stopped: {stopped}")
    return {"stopped": stopped}


def _sweep_swapped(bdir: str, path) -> None:
    """A crash between a swap's bookkeeping and the staged file's removal leaves
    the build behind: it's done with, take it."""
    try:
        names = [n for n in os.listdir(bdir) if n.endswith(".cbz") and n[:-4].isdigit()]
    except OSError:
        return
    for n in names:
        row = retag.journal(int(n[:-4]), path)
        if row and row["state"] == "swapped":
            _rm(os.path.join(bdir, n))


# --- the check (read-only) -------------------------------------------------------------
def _staged_bytes(bdir: str) -> tuple[int, int]:
    total = n = 0
    try:
        with os.scandir(bdir) as it:
            for e in it:
                if e.is_file():
                    total += e.stat().st_size
                    n += 1
    except OSError:
        pass
    return total, n


def batch_report(sample: int = 10, path=None) -> dict:
    """Everything the user needs to say yes (or no) to the swap. Reads the DB and
    lists /retag/build; writes nothing."""
    path = path or DB_PATH
    ensure_tables(path)
    bdir = build_dir()
    with db._connect(path) as conn:
        by_state = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM retag_journal GROUP BY state")}
        by_source = {r["build_source"]: r["n"] for r in conn.execute(
            "SELECT build_source, COUNT(*) AS n FROM retag_journal WHERE build_source IS NOT NULL GROUP BY build_source")}
        problems = [dict(r) for r in conn.execute(
            "SELECT book_id, path, state, error, updated_at FROM retag_journal WHERE state IN ('failed', 'stale') "
            "ORDER BY updated_at DESC LIMIT ?", (REPORT_LIST_MAX,))]
        reasons = {}
        for r in conn.execute("SELECT error FROM retag_journal WHERE state IN ('failed', 'stale')"):
            k = (r["error"] or "?")[:80]
            reasons[k] = reasons.get(k, 0) + 1
        cbr = [dict(r) for r in conn.execute(
            "SELECT book_id, path, new_path FROM retag_journal WHERE state = 'verified' AND new_path IS NOT NULL "
            "AND new_path != path ORDER BY path")]
        verified = [dict(r) for r in conn.execute(
            "SELECT book_id, path, new_path, page_count, build_source FROM retag_journal WHERE state = 'verified'")]
        skipped = {r["error"]: r["n"] for r in conn.execute(
            "SELECT error, COUNT(*) AS n FROM retag_journal WHERE state = 'skipped' GROUP BY error")}
    staged_bytes, staged_files = _staged_bytes(bdir)
    on_disk = [v for v in verified if os.path.isfile(os.path.join(bdir, f"{v['book_id']}.cbz"))]
    picks = random.sample(on_disk, min(sample, len(on_disk)))
    for v in picks:
        v["staged"] = os.path.join(bdir, f"{v['book_id']}.cbz")
        v["staged_comicinfo"] = f"/api/retag/batch/staged/{v['book_id']}/comicinfo"
        v["record_preview"] = f"/api/books/{v['book_id']}/comicinfo-preview"
    try:
        du = shutil.disk_usage(BATCH_ROOT)
        disk = {"free": du.free, "total": du.total}
    except OSError:
        disk = None
    try:
        check_source()
        source_ok, source_note = True, None
    except BatchRefused as e:
        source_ok, source_note = False, str(e)
    return {
        "armed": armed(path), "progress": status(),
        "states": by_state, "build_source": by_source,
        "problems": {"total": sum(reasons.values()), "reasons": reasons, "rows": problems},
        "skipped": skipped,
        "cbr_to_cbz": {"total": len(cbr), "rows": cbr[:REPORT_LIST_MAX]},
        "verified_without_build": len(verified) - len(on_disk),
        "staged": {"bytes": staged_bytes, "files": staged_files, "dir": bdir},
        "disk": disk, "source": {"dir": source_dir(), "ok": source_ok, "note": source_note},
        "sample": picks,
    }


# --- routes ----------------------------------------------------------------------------
def _armed_or_403() -> None:
    if not armed(DB_PATH):
        raise HTTPException(403, "The retag batch isn't armed (config retag_batch_armed = '1')")


def _start_or_409(phase: str, fn, *args, **kw) -> dict:
    try:
        return start(phase, fn, *args, **kw)
    except BatchRefused as e:
        raise HTTPException(409, str(e))


@router.post("/api/retag/batch/queue")
def route_queue():
    _armed_or_403()
    return _start_or_409("queue", batch_queue, DB_PATH)


@router.post("/api/retag/batch/build")
def route_build():
    _armed_or_403()
    try:
        check_source()                 # refuse HERE, with the reason, not in a thread's log
    except BatchRefused as e:
        raise HTTPException(409, str(e))
    return _start_or_409("build", batch_build, DB_PATH)


@router.post("/api/retag/batch/swap")
def route_swap():
    _armed_or_403()
    if not os.path.isdir(build_dir()):
        raise HTTPException(409, f"{build_dir()} doesn't exist — nothing to swap")
    return _start_or_409("swap", batch_swap, DB_PATH)


@router.post("/api/retag/batch/stop")
def route_stop():
    # never gated: stopping must always work, armed or not
    return stop()


@router.get("/api/retag/batch-report")
def route_report(sample: int = 10):
    return batch_report(max(0, min(sample, 100)), DB_PATH)


@router.get("/api/retag/batch/dry-count")
def route_dry_count():
    return dry_count(DB_PATH)


@router.get("/api/retag/batch/staged/{book_id}/comicinfo")
def route_staged_comicinfo(book_id: int):
    """The ComicInfo.xml INSIDE the staged build — what the swap will put on the NAS.
    Compare with /api/books/{id}/comicinfo-preview (what the record says today)."""
    p = os.path.join(build_dir(), f"{book_id}.cbz")
    if not os.path.isfile(p):
        raise HTTPException(404, "No staged build for this book")
    try:
        with zipfile.ZipFile(p) as zf:
            xml = zf.read(retag.COMICINFO)
    except (KeyError, zipfile.BadZipFile) as e:
        raise HTTPException(500, f"The staged build has no readable {retag.COMICINFO}: {e}")
    row = retag.journal(book_id, DB_PATH) or {}
    return Response(xml, media_type="application/xml",
                    headers={"X-Comicinfo-Digest": comicinfo.digest(xml),
                             "X-Journal-Digest": row.get("xml_digest") or "",
                             "X-Journal-State": row.get("state") or ""})
