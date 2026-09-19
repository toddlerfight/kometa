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
from kometa.naming import (_safe, _resolve_dir, _season_from_entries,
                           _season_from_title, OWNED_EXTS, PIPELINE_EXTS,
                           parse_issue_number, parse_volume_number,
                           scan_folder_numbers,
                           canonical_issue_filename, format_issue_number,
                           is_variant_scan, counts_as_owned, norm_key,
                           find_issue_file)
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


def _rar_entry_count(path: str) -> int | None:
    """How many image entries the archive CLAIMS to hold, per lsar, or None."""
    try:
        out = subprocess.run(['lsar', path], capture_output=True, timeout=60)
    except Exception:
        return None
    names = out.stdout.decode('utf-8', 'replace').splitlines()[1:]
    n = sum(1 for x in names if x.strip().lower().endswith(_IMG_EXTS))
    return n or None


def _extract_rar_into(path: str, tmpdir: str) -> bool:
    """Extract a RAR into tmpdir. True if we got a COMPLETE extraction.

    bsdtar first — fast, and it handles most of what we see. But libarchive only
    reads STORE-method RAR5, and on a compressed one it does the worst possible
    thing: writes SOME entries, then dies. "Did anything land?" therefore is not
    a success test — it green-lit a 9-page extract of a 26-page comic, and the
    repack then sealed that into a CBZ and deleted the original. Count against
    what lsar says should be there, and let unar have its turn whenever bsdtar
    came up short.

    Exit codes decide nothing here — unar returns 1 while cheerfully extracting
    all 26 pages. Only the file count on disk does. When lsar can't tell us the
    expected count we take the best extraction we can get, but we still run both
    and keep the fuller one rather than trusting whoever went first."""
    want = _rar_entry_count(path)
    best_dir, best_n = None, 0
    scratch = []
    try:
        for cmd_for in (lambda d: ['bsdtar', '-xf', path, '-C', d],
                        lambda d: ['unar', '-q', '-D', '-o', d, path]):
            d = tempfile.mkdtemp(prefix='kometa-x-')
            scratch.append(d)
            try:
                subprocess.run(cmd_for(d), capture_output=True, timeout=300)
            except Exception:
                continue
            n = _count_images_in_dir(d)
            if n > best_n:
                best_dir, best_n = d, n
            if want and best_n >= want:
                break                      # complete — no need for a second pass
        if not best_dir or not best_n:
            return False
        if want and best_n < want:
            logger.warning(f"RAR extract incomplete for {os.path.basename(path)}: "
                           f"{best_n}/{want} pages — refusing to treat it as readable")
            return False
        for name in os.listdir(best_dir):
            shutil.move(os.path.join(best_dir, name), os.path.join(tmpdir, name))
        return True
    finally:
        for d in scratch:
            shutil.rmtree(d, ignore_errors=True)


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
            if not _extract_rar_into(path, tmpdir):
                raise RuntimeError(f"no extractor could open {os.path.basename(path)}")
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

        # ZERO wasn't a tight enough bar. bsdtar on a compressed RAR5 writes SOME
        # entries and then dies, so a 26-page comic came through as 9 pages, got
        # sealed into a CBZ, and the original was deleted behind it. Count what
        # the source CLAIMS to hold and refuse anything short of it — a partial
        # rebuild is a destroyed comic wearing a valid ZIP header.
        claimed = _rar_entry_count(src_path)
        if claimed:
            got = sum(1 for name in zipfile.ZipFile(tmp_path).namelist()
                      if name.lower().endswith(_IMG_EXTS))
            if got < claimed:
                raise RuntimeError(
                    f"rebuild kept only {got} of {claimed} pages — refusing the swap")

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


# A .zip whose only entry is another archive is packaging, not a comic. GetComics
# serves a real slice of its catalogue double-bagged like that, and looking only at
# the OUTER magic bytes let every one of them through: a ZIP wrapping a RAR isn't a
# RAR, so ensure_cbz waved it past. Four landed that way in one session and read as
# unowned forever after — ownership opens an archive and refuses one that can't
# produce a page, correctly, so they sat in the library counting for nothing.
_WRAPPED_ARCHIVE_EXTS = ('.cbr', '.cbz', '.rar', '.zip')
_MAX_UNWRAP_DEPTH = 3


def _archive_pages(path: str) -> int:
    try:
        with zipfile.ZipFile(path) as z:
            return len([n for n in z.namelist()
                        if n.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))])
    except Exception:
        return 0


def _wrapped_archive_entry(path: str) -> str | None:
    """The sole entry of `path` if it is itself an archive, else None. One entry
    only: a multi-entry zip is a comic (or a pack), never a wrapper."""
    try:
        with zipfile.ZipFile(path) as z:
            entries = [n for n in z.namelist() if not n.endswith('/')]
    except Exception:
        return None
    if len(entries) != 1:
        return None
    return entries[0] if entries[0].lower().endswith(_WRAPPED_ARCHIVE_EXTS) else None


def _unwrap_archive(path: str, inner_name: str, extracted_dir: str | None,
                    depth: int) -> str | None:
    """Lift the inner archive out and run it back through ensure_cbz. Returns the
    new path, or None if anything about it failed — in which case the caller keeps
    the file exactly as delivered. Never trades a real file for a broken one."""
    parent = os.path.dirname(path) or None
    tmpd = tempfile.mkdtemp(dir=parent, prefix='.unwrap_')
    try:
        # Stream it out under a name WE choose — never the archive's own, which is
        # attacker-controlled text and the whole shape of a zip-slip.
        inner_path = os.path.join(tmpd, os.path.basename(inner_name) or 'inner')
        with zipfile.ZipFile(path) as z, z.open(inner_name) as src, \
                open(inner_path, 'wb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        built = ensure_cbz(inner_path, extracted_dir, _depth=depth + 1)
        if not _archive_pages(built):
            logger.warning(f"unwrap: {os.path.basename(path)} held no readable pages "
                           f"— keeping the file as delivered")
            return None
        target = os.path.splitext(path)[0] + '.cbz'
        staged = os.path.join(tmpd, 'staged.cbz')
        shutil.move(built, staged)
        os.replace(staged, target)          # atomic into place
        if target != path and os.path.exists(path):
            os.remove(path)
        logger.info(f"unwrap: {os.path.basename(path)} was a wrapper — "
                    f"lifted out {os.path.basename(inner_name)}")
        return target
    except Exception as e:
        logger.warning(f"unwrap failed for {path}: {e}")
        return None
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
        if os.path.isdir(tmpd):
            logger.warning(f"unwrap: temp dir survived cleanup: {tmpd}")


def ensure_cbz(path: str, extracted_dir: str | None = None, _depth: int = 0) -> str:
    """Repack a RAR-backed comic (magic bytes, not extension) as a verified CBZ,
    and lift a comic out of a zip that is only wrapping it. Plain ZIPs pass through
    untouched. Best-effort by design: a swap only happens after the rebuild
    verifies, so a failed repack or unwrap leaves the original exactly where it was
    and we ship that instead — a conversion hiccup must never kill a download that
    already succeeded. Returns the final path."""
    try:
        with open(path, 'rb') as fh:
            magic = fh.read(4)
    except OSError:
        return path
    if magic == b'Rar!':
        try:
            return _rebuild_as_cbz(path, [], extracted_dir)
        except Exception as e:
            logger.warning(f"ensure_cbz: CBR→CBZ repack failed for {path} — keeping the CBR: {e}")
            return path
    if magic[:2] == b'PK' and _depth < _MAX_UNWRAP_DEPTH:
        inner = _wrapped_archive_entry(path)
        if inner:
            unwrapped = _unwrap_archive(path, inner, extracted_dir, _depth)
            if unwrapped:
                return unwrapped
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


class UnreadableArchiveError(ValueError):
    """The bytes arrived and the archive cannot be opened — no pages in it.

    Deliberately NOT a DuplicateIssueError: a dupe means "you already have this",
    which parks the row quietly. This means "what we were sold is not a comic",
    which has to FAIL loudly and blacklist the source, or the next retry buys the
    same corpse. That is not hypothetical — a 424MB file with a valid ZIP header
    and no central directory lived in the library for two weeks, re-placed out of
    staging on every retry and re-stamped 'done' each time, while ownership (which
    does open the file) kept correctly reporting the issue as missing."""


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
_COMICINFO_SERIES_RE = re.compile(r'<Series>(.*?)</Series>', re.IGNORECASE | re.DOTALL)


def _read_comicinfo_xml(path: str) -> str | None:
    """The raw ComicInfo.xml out of an archive, or None. Detects format by magic
    bytes, not extension. Split out of _read_cbz_number so the Series guard can
    ask the same file the same question without opening it a second time."""
    try:
        with open(path, 'rb') as fh:
            magic = fh.read(4)
        if magic[:2] == b'PK':  # ZIP — real CBZ or mislabeled
            with zipfile.ZipFile(path, 'r') as zf:
                return _read_archive_comicinfo(zf)
        if magic[:4] == b'Rar!':  # RAR — real CBR or mislabeled .cbz
            try:
                import rarfile
                # No forced UNRAR_TOOL: rarfile auto-detects unrar/unar/bsdtar.
                # The old hard-pin to bsdtar predates unar in the image; with a
                # full RAR5 backend available, pinning the partial one (bsdtar
                # chokes on compressed/solid v5) would be self-sabotage.
                with rarfile.RarFile(path, 'r') as rf:
                    return _read_archive_comicinfo(rf)
            except Exception:
                return None
    except Exception:
        pass
    return None


def _read_cbz_number(path: str) -> float | None:
    """Return the issue number from ComicInfo.xml."""
    xml = _read_comicinfo_xml(path)
    if xml:
        m = _COMICINFO_NUM_RE.search(xml)
        if m:
            return float(m.group(1))
    return None


def _comicinfo_series(xml: str | None) -> str | None:
    """<Series> out of a ComicInfo blob."""
    if not xml:
        return None
    m = _COMICINFO_SERIES_RE.search(xml)
    return m.group(1).strip() if m and m.group(1).strip() else None


def _series_disagrees(want: str | None, got: str | None) -> bool:
    """Does the archive's own ComicInfo name a DIFFERENT series than the one we
    asked for? Tolerant on purpose: publishers pad the field with volume years
    and subtitles ('Avengers (2012-)' vs 'Avengers'), so containment either way
    counts as agreement and only two unrelated names are a rejection.

    Earned the hard way. A file landed as 'Infinity #003.cbz' carrying
    <Series>Batman: The Adventures Continue (2020-)</Series> and <Number>3</Number>.
    The number guard right below this compared 3 to 3 and waved it through, and
    Komga then renamed the whole Infinity folder after the impostor."""
    if not want or not got:
        return False
    w, g = norm_key(want), norm_key(got)
    if not w or not g:
        return False
    return w not in g and g not in w


def _comicinfo_xml_from_dir(d: str) -> str | None:
    """Root-level ComicInfo.xml from an already-extracted dir — same rule as
    _read_archive_comicinfo, where a nested one never matched either."""
    try:
        for f in os.listdir(d):
            if f.lower() == 'comicinfo.xml':
                with open(os.path.join(d, f), 'rb') as fh:
                    return fh.read().decode('utf-8', errors='replace')
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
                if not _extract_rar_into(path, tmpdir):
                    return None
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
        if not _extract_rar_into(path, tmpdir):
            raise RuntimeError("no extractor could open it")
        return tmpdir
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None


def _verify_single_issue(path: str, issue_number: float, source_name: str | None = None,
                         extracted_dir: str | None = None, page_max: int | None = None,
                         series_title: str | None = None) -> None:
    """Reject anything that isn't this single issue — raises WrongIssueError. Three
    guards: the source filename's issue number, the ComicInfo number, and a page
    count (a collection or vertical/webtoon edition dwarfs a single issue). Shared
    by both the GetComics and usenet paths so they accept/reject identically.
    extracted_dir: pre-extracted contents of path (RAR one-shot extract) — the
    ComicInfo and page-count guards read it instead of re-opening the archive.
    page_max: per-series ceiling override for oversized formats (Head Lopper's
    quarterly is a legit 72-page single issue); None = the global default."""
    name = source_name or os.path.basename(path)
    # Season guard. Three seasons of Batman: The Adventures Continue are three
    # separate runs wearing the same base title and the same issue numbers 1..8,
    # so "#1" alone is not an identity — it matched all three, and Season One's
    # folder filled up with Season Two. A series that names no season is season 1
    # (that's what "Batman: The Adventures Continue" IS); a candidate that names
    # no season stays innocent, because plenty of legitimate releases just don't
    # say. We only reject when the candidate declares a season and it's the wrong
    # one — loud, specific, and never a guess.
    want_season = _season_from_title(series_title) or 1
    got_season = _season_from_entries(extracted_dir) or _season_from_title(name)
    if got_season is not None and got_season != want_season:
        raise WrongIssueError(
            f"file is Season {got_season}, expected Season {want_season} "
            f"({series_title or 'this series'})")
    fnum = _num_from_filename(name)
    if fnum is not None and fnum != issue_number:
        raise WrongIssueError(f"file is #{int(fnum)}, expected #{int(issue_number)}")
    cxml = _comicinfo_xml_from_dir(extracted_dir) if extracted_dir else _read_comicinfo_xml(path)
    cnum = None
    if cxml:
        m = _COMICINFO_NUM_RE.search(cxml)
        cnum = float(m.group(1)) if m else None
    if cnum is not None and cnum != issue_number:
        raise WrongIssueError(f"ComicInfo reports #{int(cnum)}, expected #{int(issue_number)}")
    # The number alone is not an identity. Every run in print has a #3, so a
    # Batman #3 answers "is this issue 3?" perfectly well while being the wrong
    # comic entirely — which is exactly how one landed in the Infinity folder
    # and got the whole series renamed after it in Komga.
    cseries = _comicinfo_series(cxml)
    if _series_disagrees(series_title, cseries):
        raise WrongIssueError(
            f"ComicInfo says this is {cseries!r}, expected {series_title!r}")
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


# Extensions we bother re-stamping: everything ownership counts, plus .epub —
# Komga serves those even though no scanner claims them. (The zoo this comment
# used to disavow is dead; naming.py holds the one true set now.)
_COMIC_LIB_EXTS = OWNED_EXTS | {'.epub'}


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
    on_bytes_done=None,
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

    # Bytes complete — extract/verify/inject below is finalize, not downloading.
    if on_bytes_done:
        try:
            on_bytes_done()
        except Exception:
            pass

    size = os.path.getsize(staging_path)
    if size < 1024:
        os.remove(staging_path)
        raise ValueError(f"Downloaded file too small ({size} bytes) — likely an error page")

    # RAR/CBR: pay the full bsdtar extract ONCE, right here. Verification and
    # variant injection below both read this dir instead of each re-extracting
    # the whole archive (it used to happen three times per issue). None for
    # ZIPs (cheap member reads, no extract needed) or when the extract fails —
    # consumers then fall back to their own readers, same semantics as before.
    # A ZIP of comics is a SHELF, not an issue — decide that before anything
    # treats the wrapper as the comic. The single-issue checks used to run on the
    # wrapper first: a '001-005' pack reads as issue #1 by filename, so asking
    # it for #2 got the whole shelf thrown out as the wrong issue before a single
    # member was looked at. And a pack whose members we already owned extracted
    # nothing, so the wrapper fell through and got filed as the comic itself.
    is_pack = _pack_comic_count(staging_path) > 1
    rar_dir = None if is_pack else _extract_rar_once(staging_path)
    extracted: list[str] = []
    try:
        if not dest_dir:
            dest_dir = _resolve_dir(sources.comics_root(), publisher or "Unknown", title)
        os.makedirs(dest_dir, exist_ok=True)

        if is_pack:
            try:
                extracted = _extract_pack(staging_path, dest_dir, series_title=title)
            finally:
                os.remove(staging_path)
            logger.info(f"Pack: {len(extracted)} new file(s) from {filename}")
            target = _pick_issue_file(extracted, issue_number)
            if target is None:
                # Maybe an earlier grab of this same chunk already shelved it —
                # the issue is here, and that's an acquisition, not a miss.
                already = find_issue_file(dest_dir, title, issue_number)
                if already and counts_as_owned(already, title):
                    logger.info(f"Pack: #{format_issue_number(issue_number)} already on the shelf: {already}")
                    force_readable_tree(dest_dir)
                    if extracted:
                        try:
                            komga_scan_fn()
                        except Exception as e:
                            logger.warning(f"Komga scan trigger failed: {e}")
                    return already
                # Extracted extras stay on disk — the next sync picks them up.
                raise WrongIssueError(
                    f"Pack did not contain issue #{format_issue_number(issue_number)} "
                    f"(found: {[os.path.basename(f) for f in extracted]})"
                )
            # The wrapper skipped the single-issue checks; the member doesn't.
            try:
                _verify_single_issue(target, issue_number, os.path.basename(target),
                                     page_max=page_max, series_title=title)
            except WrongIssueError:
                os.remove(target)
                raise
            dest_path = target
            # Other newly-extracted issues stay on disk — next sync picks them up
        else:
            # Content checks: wrong issue (server filename / ComicInfo) or a collection /
            # webtoon edition (page count). Shared with the usenet finalize so both sources
            # reject the same bad content. Clean up the staging file on rejection.
            try:
                _verify_single_issue(staging_path, issue_number, filename, extracted_dir=rar_dir,
                                     page_max=page_max, series_title=title)
            except WrongIssueError:
                os.remove(staging_path)
                raise

            dest_path = os.path.join(dest_dir, _safe(filename))
            if os.path.exists(dest_path):
                os.remove(staging_path)
                raise DuplicateIssueError(
                    f"{filename} already exists in library — GetComics served an existing issue"
                )
            shutil.move(staging_path, dest_path)
            dest_path = _fix_extension(dest_path)
            logger.info(f"Placed: {dest_path}")

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

        # The library's own name, whatever the server chose to call it. Only the
        # pack path did this before, so a single-issue grab kept its scene name
        # (or its percent-escapes) and drifted from every other acquisition path.
        dest_path = _canonicalize_placed(dest_path, title, issue_number)

        # Last gate before we call this an acquisition. Everything upstream
        # verifies a CLAIM — the server's filename, ComicInfo, the download
        # client's "completed" — and none of it opens the file we just placed.
        if not counts_as_owned(dest_path, title):
            try:
                os.remove(dest_path)
            except OSError:
                pass
            raise UnreadableArchiveError(
                f"Placed file for #{format_issue_number(issue_number)} has no readable "
                f"pages — rejecting it rather than recording a comic we don't have"
            )
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
    on_bytes_done=None,
    series_title: str | None = None,
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

    # Bytes are all here — everything below is finalize (extract, CBZ rebuild),
    # which on a 1GB pack grinds for minutes. Let the caller flip the queue row
    # off "downloading" so a 100% bar doesn't read as stuck.
    if on_bytes_done:
        try:
            on_bytes_done()
        except Exception:
            pass

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
    extracted = _extract_pack(dest_path, dest_dir, series_title=series_title)
    if extracted:
        os.remove(dest_path)
        placed = extracted
        logger.info(f"Trade pack: {len(extracted)} file(s) from {os.path.basename(dest_path)}")
    elif _pack_comic_count(dest_path) > 1:
        # A pack whose every file was already in the library: nothing new, and
        # leaving the raw multi-comic ZIP behind plants a fake "trade" Komga
        # can't read. Remove it and report the dupe honestly.
        os.remove(dest_path)
        raise DuplicateIssueError("Pack contained no files not already in library")

    # New arrivals ship as CBZ — same verified rebuild seam, best-effort.
    placed = [ensure_cbz(p) for p in placed]

    force_readable_tree(dest_dir)

    if komga_scan_fn:
        try:
            komga_scan_fn()
        except Exception as e:
            logger.warning(f"Komga scan after trade failed: {e}")
    return placed


def _pack_comic_count(zip_path: str) -> int:
    """How many comic files a ZIP holds (0 for a non-ZIP / plain comic archive)."""
    try:
        if not zipfile.is_zipfile(zip_path):
            return 0
        with zipfile.ZipFile(zip_path, 'r') as zf:
            return sum(1 for n in zf.namelist()
                       if os.path.splitext(n)[1].lower() in PIPELINE_EXTS and not n.startswith('__'))
    except Exception:
        return 0


def _extract_pack(zip_path: str, dest_dir: str, series_title: str | None = None) -> list[str]:
    """If zip_path is a ZIP of comic files, extract new ones. Returns paths of extracted files.

    Pass series_title and every member lands under the library's own name —
    'New Avengers #014.cbz' — and dedupes on the ISSUE NUMBER. Without it a pack
    keeps whatever the scene called its files, and the skip check below compares
    filename STEMS: 'New Avengers 014 (2014) (Digital) (Zone-Empire)' shares no
    stem with the 'New Avengers #014' already sitting in that folder, so the
    pack cheerfully extracted a second copy of comics we already owned. Forty
    issues in, that's a folder where every issue exists twice under two names
    and neither acquisition path can see the other's work."""
    try:
        if not zipfile.is_zipfile(zip_path):
            return []
        with zipfile.ZipFile(zip_path, 'r') as zf:
            comics = [n for n in zf.namelist()
                      if os.path.splitext(n)[1].lower() in PIPELINE_EXTS and not n.startswith('__')]
            if len(comics) <= 1:
                return []
            # What the folder already holds, by number — the question the stem
            # comparison could never answer. Computed once; new arrivals are
            # added as we go so a pack carrying #14 twice only lands it once.
            have = scan_folder_numbers(dest_dir, series_title or "") if series_title else set()
            extracted = []
            for name in comics:
                fname = os.path.basename(name)
                if not fname:
                    continue
                stem, ext = os.path.splitext(fname)
                num = parse_issue_number(fname, series_title or "") if series_title else None
                # Two kinds of member must keep the name they arrived with.
                # A "Cover ONLY" or variant scan carries a real issue number but
                # isn't that issue — five variants of #001 would all claim
                # 'Series #001.cbz' and four would lose. And a COLLECTED EDITION
                # ('Transmetropolitan Vol 03') has a volume number, not an issue
                # number; renaming it to '#003.cbz' throws away the one fact that
                # says it's a trade and makes it indistinguishable from issue 3.
                canonical = (series_title and num is not None
                             and not is_variant_scan(fname)
                             and parse_volume_number(fname) is None)
                if canonical:
                    if num in have:
                        logger.info(f"Pack: skipping {fname} — already own #{format_issue_number(num)}")
                        continue
                    fname = canonical_issue_filename(series_title, num, ext)
                    stem, ext = os.path.splitext(fname)
                # The pipeline converts CBR→CBZ on the way in and deletes the
                # source, so a re-downloaded pack must recognize its own previous
                # delivery under the NEW extension — comparing only the pack's
                # raw name re-extracted (and re-converted) the entire pack on
                # every retry, and the all-dupes guard below never fired.
                if any(os.path.exists(os.path.join(dest_dir, stem + e)) for e in {ext.lower(), '.cbz'}):
                    logger.info(f"Pack: skipping {fname} — already in library")
                    continue
                out = os.path.join(dest_dir, fname)
                with zf.open(name) as src, open(out, 'wb') as dst:
                    dst.write(src.read())
                out = _fix_extension(out)
                if canonical:
                    have.add(num)
                extracted.append(out)
                logger.info(f"Pack extracted: {out}")
            return extracted
    except Exception as e:
        logger.warning(f"Pack extraction failed: {e}")
        return []


def _canonicalize_placed(path: str, title: str | None, issue_number: float) -> str:
    """Rename a placed single issue to the library's name. Returns the new path
    (or the original when there's nothing to do). Never clobbers: if the
    canonical name is already taken by a DIFFERENT file, leave this one alone
    and let the caller's ownership check decide which survives."""
    if not title:
        return path
    ext = os.path.splitext(path)[1].lower()
    want = os.path.join(os.path.dirname(path), canonical_issue_filename(title, issue_number, ext))
    if want == path or os.path.exists(want):
        return path
    try:
        os.rename(path, want)
        return want
    except OSError as e:
        logger.warning(f"Could not canonicalize {path}: {e}")
        return path


def _server_filename(response, hint_filename: str | None, url: str) -> str | None:
    """Return the real filename from the server, or None if unresolvable."""
    from urllib.parse import unquote
    cd = response.headers.get("content-disposition", "")
    if cd:
        m = re.search(r"filename\*?=(?:UTF-8'')?[\"']?([^\"'\n;]+)", cd, re.IGNORECASE)
        if m:
            name = unquote(m.group(1).strip().strip("\"'"))
            if name and any(name.lower().endswith(e) for e in PIPELINE_EXTS):
                return name
    if hint_filename and any(hint_filename.lower().endswith(e) for e in PIPELINE_EXTS):
        return unquote(hint_filename)
    # unquote here too. The Content-Disposition branch above has always decoded,
    # but this fallback handed back the raw URL segment — so a GetComics link
    # with no CD header put "Avengers%20World%20018%20%282015%29.cbz" on disk,
    # percent-escapes and all. Every later reader then failed to find an issue
    # number in it and the library reported an issue it actually owned as missing.
    basename = unquote(url.rsplit("/", 1)[-1].split("?")[0])
    if basename and any(basename.lower().endswith(e) for e in PIPELINE_EXTS):
        return basename
    return None


def _detect_ext(response, hint_filename: str | None, url: str) -> str:
    for source in [
        response.headers.get("content-disposition", ""),
        hint_filename or "",
        url,
    ]:
        # Ordered substring probe, NOT a membership test — don't swap in
        # PIPELINE_EXTS (sets don't order, and priority here is deliberate).
        for ext in (".cbz", ".cbr", ".zip", ".rar"):
            if ext in source.lower():
                return ".cbz" if ext == ".zip" else ext
    return ".cbz"
