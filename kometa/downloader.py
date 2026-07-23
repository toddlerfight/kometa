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


def _walk_dir_pages(d: str):
    """Yield (rel_name, bytes) for every file under d, sorted — the same entry
    shape the archive readers hand out, so an extracted dir is a drop-in."""
    entries = []
    for root, _dirs, files in os.walk(d):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, d).replace(os.sep, '/')
            entries.append((rel, full))
    for rel, full in sorted(entries):
        with open(full, 'rb') as fh:
            yield rel, fh.read()


def _read_archive_pages(path: str, extracted_dir: str | None = None):
    """Yield (name, bytes) for every entry, sorted by name.

    CBZ reads member-by-member via zipfile. RAR (detected by magic bytes, not
    extension — mislabeled files walk among us) gets ONE sequential bsdtar
    extract to a temp dir first: solid RAR archives are compressed as a single
    stream, so member-at-a-time access (what rarfile does with any backend that
    can't seek) dies with 'Failed the read enough data', while a front-to-back
    full extract sails through. Proven against the exact file that failed.

    If the caller already paid for that extract (download_issue's one-shot RAR
    extract), pass extracted_dir and we walk it instead of extracting AGAIN."""
    if extracted_dir:
        yield from _walk_dir_pages(extracted_dir)
        return
    with open(path, 'rb') as fh:
        is_rar = fh.read(4) == b'Rar!'
    if is_rar:
        tmpdir = tempfile.mkdtemp(prefix='kometa-cbr-')
        try:
            subprocess.run(['bsdtar', '-xf', path, '-C', tmpdir],
                           check=True, capture_output=True)
            yield from _walk_dir_pages(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        with zipfile.ZipFile(path, 'r') as src:
            for name in sorted(src.namelist()):
                yield name, src.read(name)


def _rebuild_as_cbz(src_path: str, variant_pages: list, extracted_dir: str | None = None) -> str:
    """THE archive-rebuild seam — one verified, atomic CBZ rewrite that both
    variant injection and plain CBR→CBZ conversion go through (variant_pages=[]
    is a straight repack: every entry name preserved, byte-for-byte, ZIP_STORED).

    Build in a temp file beside the target, then os.replace it into place. The
    original is bit-for-bit untouched until the swap — a crash mid-write leaves
    a stray .tmp, never a truncated comic. A .cbr source is removed only AFTER
    the rebuilt .cbz verifies and lands. Returns the final on-disk path.
    variant_pages: [(id, name, jpeg_bytes), ...] already sorted, may be empty.
    extracted_dir: pre-extracted contents of src_path (RAR one-shot extract) —
    read pages from there instead of extracting the archive all over again."""
    out_path = re.sub(r'\.cbr$', '.cbz', src_path, flags=re.IGNORECASE)
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
            source_entries = 0
            for name, data in _read_archive_pages(src_path, extracted_dir):
                source_entries += 1
                if name.lower() == 'comicinfo.xml' and cover_names:
                    # Held back for patching — only when we're actually adding
                    # covers. A plain repack writes it through untouched.
                    comic_info_data = data
                elif re.match(r'\d{3}_cover_', name) and cover_names:
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

        # Zero entries out of the source is NOT a successful read — it's bsdtar
        # (libarchive) exiting 0 on an archive it couldn't actually parse
        # (observed live with a truncated RAR5 header). Swap that in and we'd
        # replace a real comic with an empty shell, then delete the original.
        if source_entries == 0:
            raise RuntimeError("source archive yielded no entries — refusing the swap")

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
    if out_path != src_path:
        try:
            os.remove(src_path)
            logger.info(f"_rebuild_as_cbz: converted CBR→CBZ, removed source {src_path}")
        except OSError as e:
            logger.warning(f"_rebuild_as_cbz: could not remove source CBR {src_path}: {e}")

    return out_path


def inject_covers(cbz_path: str, selected: list, primary_id: str,
                  extracted_dir: str | None = None) -> tuple[int, str]:
    """
    Prepend selected variant cover images to cbz_path. Writes back in place
    (atomically, via _rebuild_as_cbz). CBR input is converted to CBZ and the
    source .cbr is removed once the rebuilt archive verifies.
    Returns (count of images injected, final on-disk path) — the path MATTERS:
    a CBR input comes back as a different filename, and recording the dead .cbr
    is how a queue row ends up pointing at a file that no longer exists.
    selected: [{"id": str, "name": str}, ...]
    primary_id: id of the cover that should sort first
    extracted_dir: pre-extracted contents of cbz_path (RAR one-shot extract) —
    read pages from there instead of extracting the archive all over again
    """
    # Bare requests.get per thread — no shared session state to trip over.
    # (Empty selection used to hand ThreadPoolExecutor max_workers=0, which is
    # a ValueError — now it's just a repack with zero covers.)
    ids = [c['id'] for c in selected]
    datas = []
    if ids:
        with ThreadPoolExecutor(max_workers=min(8, len(ids))) as ex:
            datas = list(ex.map(_download_cover, ids))
    variant_pages = [(c['id'], c.get('name', c['id']), d)
                     for c, d in zip(selected, datas) if d]

    variant_pages.sort(key=lambda x: (0 if x[0] == primary_id else 1, x[1]))

    out_path = _rebuild_as_cbz(cbz_path, variant_pages, extracted_dir)
    return len(variant_pages), out_path


def ensure_cbz(path: str, extracted_dir: str | None = None) -> str:
    """Repack a RAR-backed comic (magic bytes, not extension) as a verified CBZ;
    ZIPs pass through untouched. Best-effort by design: the swap only happens
    after the rebuild verifies, so a failed repack leaves the original .cbr
    exactly where it was and we ship that instead — a conversion hiccup must
    never kill a download that already succeeded. Returns the final path."""
    try:
        with open(path, 'rb') as fh:
            is_rar = fh.read(4) == b'Rar!'
    except OSError:
        return path
    if not is_rar:
        return path
    try:
        return _rebuild_as_cbz(path, [], extracted_dir)
    except Exception as e:
        logger.warning(f"ensure_cbz: CBR→CBZ repack failed for {path} — keeping the CBR: {e}")
        return path

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
_NUM_FROM_FNAME_BARE_RE = re.compile(r'(?<!\d)0*(\d{1,4})(?!\d)')


def _num_from_filename(name: str) -> float | None:
    m = _NUM_FROM_FNAME_RE.search(os.path.basename(name))
    return float(m.group(1)) if m else None


def _num_from_filename_broad(name: str) -> float | None:
    """Fallback for pack files like 'Batman 001 (2016)...' — finds the first non-year
    1-4 digit number. Years are stripped by PARENTHESES, not by digit-count: a bare
    \\b\\d{4}\\b strip would (and used to) eat a genuine 4-digit issue number just as
    happily as a year — Detective Comics is a legacy-numbered run past #1000, and every
    real scan-group filename observed wraps the year in parens ("(2024)"), so this is
    strictly narrower without losing any real years."""
    base = re.sub(r'\.\w+$', '', os.path.basename(name))
    if re.search(r'\bannual\b', base, re.IGNORECASE):
        return None
    base = re.sub(r'\(\d{4}\)', '', base)  # remove PARENTHESIZED years only
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


def _pick_issue_file(files: list[str], issue_number: float) -> str | None:
    """The pack-targeting rule, in exactly ONE place: first file whose parsed
    issue number matches. Used by the GetComics pack path here and both
    finalize paths in acquisition — it was copy-pasted three times before
    somebody inevitably fixed a bug in only two of them."""
    return next((f for f in files if _issue_num_from_file(f) == issue_number), None)


def _read_archive_comicinfo(archive) -> str | None:
    names = [n for n in archive.namelist() if n.lower() == 'comicinfo.xml']
    return archive.read(names[0]).decode('utf-8', errors='replace') if names else None


_COMICINFO_NUM_RE = re.compile(r'<Number>\s*(\d+(?:\.\d+)?)\s*</Number>', re.IGNORECASE)


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
                # No forced UNRAR_TOOL: rarfile auto-detects unrar/unar/bsdtar.
                # The old hard-pin to bsdtar predates unar in the image; with a
                # full RAR5 backend available, pinning the partial one (bsdtar
                # chokes on compressed/solid v5) would be self-sabotage.
                with rarfile.RarFile(path, 'r') as rf:
                    xml = _read_archive_comicinfo(rf)
            except Exception:
                return None
        if xml:
            m = _COMICINFO_NUM_RE.search(xml)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return None


def _comicinfo_number_from_dir(d: str) -> float | None:
    """<Number> from an already-extracted archive dir. Root-level ComicInfo.xml
    only — same rule as _read_archive_comicinfo, where a nested one never
    matched the archive readers either."""
    try:
        for f in os.listdir(d):
            if f.lower() == 'comicinfo.xml':
                with open(os.path.join(d, f), 'rb') as fh:
                    xml = fh.read().decode('utf-8', errors='replace')
                m = _COMICINFO_NUM_RE.search(xml)
                return float(m.group(1)) if m else None
    except Exception:
        pass
    return None


# A single issue rarely exceeds this many image pages. A trade, an omnibus, or a
# vertical/webtoon edition (each panel is its own "page") blows well past it — which
# is exactly how an 80-panel webtoon release slipped in mislabeled as a print issue.
_SINGLE_ISSUE_PAGE_MAX = 70


_IMG_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp')


def _count_images_in_dir(d: str) -> int:
    """Image-file count of an extracted archive dir — the RAR half of
    _count_archive_images, shared with the one-shot extract path."""
    n = 0
    for _root, _dirs, files in os.walk(d):
        n += sum(1 for f in files if f.lower().endswith(_IMG_EXTS))
    return n


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
                return _count_images_in_dir(tmpdir)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        return None
    return None


# Webtoon-vs-print, by the numbers. Measured from the live Absolute Superman #21
# incident: the [digital-mobile] webtoon chapter's pages are 800x1280 (ratio 1.60),
# a real print digital rip runs ~1988x3057 (ratio 1.54). Ratio ALONE cannot tell
# them apart — 1.54 vs 1.60 is a rounding error — so the rule takes BOTH prongs:
# phone-width pages (≤1000px; print rips run 1600-2100) AND a tall page shape.
# Medians, not means: one odd spread/credits page must not swing the verdict.
_WEBTOON_MAX_MEDIAN_WIDTH = 1000
_WEBTOON_MIN_MEDIAN_RATIO = 1.5
_DIM_SAMPLE_PAGES = 9
_DIM_MIN_PAGES = 3   # fewer measurable pages than this = not enough signal to condemn


def _sample_page_dims(path: str, extracted_dir: str | None = None) -> list[tuple[int, int]]:
    """(width, height) for up to _DIM_SAMPLE_PAGES images spread evenly through
    the book. Reads the extracted dir when the caller already paid for the RAR
    extract; ZIPs are read member-by-member. Empty list on ANY trouble — no
    Pillow, unreadable archive, un-decodable images — because the dimension
    guard must never reject a file it couldn't actually measure."""
    try:
        from io import BytesIO
        from PIL import Image
    except ImportError:
        return []

    def _dims(blobs) -> list[tuple[int, int]]:
        out = []
        for data in blobs:
            try:
                with Image.open(BytesIO(data)) as im:
                    out.append((im.width, im.height))
            except Exception:
                continue
        return out

    def _spread(names: list) -> list:
        if len(names) <= _DIM_SAMPLE_PAGES:
            return names
        step = len(names) / _DIM_SAMPLE_PAGES
        return [names[int(i * step)] for i in range(_DIM_SAMPLE_PAGES)]

    try:
        if extracted_dir:
            files = []
            for root, _dirs, fnames in os.walk(extracted_dir):
                files.extend(os.path.join(root, f) for f in fnames
                             if f.lower().endswith(_IMG_EXTS))
            files.sort()

            def _read(p):
                with open(p, 'rb') as fh:
                    return fh.read()
            return _dims(_read(p) for p in _spread(files))

        with open(path, 'rb') as fh:
            if fh.read(2) != b'PK':
                return []   # RAR with no pre-extracted dir — measuring would mean
                            # a second full extract; the caller's job to provide it
        with zipfile.ZipFile(path, 'r') as zf:
            names = sorted(n for n in zf.namelist() if n.lower().endswith(_IMG_EXTS))
            return _dims(zf.read(n) for n in _spread(names))
    except Exception:
        return []


def _webtoon_verdict(dims: list[tuple[int, int]]) -> str | None:
    """The human-readable reason this measures as a webtoon, or None if it
    passes. Both prongs must fire — see the constants above for why."""
    import statistics
    dims = [(w, h) for w, h in dims if w > 0]
    if len(dims) < _DIM_MIN_PAGES:
        return None
    med_w = statistics.median(w for w, _h in dims)
    med_r = statistics.median(h / w for w, h in dims)
    if med_w <= _WEBTOON_MAX_MEDIAN_WIDTH and med_r >= _WEBTOON_MIN_MEDIAN_RATIO:
        return (f"pages measure ~{int(med_w)}px wide (h/w {med_r:.2f}) — "
                f"a vertical/webtoon (mobile) rip")
    return None


def _extract_rar_once(path: str) -> str | None:
    """If path is a RAR (magic bytes, not extension), extract it ONCE to a temp
    dir and return the dir. This is the fix for the triple-extract shame spiral:
    page-count, ComicInfo, and variant-inject each used to do their own full
    bsdtar pass over the same 200MB archive. Now they all sip from this one dir.

    Returns None for ZIPs (zipfile reads members cheaply — extracting one would
    be a downgrade) and None when the extract fails, in which case consumers
    fall back to their own attempts — preserving the old 'can't read it? don't
    reject it' semantics exactly. Caller owns cleanup of the returned dir."""
    try:
        with open(path, 'rb') as fh:
            if fh.read(4) != b'Rar!':
                return None
    except OSError:
        return None
    tmpdir = tempfile.mkdtemp(prefix='kometa-rar-')
    try:
        subprocess.run(['bsdtar', '-xf', path, '-C', tmpdir],
                       check=True, capture_output=True, timeout=180)
        return tmpdir
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None


def _verify_single_issue(path: str, issue_number: float, source_name: str | None = None,
                         extracted_dir: str | None = None, page_max: int | None = None) -> None:
    """Reject anything that isn't this single issue — raises WrongIssueError. Three
    guards: the source filename's issue number, the ComicInfo number, and a page
    count (a collection or vertical/webtoon edition dwarfs a single issue). Shared
    by both the GetComics and usenet paths so they accept/reject identically.
    extracted_dir: pre-extracted contents of path (RAR one-shot extract) — the
    ComicInfo and page-count guards read it instead of re-opening the archive.
    page_max: per-series ceiling override for oversized formats (Head Lopper's
    quarterly is a legit 72-page single issue); None = the global default."""
    name = source_name or os.path.basename(path)
    fnum = _num_from_filename(name)
    if fnum is not None and fnum != issue_number:
        raise WrongIssueError(f"file is #{int(fnum)}, expected #{int(issue_number)}")
    cnum = _comicinfo_number_from_dir(extracted_dir) if extracted_dir else _read_cbz_number(path)
    if cnum is not None and cnum != issue_number:
        raise WrongIssueError(f"ComicInfo reports #{int(cnum)}, expected #{int(issue_number)}")
    pages = _count_images_in_dir(extracted_dir) if extracted_dir else _count_archive_images(path)
    limit = page_max or _SINGLE_ISSUE_PAGE_MAX
    if pages is not None and pages > limit:
        raise WrongIssueError(
            f"{pages} pages (limit {limit}) — looks like a collection or vertical/webtoon "
            f"edition, not single issue #{int(issue_number)}")
    # Fourth guard: page DIMENSIONS. The Absolute Superman #21 webtoon chapter
    # was 44 pages — sailed under the count ceiling wearing the right number, so
    # the only tell left is the pages themselves: phone-width and tall.
    verdict = _webtoon_verdict(_sample_page_dims(path, extracted_dir))
    if verdict:
        raise WrongIssueError(f"{verdict} — not print issue #{int(issue_number)}")


def _get_with_retries(url: str, label: str):
    """Streamed GET with the 3-attempt backoff both download paths use. Returns
    a live response the caller MUST close (use `with`). Connection/timeout woes
    retry; an HTTP error status raises immediately — after closing the response,
    because a leaked socket on the failure path is how you run out of file
    descriptors at 3am."""
    last_exc = None
    for attempt in range(3):
        if attempt:
            time.sleep(5 * attempt)
        try:
            r = requests.get(url, stream=True, timeout=120, headers=HEADERS, allow_redirects=True)
            try:
                r.raise_for_status()
            except BaseException:
                r.close()
                raise
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            logger.warning(f"{label} attempt {attempt + 1} failed ({e}), retrying...")
    raise last_exc or RuntimeError(f"{label} failed after retries: {url[:80]}")


# Komga runs as a non-root user (uid 1026) — anything we drop into the library has
# to be world-traversable/readable or the scanner walks straight past it and the
# issue never appears. We used to trust upstream perms: WRONG. SAB, bsdtar, and
# shutil.copy2's copystat have all handed us mode-000 files, and makedirs(exist_ok)
# flat-out REFUSES to re-chmod a dir already sitting at 000 — so a single poisoned
# download could bury a whole series behind a 000 folder (that's the Absolute Catwoman
# ghost). So we stop trusting anyone. Every finalize stamps the perms itself: dirs
# 755, comic files 644, walked from the library root all the way down. Belt AND braces.
_LIBRARY_DIR_MODE = 0o755
_LIBRARY_FILE_MODE = 0o644


def force_readable_tree(dest_dir: str) -> None:
    """Force Komga-readable perms across a just-touched library folder — the placed
    file, any pack-extracted siblings, cover-injected repackages — plus every
    ancestor up to the library root so the scanner can actually walk in. Idempotent,
    best-effort: a chmod we're not allowed to do is logged and skipped, never fatal."""
    try:
        root = os.path.realpath(sources.comics_root())
        p = os.path.realpath(dest_dir)
        # Walk the ancestor chain root..dest_dir and make each component traversable —
        # this is what un-buries a series dir some earlier download left at 000.
        chain = []
        while p.startswith(root):
            chain.append(p)
            if p == root:
                break
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        for d in chain:
            try:
                os.chmod(d, _LIBRARY_DIR_MODE)
            except OSError as e:
                logger.warning(f"chmod dir {d} failed: {e}")
        # Then the folder's own contents.
        for r, dirs, files in os.walk(dest_dir):
            for d in dirs:
                try:
                    os.chmod(os.path.join(r, d), _LIBRARY_DIR_MODE)
                except OSError:
                    pass
            for f in files:
                if os.path.splitext(f)[1].lower() not in _COMIC_LIB_EXTS:
                    continue
                try:
                    os.chmod(os.path.join(r, f), _LIBRARY_FILE_MODE)
                except OSError as e:
                    logger.warning(f"chmod file {f} failed: {e}")
    except Exception as e:  # never let a perm-stamp kill a finished download
        logger.warning(f"force_readable_tree({dest_dir}) failed: {e}")


# Extensions Komga actually serves — the set we bother re-stamping. Kept local and
# explicit rather than importing the divergent COMIC_EXTS zoo scattered elsewhere.
_COMIC_LIB_EXTS = ('.cbz', '.cbr', '.cb7', '.pdf', '.epub')


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
    page_max: int | None = None,
) -> str:
    """
    Download from url, place in library, trigger Komga scan.
    Returns the final file path. Raises on failure.
    Pass tracked_series_id + db_path to enable automatic variant injection.
    page_max: per-series single-issue page ceiling (oversized formats); None = default.
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

    r = _get_with_retries(url, "Download")

    # `with` closes the response even when the body loop blows up mid-stream —
    # before this, an early raise left the socket dangling until GC felt like it.
    with r:
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

    # RAR/CBR: pay the full bsdtar extract ONCE, right here. Verification and
    # variant injection below both read this dir instead of each re-extracting
    # the whole archive (it used to happen three times per issue). None for
    # ZIPs (cheap member reads, no extract needed) or when the extract fails —
    # consumers then fall back to their own readers, same semantics as before.
    rar_dir = _extract_rar_once(staging_path)
    try:
        # Content checks: wrong issue (server filename / ComicInfo) or a collection /
        # webtoon edition (page count). Shared with the usenet finalize so both sources
        # reject the same bad content. Clean up the staging file on rejection.
        try:
            _verify_single_issue(staging_path, issue_number, filename, extracted_dir=rar_dir,
                                 page_max=page_max)
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

        # If it's a ZIP pack containing multiple comics, extract and discard the wrapper.
        # (A pack is always a ZIP, so rar_dir is None here — no stale-dir risk below.)
        extracted = _extract_pack(dest_path, dest_dir)
        if extracted:
            os.remove(dest_path)
            logger.info(f"Pack: {len(extracted)} new file(s) from {os.path.basename(dest_path)}")
            # Find the extracted file that matches the issue we actually requested (Gap J/K)
            target = _pick_issue_file(extracted, issue_number)
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
                    added, dest_path = inject_covers(dest_path, prefs["selected"], prefs["primary_id"],
                                                     extracted_dir=rar_dir)
                    _db.clear_variant_prefs(tracked_series_id, issue_number, db_path)
                    logger.info(f"Injected {added} variant cover(s) into {dest_path}")
            except Exception as e:
                logger.warning(f"Variant injection failed: {e}")

        # No prefs? The CBR still converts — every finalize ships a CBZ. Same
        # verified rebuild seam as injection; a repack failure keeps the CBR.
        # rar_dir belongs to the DOWNLOADED archive: valid for dest_path unless
        # the pack branch above swapped dest_path for an extracted member.
        dest_path = ensure_cbz(dest_path, extracted_dir=None if extracted else rar_dir)
    finally:
        if rar_dir:
            shutil.rmtree(rar_dir, ignore_errors=True)

    # Stamp Komga-readable perms before we tell Komga to look — otherwise a 000
    # file/dir from the move or a cover-inject repackage stays invisible.
    force_readable_tree(dest_dir)

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

    r = _get_with_retries(url, "Trade download")

    with r:
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

    # New arrivals ship as CBZ — same verified rebuild seam, best-effort.
    placed = [ensure_cbz(p) for p in placed]

    force_readable_tree(dest_dir)

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
    return ".cbz"
