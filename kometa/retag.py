"""Write a clean ComicInfo.xml into the comic files (docs/library-enrichment-spec.md,
steps 3b–3d). THE job that will eventually rewrite every file in the library.

A ZIP entry can't be edited in place, so every file is rebuilt: every page copied
byte-for-byte (same names, same order, STORED), the uploader's ComicInfo dropped
wherever it hides, the record's ComicInfo added at the root. Correctness and
recoverability beat speed, so each file walks a journal:

    pending → built → verified → swapped          (final)
                 ↘ skipped (nothing to write, or the file already says it)
                 ↘ failed  (a reason; the original untouched)

- build: the source is read once and a NEW .cbz written into staging (local disk,
  never the share). Refused on zero pages, a short RAR extract, or a source that
  changed size/mtime under us.
- verify: testzip, every entry's (name, crc32, size) in order equals the source's,
  exactly one ComicInfo.xml at the root that parses and has the expected digest.
- swap: copied beside the original as '.<name>.kometa-tmp' (same folder, same
  filesystem → the rename is atomic), fsync'd, READ BACK from there and verified
  again, then os.replace'd over the original. A CBR becomes '<stem>.cbz' and the
  .cbr is MOVED into _trash/<publisher>/retag-cbr-<date>/…, never deleted.
- after: the books row follows (same id — reading progress hangs off it), and the
  reader's caches (rendered pages, the cover, the ownership verdict) are carried
  over to the new size/mtime, because the pages are byte-identical.

The original keeps its mtime (On Deck and the shelf sort on it); only the size
changes, and when even that doesn't, the mtime moves one second so the backup
mirror still sees a new file.

No scheduler job and no writing endpoint here yet: run_batch is called by hand
(3e) until the full run (3f) is wired. The only route is a read-only preview.
"""
import hashlib
import logging
import os
import shutil
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Response

import kometa.db as db
from kometa import comicinfo, downloader, reader
from kometa.naming import _IMAGE_EXTS

logger = logging.getLogger(__name__)
router = APIRouter()

DB_PATH = db.DB_PATH
TMP_SUFFIX = ".kometa-tmp"
TRASH_DIR = "_trash"            # kometa/trash.py's layout: purge sees _trash/<publisher>/<folder>/
COMICINFO = "ComicInfo.xml"
LOCK_TIMEOUT_S = 900            # how long a swap waits for a running shelf scan
MAX_CONSECUTIVE_FAILURES = 3    # three refusals in a row is a pattern, not three bad files
FINAL = ("swapped", "skipped")
STATES = ("pending", "built", "verified", "swapped", "skipped", "failed")


class RetagError(RuntimeError):
    """A refusal about ONE file: journal it as failed and carry on."""


class SourceChanged(RetagError):
    pass


class VerifyFailed(RetagError):
    pass


class StagedMissing(RetagError):
    """The staged build is gone (staging wiped): build again, not a failure."""


class LibraryBusy(Exception):
    """A shelf scan held the library past the timeout. Not the file's fault: the
    row stays 'verified' and the batch stops."""


class PostSwapError(RuntimeError):
    """The new file is in place but the bookkeeping isn't. NOT a per-file refusal:
    the batch stops, the row stays 'verified', and the next run finishes it."""


def default_staging_dir() -> str:
    # underscore: the page-cache evictor never touches it
    return os.path.join(reader.PAGE_CACHE_DIR, "_retag")


# --- the journal -------------------------------------------------------------------
def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS retag_journal (
            book_id INTEGER PRIMARY KEY, path TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            old_size INTEGER, old_mtime REAL, old_pages_digest TEXT, old_entries_digest TEXT,
            xml_digest TEXT, page_count INTEGER, new_path TEXT, new_size INTEGER, new_mtime REAL,
            trashed_to TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now')))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_retag_journal_state ON retag_journal(state)")


def journal(book_id: int, path=None) -> dict | None:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT * FROM retag_journal WHERE book_id = ?", (book_id,)).fetchone()
        return dict(r) if r else None


def _set(book_id: int, path, **cols) -> None:
    cols["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k} = ?" for k in cols)
    with db._connect(path) as conn:
        conn.execute(f"UPDATE retag_journal SET {sets} WHERE book_id = ?", (*cols.values(), book_id))


def _start(book: dict, path) -> dict:
    """First sight of a book (or a retry): pending, with the file's size and mtime
    as the journal saw them. Everything later is checked against these."""
    ensure_tables(path)
    st = os.stat(book["path"])
    with db._connect(path) as conn:
        conn.execute("""INSERT INTO retag_journal (book_id, path, state, old_size, old_mtime)
            VALUES (?, ?, 'pending', ?, ?)
            ON CONFLICT(book_id) DO UPDATE SET path = excluded.path, state = 'pending',
                old_size = excluded.old_size, old_mtime = excluded.old_mtime,
                old_pages_digest = NULL, old_entries_digest = NULL, xml_digest = NULL, page_count = NULL,
                new_path = NULL, new_size = NULL, new_mtime = NULL, trashed_to = NULL, error = NULL,
                updated_at = datetime('now')""", (book["id"], book["path"], st.st_size, st.st_mtime))
    return journal(book["id"], path)


def summary(path=None) -> dict:
    """Counts per state — what the progress view (3f) will read."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        rows = conn.execute("SELECT state, COUNT(*) AS n FROM retag_journal GROUP BY state").fetchall()
    return {r["state"]: r["n"] for r in rows}


# --- archives ----------------------------------------------------------------------
def _is_image(name: str) -> bool:
    return name.lower().endswith(_IMAGE_EXTS)


def _is_comicinfo(name: str) -> bool:
    return name.replace("\\", "/").rsplit("/", 1)[-1].lower() == "comicinfo.xml"


def _digest(entries) -> str:
    """sha256 over the ordered (name, crc32, size) list."""
    h = hashlib.sha256()
    for name, crc, size in entries:
        h.update(f"{name}\0{crc:08x}\0{size}\n".encode("utf-8", "surrogatepass"))
    return h.hexdigest()


def pages_digest(entries) -> str:
    """The spec's page fingerprint: every IMAGE entry, in order."""
    return _digest([e for e in entries if _is_image(e[0])])


def _is_rar(p: str) -> bool:
    with open(p, "rb") as fh:
        return fh.read(4) == b"Rar!"


def _source_entries(src: str, is_rar: bool):
    """Yield (ZipInfo-to-write, bytes) for every entry of the source, in order.
    CBZ: the archive's own order, its timestamps, read through zipfile (which
    checks each entry's CRC as it reads — a corrupt source fails here). CBR:
    downloader's RAR machinery (one full extract, the short-extract guard)."""
    if is_rar:
        for name, data in downloader._read_archive_pages(src):
            zi = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
            zi.compress_type = zipfile.ZIP_STORED
            yield zi, data
        return
    with zipfile.ZipFile(src) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        if len(set(names)) != len(names):
            raise RetagError("the archive holds two entries with the same name")
        for info in infos:
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = zipfile.ZIP_STORED
            zi.external_attr = info.external_attr
            yield zi, (b"" if info.is_dir() else zf.read(info))


def _stat_pair(p: str) -> tuple[int, float]:
    st = os.stat(p)
    return st.st_size, st.st_mtime


def _check_unchanged(p: str, row: dict) -> None:
    try:
        now = _stat_pair(p)
    except FileNotFoundError:
        raise SourceChanged("the file is gone")
    if now != (row["old_size"], row["old_mtime"]):
        raise SourceChanged(f"the file changed since the journal saw it ({row['old_size']} → {now[0]} bytes)")


def _new_path_for(p: str) -> str:
    stem, ext = os.path.splitext(p)
    return stem + ".cbz" if ext.lower() == ".cbr" else p


class Built(dict):
    """What a build produced: staged, pages_digest, entries_digest, xml_digest,
    page_count, is_rar — or skipped (with a reason) and no staged file."""


def build_one(book: dict, staging_dir: str, row: dict | None = None, path=None, out_name: str | None = None) -> Built:
    """Read the source, write the new .cbz into staging_dir. Returns a Built; a
    Built with 'skipped' set means there's nothing to write (no staged file)."""
    path = path or DB_PATH
    src = book["path"]
    row = row or {"old_size": None, "old_mtime": None}
    if row.get("old_size") is not None:
        _check_unchanged(src, row)
    sid = book.get("tracked_series_id")
    if not sid:
        return Built(skipped="no record: the book isn't in a tracked series")
    is_rar = _is_rar(src)
    os.makedirs(staging_dir, exist_ok=True)
    staged = os.path.join(staging_dir, out_name or f"{book['id']}.cbz")
    part = staged + ".part"
    entries, old_ci, n_ci = [], None, 0
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED, allowZip64=True) as out:
            for zi, data in _source_entries(src, is_rar):
                if _is_comicinfo(zi.filename):
                    n_ci += 1
                    if zi.filename == COMICINFO:
                        old_ci = data
                    continue                                # the uploader's: dropped, wherever it was
                out.writestr(zi, data)
                entries.append((zi.filename, zlib.crc32(data) & 0xFFFFFFFF, len(data)))
            images = [e for e in entries if _is_image(e[0])]
            # bsdtar exits 0 with nothing on an archive it couldn't parse: no pages
            # is never a successful read (downloader._rebuild_as_cbz's guard)
            if not images:
                raise RetagError("the source yielded no image entries — refusing")
            if is_rar:
                claimed = downloader._rar_entry_count(src)
                got = sum(1 for n, _c, _s in entries if n.lower().endswith(downloader._IMG_EXTS))
                if claimed and got < claimed:
                    raise RetagError(f"the RAR read gave {got} of {claimed} pages — refusing")
            page_count = sum(1 for n, _c, _s in entries if reader._is_page(n))
            xml = comicinfo.for_book(book, path, page_count)        # an issue, or the trade it is
            if xml is None:
                skip = "no record: nothing to write"
            elif not is_rar and _new_path_for(src) == src and n_ci == 1 and old_ci is not None and comicinfo.digest(old_ci) == comicinfo.digest(xml):
                skip = "the file already carries this ComicInfo"
            else:
                skip = None
                out.writestr(zipfile.ZipInfo(COMICINFO, date_time=time.localtime()[:6]), xml)
        if skip:
            os.remove(part)
            return Built(skipped=skip, xml_digest=comicinfo.digest(xml) if xml else None)
        # the source must be the file the journal saw, start to finish
        if row.get("old_size") is not None:
            _check_unchanged(src, row)
        os.replace(part, staged)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    return Built(staged=staged, pages_digest=pages_digest(entries), entries_digest=_digest(entries),
                 xml_digest=comicinfo.digest(xml), page_count=page_count, is_rar=is_rar)


def verify(p: str, want: dict) -> None:
    """Raise VerifyFailed unless the zip at p is exactly what the build promised:
    every entry's data passes its CRC (testzip), the non-ComicInfo entries' ordered
    (name, crc32, size) list matches, and one ComicInfo.xml at the root parses and
    has the expected digest. want: pages_digest, entries_digest, xml_digest."""
    try:
        with open(p, "rb") as fh, zipfile.ZipFile(fh) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise VerifyFailed(f"entry {bad!r} fails its CRC")
            infos = zf.infolist()
            cis = [i for i in infos if _is_comicinfo(i.filename)]
            if len(cis) != 1 or cis[0].filename != COMICINFO:
                raise VerifyFailed(f"expected one {COMICINFO} at the root, found {[i.filename for i in cis]}")
            xml = zf.read(cis[0])
            rest = [i for i in infos if i is not cis[0]]
            if any(i.compress_type != zipfile.ZIP_STORED for i in rest):
                raise VerifyFailed("an entry isn't STORED")
            entries = [(i.filename, i.CRC, i.file_size) for i in rest]
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, ValueError) as e:
        raise VerifyFailed(f"not a readable zip: {e}")
    try:
        ET.fromstring(xml)
    except ET.ParseError as e:
        raise VerifyFailed(f"{COMICINFO} doesn't parse: {e}")
    if comicinfo.digest(xml) != want["xml_digest"]:
        raise VerifyFailed(f"{COMICINFO} isn't the one generated")
    if pages_digest(entries) != want["pages_digest"]:
        raise VerifyFailed("the pages differ from the original's (name, crc, size, order)")
    if want.get("entries_digest") and _digest(entries) != want["entries_digest"]:
        raise VerifyFailed("the non-page entries differ from the original's")


def _want(row: dict) -> dict:
    return {"pages_digest": row["old_pages_digest"], "entries_digest": row["old_entries_digest"],
            "xml_digest": row["xml_digest"]}


def _is_ours(p: str, row: dict) -> bool:
    try:
        verify(p, _want(row))
        return True
    except (VerifyFailed, OSError):
        return False


# --- the swap ----------------------------------------------------------------------
def _fsync_dir(d: str) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass                    # not every filesystem syncs a directory; the rename is still atomic
    finally:
        os.close(fd)


def _drop_cached(p: str) -> None:
    """Ask the kernel to forget p's pages so the read-back comes off the server,
    not out of our own write cache (Linux; a no-op where fadvise doesn't exist)."""
    if not hasattr(os, "posix_fadvise"):
        return
    try:
        fd = os.open(p, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    except OSError:
        pass


def _copy_synced(src: str, dst: str) -> None:
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        shutil.copyfileobj(fi, fo, 4 << 20)
        fo.flush()
        os.fsync(fo.fileno())


def _trash_dest(old_path: str, root: str) -> str:
    """_trash/<publisher>/retag-cbr-<date>/<series folder(s)>/<file>: purge sees
    the dated folder as one trashed folder and lets it go after TRASH_DAYS."""
    rel = os.path.relpath(os.path.realpath(old_path), os.path.realpath(root))
    parts = rel.split(os.sep)
    if rel.startswith("..") or len(parts) < 2 or parts[0] == TRASH_DIR:
        raise RetagError("the file isn't under the comics root — nowhere to bin the .cbr")
    dest = os.path.join(root, TRASH_DIR, parts[0], f"retag-cbr-{datetime.now():%Y-%m-%d}", *parts[1:])
    if os.path.exists(dest):
        stem, ext = os.path.splitext(dest)
        dest = f"{stem} ({int(time.time())}){ext}"
    return dest


@contextmanager
def _holding_scan_lock(timeout: float):
    """The shelf scan prunes book rows whose file it didn't list. Between the
    rename and the books row following it, the scan must not look — or a CBR's
    row (and its reading progress) goes with the old name."""
    from kometa import shelf
    if not shelf._scan_lock.acquire(timeout=timeout):
        raise LibraryBusy("a shelf scan held the library too long — try again later")
    try:
        yield
    finally:
        shelf._scan_lock.release()


def _carry_caches(book_id: int, old: tuple, new: tuple, old_path: str, new_path: str) -> None:
    """Pages are byte-identical, so what was rendered from the old file is still
    right: move it to the new version's name instead of re-rendering."""
    old_ver, new_ver = reader._version(*old), reader._version(*new)
    if old_ver == new_ver and old_path == new_path:
        return
    pages_old = os.path.join(reader.PAGE_CACHE_DIR, str(book_id), old_ver)
    pages_new = os.path.join(reader.PAGE_CACHE_DIR, str(book_id), new_ver)
    if os.path.isdir(pages_old) and not os.path.exists(pages_new):
        os.rename(pages_old, pages_new)
    cover_old, cover_new = reader._cover_cache_path(old_path, old_ver), reader._cover_cache_path(new_path, new_ver)
    if os.path.isfile(cover_old) and not os.path.exists(cover_new):
        os.rename(cover_old, cover_new)
    if new_path != old_path:
        # the old CBR's unpacked pages: nothing will read them again
        shutil.rmtree(reader._rar_dir(old_path, old_ver), ignore_errors=True)


def _finish(book: dict, row: dict, new_path: str, path, trashed_to: str | None = None) -> None:
    """The file is in place: the books row follows it in the same transaction that
    marks the journal 'swapped'. Same id, so read progress, marks and the image
    store's book/<id> keys all stay attached."""
    try:
        new_size, new_mtime = _stat_pair(new_path)
        old = (row["old_size"], row["old_mtime"])
        cached_ok = (book.get("size"), book.get("mtime")) == old
        with db._connect(path) as conn:
            other = conn.execute("SELECT id FROM books WHERE path = ? AND id != ?", (new_path, book["id"])).fetchone()
            if other:
                # a shelf index slipped in a row for the new .cbz: drop it if nobody read it
                if conn.execute("SELECT 1 FROM read_progress WHERE book_id = ?", (other["id"],)).fetchone():
                    raise PostSwapError(f"book {other['id']} already holds {new_path} and has reading progress")
                db._forget_book_ids(conn, [other["id"]])
            cur = conn.execute("SELECT pages_json, page_count FROM books WHERE id = ?", (book["id"],)).fetchone()
            pages_json = page_count = None
            if cached_ok:
                pages_json = cur["pages_json"] if cur and cur["pages_json"] else _pages_json(book)
                page_count = cur["page_count"] if cur and cur["page_count"] else book.get("page_count")
            conn.execute("UPDATE books SET path = ?, size = ?, mtime = ?, pages_json = ?, page_count = ? WHERE id = ?",
                         (new_path, new_size, new_mtime, pages_json, page_count, book["id"]))
            if cached_ok and conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'archive_verdict'").fetchone():
                conn.execute("UPDATE OR REPLACE archive_verdict SET path = ?, size = ?, mtime = ? WHERE path = ? AND size = ? AND mtime = ?",
                             (new_path, new_size, new_mtime, book["path"], *old))
            _set(book["id"], path, state="swapped", new_path=new_path, new_size=new_size, new_mtime=new_mtime,
                 trashed_to=trashed_to or row.get("trashed_to"), error=None)
    except PostSwapError:
        raise
    except Exception as e:
        raise PostSwapError(f"swapped, but the books row didn't follow: {e}") from e
    if cached_ok:
        try:
            _carry_caches(book["id"], old, (new_size, new_mtime), book["path"], new_path)
        except OSError as e:
            logger.warning(f"Retag: page cache for book {book['id']} not carried over (it'll re-render): {e}")
    try:
        from kometa.naming import _archive_verdict
        _archive_verdict.cache_clear()
    except Exception:
        pass


def _pages_json(book: dict):
    import json
    return json.dumps(book["pages"]) if book.get("pages") else None


def _bin_cbr(old_path: str, root: str) -> str:
    dest = _trash_dest(old_path, root)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(old_path, dest)
    return dest


def swap(book: dict, row: dict, staged: str, root: str, path=None, lock_timeout: float = LOCK_TIMEOUT_S) -> str:
    """Put the verified build in place of the original. Returns the final path.

    Resumable from any crash: a file already at the destination that verifies as
    THIS build is finished, not rebuilt; anything else there is refused."""
    path = path or DB_PATH
    old_path, new_path = row["path"], row["new_path"]
    converting = new_path != old_path

    # --- a crash after the rename: finish the bookkeeping, nothing else ---
    if not converting:
        try:
            now = _stat_pair(old_path)
        except FileNotFoundError:
            raise SourceChanged("the file is gone")
        if now != (row["old_size"], row["old_mtime"]):
            if _is_ours(old_path, row):
                _finish(book, row, new_path, path)
                return new_path
            raise SourceChanged("the file changed since the journal saw it")
    elif os.path.exists(new_path):
        if not _is_ours(new_path, row):
            raise RetagError(f"{os.path.basename(new_path)!r} already exists beside the .cbr")
        trashed = row.get("trashed_to")
        with _holding_scan_lock(lock_timeout):
            if os.path.exists(old_path):
                _check_unchanged(old_path, row)
                trashed = _bin_cbr(old_path, root)
                _set(book["id"], path, trashed_to=trashed)
            _finish(book, row, new_path, path, trashed)
        return new_path
    else:
        _check_unchanged(old_path, row)

    if not os.path.isfile(staged):
        raise StagedMissing("the staged build is gone")
    folder = os.path.dirname(new_path)
    tmp = os.path.join(folder, "." + os.path.basename(new_path) + TMP_SUFFIX)
    replaced = False
    try:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)              # a previous attempt's leftover — ours by name
            _copy_synced(staged, tmp)
            # keep the original's mtime (On Deck sorts on it); a same-size result moves
            # it one second so a size+mtime mirror still sees the change
            st_old = os.stat(old_path)
            mtime_ns = st_old.st_mtime_ns + (1_000_000_000 if os.path.getsize(tmp) == st_old.st_size else 0)
            os.utime(tmp, ns=(st_old.st_atime_ns, mtime_ns))
        except OSError as e:
            raise RetagError(f"couldn't write the temp file beside the original: {e}") from e
        _drop_cached(tmp)
        verify(tmp, _want(row))             # the read-back: off the share, not out of staging
        with _holding_scan_lock(lock_timeout):
            _check_unchanged(old_path, row)
            if converting and os.path.exists(new_path):
                raise RetagError(f"{os.path.basename(new_path)!r} appeared beside the .cbr")
            try:
                os.replace(tmp, new_path)
            except OSError as e:
                raise RetagError(f"the swap failed, original untouched: {e}") from e
            replaced = True
            _fsync_dir(folder)
            trashed = None
            if converting:
                try:
                    trashed = _bin_cbr(old_path, root)
                except Exception as e:
                    # the .cbr is where it was: take the new file back out, as if never written
                    os.remove(new_path)
                    replaced = False
                    raise RetagError(f"the .cbr couldn't be moved to the trash: {e}") from e
            _finish(book, row, new_path, path, trashed)
    finally:
        if not replaced:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return new_path


# --- one book, journal-driven --------------------------------------------------------
def process_one(book_id: int, staging_dir: str, root: str, path=None, retry_failed: bool = False,
                lock_timeout: float = LOCK_TIMEOUT_S) -> str:
    """Advance one book as far as it will go. Returns its journal state.
    RetagError (a refusal about this file) is journaled as 'failed' and returned;
    anything else propagates — the batch decides whether to stop."""
    path = path or DB_PATH
    ensure_tables(path)
    book = db.get_book(book_id, path)
    row = journal(book_id, path)
    if row and row["state"] in FINAL:
        return row["state"]
    if row and row["state"] == "failed" and not retry_failed:
        return "failed"
    if not book:
        if row:
            _set(book_id, path, state="failed", error="no such book")
        return "failed"
    staged = os.path.join(staging_dir, f"{book_id}.cbz")
    try:
        if not os.path.exists(book["path"]) and not (row and row["state"] == "verified"):
            raise RetagError("the file is gone")
        # a fresh start: first sight, a retry, or the book moved/changed before
        # anything was written (verified rows keep their path: a crash mid-swap
        # is finished by swap(), never restarted)
        if not row or row["state"] == "failed" or (row["state"] in ("pending", "built") and row["path"] != book["path"]):
            row = _start(book, path)
        for _ in range(4):
            state = row["state"]
            if state == "pending":
                built = build_one(book, staging_dir, row, path)
                if built.get("skipped"):
                    _set(book_id, path, state="skipped", error=built["skipped"], xml_digest=built.get("xml_digest"))
                    return "skipped"
                _set(book_id, path, state="built", old_pages_digest=built["pages_digest"],
                     old_entries_digest=built["entries_digest"], xml_digest=built["xml_digest"],
                     page_count=built["page_count"], new_path=_new_path_for(book["path"]),
                     attempts=(row.get("attempts") or 0) + 1, error=None)
            elif state == "built":
                if not os.path.isfile(staged):
                    _set(book_id, path, state="pending")
                else:
                    _check_unchanged(book["path"], row)
                    verify(staged, _want(row))
                    _set(book_id, path, state="verified")
            elif state == "verified":
                try:
                    swap(book, row, staged, root, path, lock_timeout)
                except StagedMissing:
                    _set(book_id, path, state="pending")
                    row = journal(book_id, path)
                    continue
                try:
                    os.remove(staged)
                except OSError:
                    pass
                return "swapped"
            else:
                return state
            row = journal(book_id, path)
        raise RetagError(f"no progress past {row['state']!r}")
    except RetagError as e:
        _fail(book_id, book, path, str(e))
        try:
            os.remove(staged)
        except OSError:
            pass
        return "failed"


def _fail(book_id, book, path, reason: str) -> None:
    if not journal(book_id, path):
        with db._connect(path) as conn:
            conn.execute("INSERT INTO retag_journal (book_id, path, state) VALUES (?, ?, 'failed')",
                         (book_id, (book or {}).get("path") or ""))
    _set(book_id, path, state="failed", error=reason[:500])
    logger.warning(f"Retag: book {book_id} failed: {reason}")


def dry_run_one(book_id: int, staging_dir: str, path=None, keep: bool = False) -> dict:
    """Build and verify in staging only — nothing on the share is written and the
    journal isn't touched. Reports what a real run WOULD do."""
    path = path or DB_PATH
    book = db.get_book(book_id, path)
    if not book:
        return {"book_id": book_id, "would": "fail", "reason": "no such book"}
    row = journal(book_id, path)
    if row and row["state"] in FINAL:
        return {"book_id": book_id, "path": book["path"], "would": "nothing", "reason": f"already {row['state']}"}
    out = {"book_id": book_id, "path": book["path"]}
    d = os.path.join(staging_dir, "dry-run")
    try:
        st = _stat_pair(book["path"])
        b = build_one(book, d, {"old_size": st[0], "old_mtime": st[1]}, path)
        if b.get("skipped"):
            return {**out, "would": "skip", "reason": b["skipped"]}
        try:
            verify(b["staged"], b)
            new_path = _new_path_for(book["path"])
            return {**out, "would": "convert" if new_path != book["path"] else "rewrite", "new_path": new_path,
                    "old_size": st[0], "new_size": os.path.getsize(b["staged"]), "pages": b["page_count"],
                    "xml_digest": b["xml_digest"], **({"staged": b["staged"]} if keep else {})}
        finally:
            if not keep:
                try:
                    os.remove(b["staged"])
                except OSError:
                    pass
    except (RetagError, zipfile.BadZipFile, RuntimeError, OSError) as e:
        return {**out, "would": "fail", "reason": str(e)}


# exceptions that are about ONE file — anything else is systemic and stops the run
_PER_FILE = (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, zlib.error, EOFError)


def run_batch(book_ids, staging_dir: str | None = None, dry_run: bool = False, sleep=time.sleep,
              yield_to_reader=None, root: str | None = None, path=None, retry_failed: bool = False,
              lock_timeout: float = LOCK_TIMEOUT_S, max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES) -> dict:
    """One file at a time, the journal deciding what's left to do; the reader goes
    first between files. Stops at the first exception it doesn't understand (and
    after max_consecutive_failures refusals in a row) rather than ploughing on
    through a systemic failure. Returns {counts, items, stopped}."""
    from kometa import activity, sources
    path = path or DB_PATH
    staging_dir = staging_dir or default_staging_dir()
    yield_to_reader = yield_to_reader or activity.yield_to_reader
    root = root or sources.comics_root()
    ensure_tables(path)
    counts: dict[str, int] = {}
    items, stopped, streak = [], None, 0
    for i, bid in enumerate(book_ids):
        if activity.STOPPING.is_set():
            stopped = "app shutting down"
            break
        if i:
            yield_to_reader(sleep=sleep)
        if dry_run:
            item = dry_run_one(bid, staging_dir, path)
            counts[item["would"]] = counts.get(item["would"], 0) + 1
            items.append(item)
            continue
        before = (journal(bid, path) or {}).get("state")
        try:
            state = process_one(bid, staging_dir, root, path, retry_failed, lock_timeout)
        except _PER_FILE as e:
            _fail(bid, db.get_book(bid, path), path, f"{e.__class__.__name__}: {e}")
            state = "failed"
        except RuntimeError as e:
            if isinstance(e, PostSwapError) or e.__class__ is not RuntimeError:
                stopped = f"book {bid}: {e.__class__.__name__}: {e}"
                _note_stop(bid, path, stopped)
                break
            # downloader's RAR machinery says 'no extractor could open' with a bare RuntimeError
            _fail(bid, db.get_book(bid, path), path, str(e))
            state = "failed"
        except Exception as e:
            stopped = f"book {bid}: {e.__class__.__name__}: {e}"
            _note_stop(bid, path, stopped)
            break
        row = journal(bid, path) or {}
        counts[state] = counts.get(state, 0) + 1
        items.append({"book_id": bid, "state": state, "path": row.get("path"), "new_path": row.get("new_path"),
                      "error": row.get("error"), "was": before})
        if state == "failed" and before != "failed":
            streak += 1
            if streak >= max_consecutive_failures:
                stopped = f"{streak} failures in a row — stopping to look"
                break
        else:
            streak = 0
    if stopped:
        logger.warning(f"Retag batch stopped: {stopped}")
    return {"counts": counts, "items": items, "stopped": stopped, "dry_run": dry_run}


def _note_stop(book_id, path, reason: str) -> None:
    """An unexpected error: note it on the row WITHOUT changing the state, so the
    next run picks up exactly where this one was (a 'verified' row resumes the swap)."""
    try:
        if journal(book_id, path):
            _set(book_id, path, error=reason[:500])
    except Exception:
        pass


# --- read-only preview (3e review) ----------------------------------------------------
@router.get("/api/books/{book_id}/comicinfo-preview")
def comicinfo_preview(book_id: int):
    """The ComicInfo.xml the retag job would write into this book. Reads the
    record only — the file isn't opened, nothing is written."""
    book = db.get_book(book_id, DB_PATH)
    if not book:
        raise HTTPException(404, "No such book")
    if not book.get("tracked_series_id"):
        raise HTTPException(404, "This book isn't in a tracked series")
    xml = comicinfo.for_book(book, DB_PATH, book.get("page_count"))
    if xml is None:
        raise HTTPException(404, "The record has nothing to write for this issue")
    return Response(xml, media_type="application/xml", headers={"X-Comicinfo-Digest": comicinfo.digest(xml)})


