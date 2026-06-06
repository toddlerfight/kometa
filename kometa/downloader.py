import io
import os
import re
import time
import shutil
import logging
import zipfile
import requests

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


def inject_covers(cbz_path: str, selected: list, primary_id: str) -> int:
    """
    Prepend selected variant cover images to cbz_path. Writes back in place.
    CBR input is converted to CBZ. Returns count of images injected.
    selected: [{"id": str, "name": str}, ...]
    primary_id: id of the cover that should sort first
    """
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'darwin', 'mobile': False}
        )
    except ImportError:
        raise RuntimeError("cloudscraper not installed — cannot fetch cover images")

    variant_pages = []
    for cover in selected:
        url = S3_LARGE.format(cover['id'])
        r = scraper.get(url, headers={'Referer': _LOCG_BASE + '/'})
        if r.status_code == 200 and r.content:
            variant_pages.append((cover['id'], cover.get('name', cover['id']), r.content))

    variant_pages.sort(key=lambda x: (0 if x[0] == primary_id else 1, x[1]))

    is_rar = cbz_path.lower().endswith('.cbr')
    if is_rar:
        try:
            import rarfile
            rarfile.UNRAR_TOOL = 'bsdtar'
            opener = rarfile.RarFile
        except ImportError:
            raise RuntimeError("rarfile not installed — cannot read CBR")
    else:
        opener = zipfile.ZipFile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:
        cover_names = []
        for i, (_vid, name, data) in enumerate(variant_pages):
            safe = re.sub(r'[^\w\s\-]', '', name).strip().replace(' ', '_')
            fname = f"{str(i).zfill(3)}_cover_{safe}.jpg"
            zf.writestr(fname, data)
            cover_names.append(fname)

        with opener(cbz_path, 'r') as src:
            orig_names = sorted(src.namelist())
            comic_info_data = None
            for name in orig_names:
                if name.lower() == 'comicinfo.xml':
                    comic_info_data = src.read(name)
                else:
                    zf.writestr(name, src.read(name))

        if comic_info_data is not None:
            comic_info_data = _patch_comic_info(comic_info_data, len(cover_names))
            zf.writestr('ComicInfo.xml', comic_info_data)

    out_path = re.sub(r'\.cbr$', '.cbz', cbz_path, flags=re.IGNORECASE)
    buf.seek(0)
    with open(out_path, 'wb') as f:
        f.write(buf.read())

    return len(variant_pages)

STAGING = os.environ.get("KOMETA_DOWNLOADS", "/downloads")
COMICS_ROOT = os.environ.get("COMICS_ROOT", "/comics")
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


def _safe(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "-", name)
    name = re.sub(r'-+', '-', name)   # collapse consecutive dashes
    return name.strip('-').strip()


_PUB_NOISE = re.compile(r'\b(comics?|studios?|publishing|entertainment|press|inc|llc|productions?)\b', re.I)


def _pub_key(s: str) -> str:
    """Strip publisher suffixes and punctuation for fuzzy matching."""
    return re.sub(r'[^a-z0-9]', '', _PUB_NOISE.sub('', s).lower())


def _resolve_dir(root: str, publisher: str, title: str) -> str:
    """
    Find the best matching publisher+title directory under root.
    Strips common suffixes so 'Image' matches 'Image Comics', handles
    case differences, and prefers the most-populated dir when ambiguous.
    Falls back to safe-computed names if nothing matches.
    """
    safe_pub = _safe(publisher)
    safe_title = _safe(title)
    pub_key = _pub_key(publisher)

    # Score candidate publisher dirs: exact key match beats prefix, more subdirs wins ties
    best, best_score = None, (-1, -1)
    try:
        for entry in os.listdir(root):
            if not os.path.isdir(os.path.join(root, entry)):
                continue
            entry_key = _pub_key(entry)
            if not entry_key or not pub_key:
                continue
            if entry_key == pub_key:
                exact = 1
            elif entry_key.startswith(pub_key) or pub_key.startswith(entry_key):
                exact = 0
            else:
                continue
            # Count subdirectories as tiebreaker — more content = more canonical
            try:
                subdirs = sum(1 for e in os.listdir(os.path.join(root, entry))
                              if os.path.isdir(os.path.join(root, entry, e)))
            except OSError:
                subdirs = 0
            score = (exact, subdirs)
            if score > best_score:
                best, best_score = entry, score
    except OSError:
        pass

    if best:
        safe_pub = best

    pub_dir = os.path.join(root, safe_pub)

    # Find existing series dir — case-insensitive
    try:
        for entry in os.listdir(pub_dir):
            if entry.lower() == safe_title.lower() and os.path.isdir(os.path.join(pub_dir, entry)):
                return os.path.join(pub_dir, entry)
    except OSError:
        pass

    return os.path.join(pub_dir, safe_title)


def _issue_year(store_date: str | None) -> int | None:
    if store_date and len(store_date) >= 4:
        try:
            return int(store_date[:4])
        except ValueError:
            pass
    return None


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
    os.makedirs(STAGING, exist_ok=True)

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
    check_dir = dest_dir or _resolve_dir(COMICS_ROOT, publisher or "Unknown", title)
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
    staging_path = os.path.join(STAGING, filename)
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

    # Check 1: server filename issue number — catches CBR files and anything without
    # ComicInfo.xml. Only fires when we got a real server name (not our fallback).
    fname_num = _num_from_filename(filename)
    if fname_num is not None and fname_num != issue_number:
        os.remove(staging_path)
        raise WrongIssueError(
            f"Server filename '{filename}' is issue #{int(fname_num)}, expected #{int(issue_number)}"
        )

    # Check 2: ComicInfo.xml embedded number — definitive for CBZ and CBR.
    found_num = _read_cbz_number(staging_path)
    if found_num is not None and found_num != issue_number:
        os.remove(staging_path)
        raise WrongIssueError(
            f"ComicInfo.xml reports issue #{int(found_num)}, expected #{int(issue_number)} — "
            f"GetComics placeholder not yet replaced"
        )

    if not dest_dir:
        dest_dir = _resolve_dir(COMICS_ROOT, publisher or "Unknown", title)
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
