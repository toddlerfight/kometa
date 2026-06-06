"""Pure parsing helpers — identify comic issues from filenames and normalize
search/URL strings. Text in, value out (plus directory scans that only read
names). No DB, no clients, no app state — trivially testable in isolation.
"""
import os
import re


def parse_issue_number(filename: str, series_title: str = "") -> float | None:
    name = os.path.splitext(filename)[0]
    # #001 or #1.5
    m = re.search(r'#(\d+(?:\.\d+)?)', name)
    if m:
        return float(m.group(1))
    # Issue 001
    m = re.search(r'\bIssue\s+(\d+(?:\.\d+)?)\b', name, re.IGNORECASE)
    if m:
        return float(m.group(1))
    # Strip series title then find first number under 1000 (avoids years)
    remainder = name
    if series_title:
        remainder = re.sub(re.escape(series_title), '', name, count=1, flags=re.IGNORECASE).strip(' -_')
    for m in re.finditer(r'\b(\d+(?:\.\d+)?)\b', remainder):
        val = float(m.group(1))
        if val < 1000:
            return val
    return None


def scan_folder_numbers(folder_path: str, series_title: str = "") -> set[float]:
    exts = {'.cbz', '.cbr', '.zip', '.rar', '.pdf'}
    numbers = set()
    try:
        for name in os.listdir(folder_path):
            if os.path.splitext(name)[1].lower() in exts:
                num = parse_issue_number(name, series_title)
                if num is not None:
                    numbers.add(num)
    except Exception:
        pass
    return numbers


def find_issue_file(folder_path: str, series_title: str, number: float) -> str | None:
    """Scan folder_path for a comic file matching issue number. Returns full path or None."""
    if not folder_path or not os.path.isdir(folder_path):
        return None
    for fname in os.listdir(folder_path):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in {'.cbz', '.cbr', '.zip', '.rar', '.pdf'}:
            continue
        parsed = parse_issue_number(fname, series_title)
        if parsed is not None and parsed == number:
            return os.path.join(folder_path, fname)
    return None


def normalize_url(url: str) -> str:
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def norm(s: str) -> str:
    return re.sub(r'[^a-z0-9 ]', '', s.lower())
