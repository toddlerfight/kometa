import os
import re
import time
import shutil
import logging
import zipfile
import tempfile
import subprocess
import requests
from concurrent.futures import ThreadPoolExecutor

# Path/name helpers live in naming now (next to scan_folder_numbers); imported
# back here so existing call sites — and acquisition's imports — stay unchanged.
from kometa.naming import _safe, _resolve_dir
import kometa.sources as sources

logger = logging.getLogger(__name__)

S3_LARGE = "https://s3.amazonaws.com/comicgeeks/comics/covers/large-{}.jpg"
_LOCG_BASE = "https://leagueofcomicgeeks.com"

_PAGES_BLOCK_RE = re.compile(r'\s*<Pages>.*?</Pages>', re.DOTALL)
_PAGE_COUNT_RE  = re.compile(r'(<PageCount>)\d+(</PageCount>)')


def _patch_comic_info(xml_bytes: bytes, added_covers: int) -> bytes:
    """Update ComicInfo.xml: strip <Pages> block and bump PageCount."""
    try:
        xml = xml_bytes.decode('utf-8', errors='replace')
        xml = _PAGES_BLOCK_RE.sub('', xml)
        def _bump(m):
            old = int(m.group(0).split('>')[1].split('<')[0])
            return f"{m.group(1)}{old + added_covers}{m.group(2)}"
        xml = _PAGE_COUNT_RE.sub(_bump, xml)
        return xml.encode('utf-8')
    except Exception:
        return xml_bytes


def _download_cover(cover_id: str) -> bytes | None:
    """Plain requests, no scraper — the covers sit on public S3, and there is no
    Cloudflare bouncer guarding a bucket that never asked for one."""
    try:
        r = requests.get(S3_LARGE.format(cover_id), timeout=30)
    except requests.RequestException:
        return None
    return r.content if r.status_code == 200 and r.content else None


def _read_archive_pages(path: str):
    """Yield (name, bytes) for every entry, sorted by name.

    CBZ reads member-by-member via zipfile. RAR (detected by magic bytes, not
    extension — mislabeled files walk among us) gets ONE sequential bsdtar
    extract to a temp dir first: solid RAR archives are compressed as a single
    stream, so member-at-a-time access (what rarfile does with any backend that
    can't seek) dies with 'Failed the read enough data', while a front-to-back
    full extract sails through. Proven against the exact file that failed."""
    with open(path, 'rb') as fh:
        is_rar = fh.read(4) == b'Rar!'
    if is_rar:
        tmpdir = tempfile.mkdtemp(prefix='kometa-cbr-')
        try:
            subprocess.run(['bsdtar', '-xf', path, '-C', tmpdir],
                           check=True, capture_output=True)
            entries = []
            for root, _dirs, files in os.walk(tmpdir):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, tmpdir).replace(os.sep, '/')
                    entries.append((rel, full))
            for rel, full in sorted(entries):
                with open(full, 'rb') as fh:
                    yield rel, fh.read()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        with zipfile.ZipFile(path, 'r') as src:
            for name in sorted(src.namelist()):
                yield name, src.read(name)


def inject_covers(cbz_path: str, selected: list, primary_id: str) -> int:
    """
    Prepend selected variant cover images to cbz_path. Writes back in place
    (atomically — a temp file next to the target, then os.replace).
    CBR input is converted to CBZ and the source .cbr is removed once the
    rebuilt archive verifies. Returns count of images injected.
    selected: [{"id": str, "name": str}, ...]
    primary_id: id of the cover that should sort first
    """
    # Bare requests.get per thread — no shared session state to trip over
    ids = [c['id'] for c in selected]
    with ThreadPoolExecutor(max_workers=min(8, len(ids))) as ex:
        datas = list(ex.map(_download_cover, ids))
    variant_pages = [(c['id'], c.get('name', c['id']), d)
                     for c, d in zip(selected, datas) if d]

    variant_pages.sort(key=lambda x: (0 if x[0] == primary_id else 1, x[1]))

    # Build the rebuilt archive in a temp file beside the target, then os.replace
    # it into place. The original is bit-for-bit untouched until the swap —
    # a crash mid-write leaves a stray .tmp, never a truncated comic.
    out_path = re.sub(r'\.cbr$', '.cbz', cbz_path, flags=re.IGNORECASE)
    tmp_path = out_path + '.kometa-tmp'
    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_STORED) as zf:
            cover_names = []
            for i, (_vid, name, data) in enumerate(variant_pages):
                safe = re.sub(r'[^\w\s\-]', '', name).strip().replace(' ', '_')
                fname = f"{str(i).zfill(3)}_cover_{safe}.jpg"
                zf.writestr(fname, data)
                cover_names.append(fname)

            comic_info_data = None
            for name, data in _read_archive_pages(cbz_path):
                if name.lower() == 'comicinfo.xml':
                    comic_info_data = data
                elif re.match(r'\d{3}_cover_', name):
                    # Drop covers WE injected on a previous apply — replace, don't stack.
                    # Never matches the comic's own pages (they're not named NNN_cover_…),
                    # so the original cover + interior pages are always preserved.
                    continue
                else:
                    zf.writestr(name, data)

            if comic_info_data is not None:
                comic_info_data = _patch_comic_info(comic_info_data, len(cover_names))
                zf.writestr('ComicInfo.xml', comic_info_data)
            entry_count = len(zf.namelist())

        # Verify the rebuild BEFORE it replaces anything (and long before the
        # source .cbr is allowed to die). Paranoia is free; re-downloading a
        # comic that got eaten is not.
        with zipfile.ZipFile(tmp_path, 'r') as chk:
            if chk.testzip() is not None or len(chk.namelist()) != entry_count:
                raise RuntimeError("rebuilt archive failed verification")

        os.replace(tmp_path, out_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    # CBR→CBZ conversion: the verified replacement is on disk, so the source
    # .cbr is now a 180MB duplicate that makes Komga see two books for one
    # issue. Remove it — failure here is logged, never fatal.
    if out_path != cbz_path:
        try:
            os.remove(cbz_path)
            logger.info(f"inject_covers: converted CBR→CBZ, removed source {cbz_path}")
        except OSError as e:
            logger.warning(f"inject_covers: could not remove source CBR {cbz_path}: {e}")

    return len(variant_pages)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def _fix_extension(path: str) -> str:
    """Rename file if its extension doesn't match its magic bytes. Returns final path."""
    try:
        with open(path, 'rb') as f:
            magic = f.read(4)
    except OSError:
        return path
    ext = os.path.splitext(path)[1].lower()
    if magic[:4] == b'Rar!' and ext != '.cbr':
        correct = path[:-len(ext)] + '.cbr'
        os.rename(path, correct)
        logger.info(f"Extension fix: {os.path.basename(path)} → .cbr")
        return correct
    if magic[:2] == b'PK' and ext == '.cbr':
        correct = path[:-4] + '.cbz'
        os.rename(path, correct)
        logger.info(f"Extension fix: {os.path.basename(path)} → .cbz")
        return correct
    return path


def _remote_content_length(url: str) -> int | None:
    """HEAD the URL and return Content-Length, or None if unavailable."""
    try:
        r = requests.head(url, timeout=15, headers=HEADERS, allow_redirects=True)
        cl = r.headers.get("content-length")
        return int(cl) if cl else None
    except Exception:
        return None


def _prev_issue_size(directory: str, issue_number: float) -> int | None:
    """Return the file size of the previous issue in directory, or None if not found."""
    prev = int(issue_number) - 1 if issue_number == int(issue_number) else int(issue_number)
    patterns = list(dict.fromkeys([f"#{prev:03d}", f"#{prev}", f"Issue {prev:03d}", f"Issue {prev}"]))
    try:
        for entry in os.scandir(directory):
            if not entry.is_file():
                continue
            name = entry.name
            if any(p in name for p in patterns):
                return entry.stat().st_size
    except OSError:
        pass
    return None


class DuplicateIssueError(ValueError):
    pass


class WrongIssueError(DuplicateIssueError):
    pass


# Matches "#135", "#135.1" — strips leading zeros
_NUM_FROM_FNAME_RE = re.compile(r'#\s*0*(\d+(?:\.\d+)?)')
# Matches bare "001 (2016)" style — used as fallback for pack filenames without #
_NUM_FROM_FNAME_BARE_RE = re.compile(r'(?<!\d)0*(\d{1,3})(?!\d)')


def _num_from_filename(name: str) -> float | None:
    m = _NUM_FROM_FNAME_RE.search(os.path.basename(name))
    return float(m.group(1)) if m else None


def _num_from_filename_broad(name: str) -> float | None:
    """Fallback for pack files like 'Batman 001 (2016)...' — finds first non-year 1-3 digit number."""
    base = re.sub(r'\.\w+$', '', os.path.basename(name))
    if re.search(r'\bannual\b', base, re.IGNORECASE):
        return None
    base = re.sub(r'\b\d{4}\b', '', base)  # remove 4-digit years first
    m = _NUM_FROM_FNAME_BARE_RE.search(base)
    if m:
        return float(m.group(1))
    return None


def _issue_num_from_file(path: str) -> float | None:
    """Filename (#N), ComicInfo.xml, then broad bare-number fallback — for pack content identification."""
    n = _num_from_filename(path)
    if n is not None:
        return n
    n = _read_cbz_number(path)
    if n is not None:
        return n
    return _num_from_filename_broad(path)


def _read_archive_comicinfo(archive) -> str | None:
    names = [n for n in archive.namelist() if n.lower() == 'comicinfo.xml']
    return archive.read(names[0]).decode('utf-8', errors='replace') if names else None


def _read_cbz_number(path: str) -> float | None:
    """Return the issue number from ComicInfo.xml. Detects format by magic bytes, not extension."""
    try:
        with open(path, 'rb') as fh:
            magic = fh.read(4)
        xml = None
        if magic[:2] == b'PK':  # ZIP — real CBZ or mislabeled
            with zipfile.ZipFile(path, 'r') as zf:
                xml = _read_archive_comicinfo(zf)
        elif magic[:4] == b'Rar!':  # RAR — real CBR or mislabeled .cbz
            try:
                import rarfile
                rarfile.UNRAR_TOOL = 'bsdtar'
                with rarfile.RarFile(path, 'r') as rf:
                    xml = _read_archive_comicinfo(rf)
            except Exception:
                return None
        if xml:
            m = re.search(r'<Number>\s*(\d+(?:\.\d+)?)\s*</Number>', xml, re.IGNORECASE)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return None


# A single issue rarely exceeds this many image pages. A trade, an omnibus, or a
# vertical/webtoon edition (each panel is its own "page") blows well past it — which
# is exactly how an 80-panel webtoon release slipped in mislabeled as a print issue.
_SINGLE_ISSUE_PAGE_MAX = 70


_IMG_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp')


def _count_archive_images(path: str) -> int | None:
    """Image-entry count of a CBZ/CBR, or None if unreadable. ZIP lists cheaply;
    RAR needs a front-to-back extract — solid RARs can't be listed member-by-member
    (`bsdtar -tf` dies where `-xf` works), the same reason _read_archive_pages
    extracts rather than streams. None when we genuinely can't tell, so the page
    guard never false-rejects an archive it couldn't read."""
    try:
        with open(path, 'rb') as fh:
            magic = fh.read(4)
        if magic[:2] == b'PK':
            with zipfile.ZipFile(path, 'r') as zf:
                return sum(1 for n in zf.namelist() if n.lower().endswith(_IMG_EXTS))
        if magic[:4] == b'Rar!':
            tmpdir = tempfile.mkdtemp(prefix='kometa-count-')
            try:
                subprocess.run(['bsdtar', '-xf', path, '-C', tmpdir],
                               check=True, capture_output=True, timeout=180)
                n = 0
                for _root, _dirs, files in os.walk(tmpdir):
                    n += sum(1 for f in files if f.lower().endswith(_IMG_EXTS))
                return n
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        return None
    return None


def _verify_single_issue(path: str, issue_number: float, source_name: str | None = None) -> None:
    """Reject anything that isn't this single issue — raises WrongIssueError. Three
    guards: the source filename's issue number, the ComicInfo number, and a page
    count (a collection or vertical/webtoon edition dwarfs a single issue). Shared
    by both the GetComics and usenet paths so they accept/reject identically."""
    name = source_name or os.path.basename(path)
    fnum = _num_from_filename(name)
    if fnum is not None and fnum != issue_number:
        raise WrongIssueError(f"file is #{int(fnum)}, expected #{int(issue_number)}")
    cnum = _read_cbz_number(path)
    if cnum is not None and cnum != issue_number:
        raise WrongIssueError(f"ComicInfo reports #{int(cnum)}, expected #{int(issue_number)}")
    pages = _count_archive_images(path)
    if pages is not None and pages > _SINGLE_ISSUE_PAGE_MAX:
        raise WrongIssueError(
            f"{pages} pages — looks like a collection or vertical/webtoon edition, "
            f"not single issue #{int(issue_number)}")


def download_issue(
    url: str,
    title: str,
    publisher: str | None,
    issue_number: float,
    store_date: str | None,
    hint_filename: str | None,
    komga_scan_fn,
    progress_fn=None,
    dest_dir: str | None = None,
    tracked_series_id: int | None = None,
    db_path: str | None = None,
) -> str:
    """
    Download from url, place in library, trigger Komga scan.
    Returns the final file path. Raises on failure.
    Pass tracked_series_id + db_path to enable automatic variant injection.
    """
    os.makedirs(sources.staging_dir(), exist_ok=True)

    # Pre-download: if the scraper gave us a direct filename hint (Strategy 3),
    # check its issue number before wasting bandwidth.
    if hint_filename:
        hint_num = _num_from_filename(hint_filename)
        if hint_num is not None and hint_num != issue_number:
            raise WrongIssueError(
                f"Hint filename '{hint_filename}' is issue #{int(hint_num)}, expected #{int(issue_number)}"
            )

    # Compare remote Content-Length against the previous issue's file size.
    # GetComics sometimes posts last week's file under the new issue title —
    # a size match against the previous issue is a reliable signal to skip.
    check_dir = dest_dir or _resolve_dir(sources.comics_root(), publisher or "Unknown", title)
    remote_size = _remote_content_length(url)
    if remote_size and remote_size > 1024:
        prev_size = _prev_issue_size(check_dir, issue_number)
        if prev_size and prev_size == remote_size:
            raise DuplicateIssueError(
                f"Remote size ({remote_size:,} bytes) matches previous issue — "
                f"GetComics likely posted last week's file under issue #{int(issue_number)}"
            )

    last_exc = None
    for attempt in range(3):
        if attempt:
            time.sleep(5 * attempt)
        try:
            r = requests.get(url, stream=True, timeout=120, headers=HEADERS, allow_redirects=True)
            r.raise_for_status()
            break
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            logger.warning(f"Download attempt {attempt + 1} failed ({e}), retrying...")
    else:
        raise last_exc or RuntimeError(f"Download failed after retries: {url[:80]}")

    filename = _server_filename(r, hint_filename, url)
    if not filename:
        logger.warning(f"Could not determine server filename for {url[:80]} — using fallback name")
        ext = _detect_ext(r, hint_filename, url)
        num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
        filename = f"{_safe(title)} #{num_int:03d}{ext}"

    # Strip any path components the server may have embedded — stage flat
    filename = _safe(os.path.basename(filename))
    staging_path = os.path.join(sources.staging_dir(), filename)
    total = int(r.headers.get("content-length", 0))
    done = 0
    with open(staging_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
            done += len(chunk)
            if progress_fn:
                progress_fn(done, total)

    size = os.path.getsize(staging_path)
    if size < 1024:
        os.remove(staging_path)
        raise ValueError(f"Downloaded file too small ({size} bytes) — likely an error page")

    # Content checks: wrong issue (server filename / ComicInfo) or a collection /
    # webtoon edition (page count). Shared with the usenet finalize so both sources
    # reject the same bad content. Clean up the staging file on rejection.
    try:
        _verify_single_issue(staging_path, issue_number, filename)
    except WrongIssueError:
        os.remove(staging_path)
        raise

    if not dest_dir:
        dest_dir = _resolve_dir(sources.comics_root(), publisher or "Unknown", title)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, _safe(filename))
    if os.path.exists(dest_path):
        os.remove(staging_path)
        raise DuplicateIssueError(
            f"{filename} already exists in library — GetComics served an existing issue"
        )
    shutil.move(staging_path, dest_path)
    dest_path = _fix_extension(dest_path)
    logger.info(f"Placed: {dest_path}")

    # If it's a ZIP pack containing multiple comics, extract and discard the wrapper
    extracted = _extract_pack(dest_path, dest_dir)
    if extracted:
        os.remove(dest_path)
        logger.info(f"Pack: {len(extracted)} new file(s) from {os.path.basename(dest_path)}")
        # Find the extracted file that matches the issue we actually requested (Gap J/K)
        target = next(
            (f for f in extracted if _issue_num_from_file(f) == issue_number),
            None,
        )
        if target is None:
            # Pack didn't contain our specific issue — leave extracted files on disk
            # so Komga picks them up on next scan; only fail this queue entry
            logger.warning(
                f"Pack did not contain issue #{int(issue_number)}, "
                f"leaving {len(extracted)} extracted file(s) on disk"
            )
            raise WrongIssueError(
                f"Pack did not contain issue #{int(issue_number)} "
                f"(found: {[os.path.basename(f) for f in extracted]})"
            )
        dest_path = target
        # Other newly-extracted issues stay on disk — next sync picks them up

    if tracked_series_id is not None and db_path is not None:
        try:
            from kometa import db as _db
            prefs = _db.get_variant_prefs(tracked_series_id, issue_number, db_path)
            if prefs:
                added = inject_covers(dest_path, prefs["selected"], prefs["primary_id"])
                _db.clear_variant_prefs(tracked_series_id, issue_number, db_path)
                logger.info(f"Injected {added} variant cover(s) into {dest_path}")
        except Exception as e:
            logger.warning(f"Variant injection failed: {e}")

    try:
        komga_scan_fn()
    except Exception as e:
        logger.warning(f"Komga scan trigger failed: {e}")

    return dest_path


def download_trade(
    url: str,
    dest_dir: str,
    hint_filename: str | None = None,
    fallback_name: str | None = None,
    progress_fn=None,
    komga_scan_fn=None,
) -> list[str]:
    """Download a collected edition (TPB/HC) into dest_dir. The 'dumb' path: NO
    issue-number validation (a trade has no single number; a 'Vol 1-6' bundle
    holds many), NO variant injection, NO reconcile. Just fetch, place, and let
    Komga scan. Returns the placed file path(s) — a pack expands to several."""
    os.makedirs(sources.staging_dir(), exist_ok=True)
    os.makedirs(dest_dir, exist_ok=True)

    last_exc = None
    for attempt in range(3):
        if attempt:
            time.sleep(5 * attempt)
        try:
            r = requests.get(url, stream=True, timeout=120, headers=HEADERS, allow_redirects=True)
            r.raise_for_status()
            break
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            logger.warning(f"Trade download attempt {attempt + 1} failed ({e}), retrying...")
    else:
        raise last_exc or RuntimeError(f"Trade download failed after retries: {url[:80]}")

    filename = _server_filename(r, hint_filename, url)
    if not filename:
        # GetComics gave no Content-Disposition — name it from the trade itself so
        # Komga reads something sane, not "trade.cbz".
        base = fallback_name or "trade"
        filename = f"{_safe(base)}{_detect_ext(r, hint_filename, url)}"
    filename = _safe(os.path.basename(filename))
    staging_path = os.path.join(sources.staging_dir(), filename)
    total = int(r.headers.get("content-length", 0))
    done = 0
    with open(staging_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
            done += len(chunk)
            if progress_fn:
                progress_fn(done, total)

    size = os.path.getsize(staging_path)
    if size < 1024:
        os.remove(staging_path)
        raise ValueError(f"Downloaded file too small ({size} bytes) — likely an error page")

    dest_path = os.path.join(dest_dir, _safe(filename))
    if os.path.exists(dest_path):
        os.remove(staging_path)
        raise DuplicateIssueError(f"{filename} already exists in library")
    shutil.move(staging_path, dest_path)
    dest_path = _fix_extension(dest_path)
    logger.info(f"Placed trade: {dest_path}")

    # A bundled trade ('Vol 1-6') often arrives as a ZIP of CBZs — keep ALL of
    # them (no issue-targeting, unlike download_issue's pack handling).
    placed = [dest_path]
    extracted = _extract_pack(dest_path, dest_dir)
    if extracted:
        os.remove(dest_path)
        placed = extracted
        logger.info(f"Trade pack: {len(extracted)} file(s) from {os.path.basename(dest_path)}")

    if komga_scan_fn:
        try:
            komga_scan_fn()
        except Exception as e:
            logger.warning(f"Komga scan after trade failed: {e}")
    return placed


def _extract_pack(zip_path: str, dest_dir: str) -> list[str]:
    """If zip_path is a ZIP of comic files, extract new ones. Returns paths of extracted files."""
    try:
        if not zipfile.is_zipfile(zip_path):
            return []
        with zipfile.ZipFile(zip_path, 'r') as zf:
            comics = [n for n in zf.namelist()
                      if os.path.splitext(n)[1].lower() in set(_COMIC_EXTS) and not n.startswith('__')]
            if len(comics) <= 1:
                return []
            extracted = []
            for name in comics:
                fname = os.path.basename(name)
                if not fname:
                    continue
                out = os.path.join(dest_dir, fname)
                if os.path.exists(out):
                    logger.info(f"Pack: skipping {fname} — already in library")
                    continue
                with zf.open(name) as src, open(out, 'wb') as dst:
                    dst.write(src.read())
                out = _fix_extension(out)
                extracted.append(out)
                logger.info(f"Pack extracted: {out}")
            return extracted
    except Exception as e:
        logger.warning(f"Pack extraction failed: {e}")
        return []


_COMIC_EXTS = ('.cbz', '.cbr', '.zip', '.rar')


def _server_filename(response, hint_filename: str | None, url: str) -> str | None:
    """Return the real filename from the server, or None if unresolvable."""
    from urllib.parse import unquote
    cd = response.headers.get("content-disposition", "")
    if cd:
        m = re.search(r"filename\*?=(?:UTF-8'')?[\"']?([^\"'\n;]+)", cd, re.IGNORECASE)
        if m:
            name = unquote(m.group(1).strip().strip("\"'"))
            if name and any(name.lower().endswith(e) for e in _COMIC_EXTS):
                return name
    if hint_filename and any(hint_filename.lower().endswith(e) for e in _COMIC_EXTS):
        return hint_filename
    basename = url.rsplit("/", 1)[-1].split("?")[0]
    if basename and any(basename.lower().endswith(e) for e in _COMIC_EXTS):
        return basename
    return None


def _detect_ext(response, hint_filename: str | None, url: str) -> str:
    for source in [
        response.headers.get("content-disposition", ""),
        hint_filename or "",
        url,
    ]:
        for ext in (".cbz", ".cbr", ".zip", ".rar"):
            if ext in source.lower():
                return ".cbz" if ext == ".zip" else ext
    ct = response.headers.get("content-type", "")
    if "zip" in ct:
        return ".cbz"
    return ".cbz"
