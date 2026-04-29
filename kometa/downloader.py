import os
import re
import shutil
import logging
import requests

logger = logging.getLogger(__name__)

STAGING = os.environ.get("KOMETA_DOWNLOADS", "/downloads")
COMICS_ROOT = os.environ.get("COMICS_ROOT", "/comics")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "-", name).strip()


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
) -> str:
    """
    Download from url, place in library, trigger Komga scan.
    Returns the final file path.
    Raises on failure.
    """
    os.makedirs(STAGING, exist_ok=True)

    r = requests.get(url, stream=True, timeout=120, headers=HEADERS, allow_redirects=True)
    r.raise_for_status()

    ext = _detect_ext(r, hint_filename, url)
    num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
    year = _issue_year(store_date)
    year_str = f" ({year})" if year else ""
    num_str = f"{num_int:03d}" if isinstance(num_int, int) else str(num_int)

    safe_title = _safe(title)
    safe_pub = _safe(publisher or "Unknown")
    filename = f"{safe_title} #{num_str}{year_str}{ext}"

    staging_path = os.path.join(STAGING, filename)
    with open(staging_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)

    size = os.path.getsize(staging_path)
    if size < 1024:
        os.remove(staging_path)
        raise ValueError(f"Downloaded file too small ({size} bytes) — likely an error page")

    dest_dir = os.path.join(COMICS_ROOT, safe_pub, safe_title)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    shutil.move(staging_path, dest_path)
    logger.info(f"Placed: {dest_path}")

    try:
        komga_scan_fn()
    except Exception as e:
        logger.warning(f"Komga scan trigger failed: {e}")

    return dest_path


def _detect_ext(response, hint_filename: str | None, url: str) -> str:
    for source in [
        response.headers.get("content-disposition", ""),
        hint_filename or "",
        url,
    ]:
        for ext in (".cbz", ".cbr", ".zip", ".rar"):
            if ext in source.lower():
                return ext
    ct = response.headers.get("content-type", "")
    if "zip" in ct:
        return ".cbz"
    return ".cbz"
