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


# "Vol 1", "Vol. 01", "Volume 1", "v01" — but NOT a bare issue number, so a trade
# on disk reads as a volume and a single doesn't masquerade as one.
_VOL_FILE_RE = re.compile(r'\b(?:vol(?:ume)?\.?\s*|v)(\d+)', re.IGNORECASE)


def parse_volume_number(name: str) -> int | None:
    """Volume number of a COLLECTED EDITION from a filename or Komga book name, or
    None. The trade analogue of parse_issue_number, with a critical guard: a name
    carrying an issue number (#001) is a single issue that merely labels its arc
    ('Saga - Vol. 1 #001') — NOT a trade. Don't splitext: it mis-splits on the dot
    in 'Vol.' (and these names may have no extension anyway)."""
    if re.search(r'#\s*\d', name):
        return None
    m = _VOL_FILE_RE.search(name)
    return int(m.group(1)) if m else None


def scan_folder_volumes(folder_path: str) -> set[int]:
    """Volume numbers of collected editions present on disk — same folder-is-truth
    model as scan_folder_numbers, just for trades. A trade counts as owned when its
    volume turns up here."""
    exts = {'.cbz', '.cbr', '.zip', '.rar', '.pdf'}
    vols = set()
    try:
        for name in os.listdir(folder_path):
            if os.path.splitext(name)[1].lower() in exts:
                v = parse_volume_number(name)
                if v is not None:
                    vols.add(v)
    except Exception:
        pass
    return vols


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


def norm_key(s: str) -> str:
    """THE punctuation/spacing-insensitive comparison key — collapse RUNS of
    non-alphanumerics to a single space. The `+` matters: without it ": " becomes
    2 spaces and " - " becomes 3, so 'Batman: Gargoyle … - Noir Edition' and a
    release named 'Batman - Gargoyle … Noir Edition' normalise to DIFFERENT
    spacing and substring matches silently fail. One definition, four consumers
    (arc titles, edition/book names, NZB scoring, Wikipedia arc tables) — this
    key deciding 'same name?' identically everywhere is a feature, not tidiness."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


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

    This is how the folder path gets derived from publisher+title alone —
    no Komga needed. Existing series resolve to their real on-disk folder
    (variation-tolerant), new ones to a fresh canonical path.
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
