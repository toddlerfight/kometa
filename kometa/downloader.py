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
        for i, (vid, name, data) in enumerate(variant_pages):
            safe = re.sub(r'[^\w\s\-]', '', name).strip().replace(' ', '_')
            zf.writestr(f"{str(i).zfill(3)}_cover_{safe}.jpg", data)
        with opener(cbz_path, 'r') as src:
            for name in sorted(src.namelist()):
                zf.writestr(name, src.read(name))

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
        raise last_exc

    ext = _detect_ext(r, hint_filename, url)
    num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
    year = _issue_year(store_date)
    year_str = f" ({year})" if year else ""
    num_str = f"{num_int:03d}" if isinstance(num_int, int) else str(num_int)

    safe_title = _safe(title)
    safe_pub = _safe(publisher or "Unknown")
    filename = f"{safe_title} #{num_str}{year_str}{ext}"

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

    if not dest_dir:
        dest_dir = _resolve_dir(COMICS_ROOT, publisher or "Unknown", title)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    shutil.move(staging_path, dest_path)
    logger.info(f"Placed: {dest_path}")

    # If it's a ZIP pack containing multiple comics, extract and discard the wrapper
    extracted = _extract_pack(dest_path, dest_dir)
    if extracted:
        os.remove(dest_path)
        logger.info(f"Pack extracted: {len(extracted)} files from {dest_path}")
        dest_path = extracted[0]

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
    """If zip_path is a ZIP containing multiple comic files, extract them. Returns extracted paths."""
    COMIC_EXTS = {'.cbz', '.cbr'}
    try:
        if not zipfile.is_zipfile(zip_path):
            return []
        with zipfile.ZipFile(zip_path, 'r') as zf:
            comics = [n for n in zf.namelist()
                      if os.path.splitext(n)[1].lower() in COMIC_EXTS and not n.startswith('__')]
            if len(comics) <= 1:
                return []
            extracted = []
            for name in comics:
                filename = os.path.basename(name)
                if not filename:
                    continue
                out = os.path.join(dest_dir, filename)
                with zf.open(name) as src, open(out, 'wb') as dst:
                    dst.write(src.read())
                extracted.append(out)
                logger.info(f"Extracted: {out}")
            return extracted
    except Exception as e:
        logger.warning(f"Pack extraction failed: {e}")
        return []


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
