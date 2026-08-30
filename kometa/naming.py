"""Pure parsing helpers — identify comic issues from filenames and normalize
search/URL strings. Text in, value out (plus directory scans that read names
and, for ownership, cheaply confirm a file isn't a hollow shell). No DB, no
clients, no app state — trivially testable in isolation.
"""
import functools
import os
import re
import subprocess
import zipfile

# The extension zoo, consolidated. Eight modules each kept their own drifting
# copy of "what's a comic" — so a .cb7 counted toward an arc but was invisible
# to the series it belonged to. Two sets, two questions. Pick the one that
# answers YOURS, and never define a new one.
#
# Does this file on disk count as a comic you OWN? Broad — folder is truth no
# matter how the file got there (grabbed, ripped, hand-dropped in 2019).
OWNED_EXTS = frozenset({'.cbz', '.cbr', '.cb7', '.cbt', '.zip', '.rar', '.pdf'})
# Can the download pipeline open and repack it? Narrow — only what the extract/
# rebuild tooling actually speaks. Everything it ships lands as .cbz.
PIPELINE_EXTS = frozenset({'.cbz', '.cbr', '.zip', '.rar'})


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


_IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.avif')
# Formats we can't crack open cheaply. Ownership takes them at their word rather
# than pretending to know — see the fail-open note in _archive_has_pages.
_UNPROBEABLE_EXTS = frozenset({'.pdf', '.cb7', '.cbt'})
# Extensions that CLAIM to be a zip or a rar. For these, the magic bytes are a
# promise the file can be held to — anything else is not a comic, whatever the
# name says.
_ZIP_OR_RAR_EXTS = frozenset({'.cbz', '.cbr', '.zip', '.rar'})


def _archive_has_pages(path: str) -> bool:
    """Can this file yield a single page? Ownership's honesty check.

    A 76MB truncated CBR that lists ZERO entries was counting as an owned issue,
    because ownership only ever looked at the NAME. The file existed, so the
    issue was 'acquired', so nothing ever tried to fetch it again — a hole in the
    library that hides itself. Extension and size are both liars; entries aren't.

    FAILS OPEN, always. Missing lsar, an odd format, a permissions problem: we
    return True and let the file count. Marking a real comic unowned because our
    own tooling came up short would re-download books you already have, and
    that's a worse failure than the one we're fixing."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _UNPROBEABLE_EXTS:
        return True
    try:
        with open(path, 'rb') as fh:
            magic = fh.read(4)
    except OSError:
        return True
    if magic[:2] == b'PK':
        try:
            with zipfile.ZipFile(path) as zf:
                return any(n.lower().endswith(_IMAGE_EXTS) for n in zf.namelist())
        except Exception:
            return False        # a ZIP whose own directory won't parse is broken, full stop
    if magic == b'Rar!':
        try:
            out = subprocess.run(['lsar', path], capture_output=True, timeout=60)
        except Exception:
            return True         # no lsar here — not our place to condemn the file
        # Deliberately NOT checking returncode. lsar exits non-zero on any damaged
        # archive, which is a verdict about the FILE, not about whether lsar could
        # do its job — treating it as "couldn't tell" (the first cut of this did)
        # fails open on exactly the corruption we're hunting, and the zero-page
        # CBR sailed straight through. Trust the entries it managed to list: a
        # partial listing still means real pages, an empty one means nothing.
        names = out.stdout.decode('utf-8', 'replace').splitlines()[1:]
        return any(n.strip().lower().endswith(_IMAGE_EXTS) for n in names)
    # Magic matched neither. A .cbz/.cbr is a promise to be a zip or a rar, and
    # this file isn't keeping it — the 76MB #006 was NUL bytes end to end, a
    # download that reserved its space and never filled it. Extensions we can't
    # probe were returned above; anything still here is a broken claim.
    return ext not in _ZIP_OR_RAR_EXTS


@functools.lru_cache(maxsize=4096)
def _has_pages_cached(path: str, size: int, mtime: float) -> bool:
    # Keyed on identity-plus-fingerprint so a re-download of the same path is
    # re-probed, but a full library sync doesn't re-crack every archive it owns.
    return _archive_has_pages(path)


def is_readable_comic(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return _has_pages_cached(path, st.st_size, st.st_mtime)


def scan_folder_numbers(folder_path: str, series_title: str = "") -> set[float]:
    numbers = set()
    try:
        for name in os.listdir(folder_path):
            if os.path.splitext(name)[1].lower() in OWNED_EXTS:
                num = parse_issue_number(name, series_title)
                if num is not None and is_readable_comic(os.path.join(folder_path, name)):
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
    return {v for v, _ in scan_folder_volume_entries(folder_path)}


def scan_folder_volume_entries(folder_path: str) -> list[tuple[int, str]]:
    """(volume, lowercased filename) pairs for the collected editions on disk.
    The name rides along so callers can tell EDITIONS apart — an Absolute Vol 1
    and a plain Vol 1 TPB share a volume number but are different books."""
    entries = []
    try:
        for name in os.listdir(folder_path):
            if os.path.splitext(name)[1].lower() in OWNED_EXTS:
                v = parse_volume_number(name)
                if v is not None:
                    entries.append((v, name.lower()))
    except Exception:
        pass
    return entries


# Words that mark a DIFFERENT collected edition of the same volume number — an
# "Absolute Vol. 1" is not a "Vol. 1 TP", no matter what the vol digit says.
_EDITION_WORDS = ("absolute", "omnibus", "deluxe", "compendium")


def edition_keywords(name: str) -> frozenset:
    """The special-edition words present in a trade title / filename. Ownership and
    Komga matching require the trade's set to EQUAL the file's set: a plain TPB
    (empty set) never claims an Absolute file, and vice versa."""
    low = name.lower()
    return frozenset(w for w in _EDITION_WORDS if w in low)


def find_issue_file(folder_path: str, series_title: str, number: float) -> str | None:
    """Scan folder_path for a comic file matching issue number. Returns full path or None."""
    if not folder_path or not os.path.isdir(folder_path):
        return None
    for fname in os.listdir(folder_path):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in OWNED_EXTS:
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
