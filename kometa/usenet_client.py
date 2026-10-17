import re
import logging

from kometa.naming import norm_key as _norm

logger = logging.getLogger(__name__)

# Release scoring shared by the Prowlarr path (kometa/prowlarr_client.py). The
# per-indexer Newznab client that used to live here was retired with the move to
# Prowlarr; its search functions are gone (2026-10-11).

_TITLE_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def year_mismatch(title: str, series_year, store_date: str | None = None) -> bool:
    """A result whose title carries a year the series can't account for is a
    different release wearing the same name. Two doors, and the name needed both.

    DOWNWARD — the Keith Urban 'Ripcord' (2016) ALBUM grabbed for the Ripcord
    (2026) comic. Uses the EARLIEST year in the title (packs legitimately span
    ranges forward, never backward past the series' birth).

    UPWARD — the door that was standing wide open until 2026-09-11: following
    Hickman's New Avengers (2013) hoovered up the 2025 relaunch, because
    'New Avengers 009 (2026)' is not older than 2013 and nothing else looked.
    Marvel and DC recycle a title every few years; 'began in 2013' says nothing
    about how far forward a name stays in print. The issue's own store_date is
    the bound that was missing — #9 shipped in 2013, so a 2026 stamp on it is a
    lie no matter what the series year says.

    A title year is plausible if it is either the series' BIRTH year (scene packs
    stamp '(2013-)', not the issue's release year) or within a year of when this
    issue actually shipped. No store_date means no upper bound to enforce — back
    issues of a long run must still pass — so that case keeps the old behaviour.

    Only a known mismatch rejects: no year in the title, or nothing to anchor
    against, passes. This is a tripwire, not a gate."""
    years = [int(m.group(0)) for m in _TITLE_YEAR_RE.finditer(title or "")]
    if not years:
        return False

    # Downward door. Stays live even when a store_date gives us an upper bound —
    # a pack reaching back past the series' birth is still a different thing.
    if series_year and min(years) < int(series_year) - 1:
        return True

    try:
        release_year = int(str(store_date)[:4]) if store_date else None
    except ValueError:
        release_year = None
    if release_year is None:
        return False

    plausible = {release_year - 1, release_year, release_year + 1}
    if series_year:
        plausible.add(int(series_year))
    return not any(y in plausible for y in years)


# TV/movie/music/ebook release markers. A comic NZB never carries these, so any
# hit disqualifies — this is what finally shuts the 'Ripcord' trap: the Keith
# Urban ALBUM (flac/kbps), the Shazam & Andy Richter TV EPISODES (SxxExx/720p),
# and the Scott Pratt EPUB all die before they can score. digital-mobile is a
# comic, technically — a vertical 800px webtoon strip wearing a print issue's
# number (the Absolute Superman #21 Infinite Edition grab). Never what we want.
_MEDIA_NOISE_RE = re.compile(
    r'\b(?:'
    r's\d{1,2}e\d{1,2}'                                   # TV episode  S02E05
    r'|\d{3,4}p'                                          # video res   720p/1080p/2160p
    r'|blu-?ray|web-?dl|webrip|hdtv|dvd-?rip|bd-?rip|hd-?rip'
    r'|x26[45]|hevc|xvid|remux'                           # video codecs
    r'|flac|mp3|aac|ac3|dts|ddp?5|\d{3}kbps'             # audio codecs
    r'|epub|mobi|azw3?'                                   # ebooks
    r'|digital[ ._-]?mobile'                              # vertical webtoon rips
    r')\b', re.I)


# A release carrying Japanese, Korean or Chinese script is not the English-language
# Marvel/DC issue we asked for — live Prowlarr served a Touhou doujinshi,
# '(ネオ例大祭8) [羊箱] 幻想郷SECRET WARS 1 (東方Project)', as the best match for
# Secret Wars #1. It scored full marks because _norm strips non-ASCII: by the time
# anything looked, the title read '8 secret wars 1 project'. The evidence only
# exists on the RAW string, so it has to be read here, like the media-noise check.
# Latin diacritics are deliberately NOT included — an accent is not another alphabet.
_CJK_RE = re.compile(
    r'[\u3040-\u309f'      # hiragana
    r'\u30a0-\u30ff'       # katakana
    r'\u3400-\u4dbf'       # CJK unified ideographs extension A
    r'\u4e00-\u9fff'       # CJK unified ideographs
    r'\uac00-\ud7af]'      # hangul syllables
)


def _looks_non_comic(title: str) -> bool:
    return bool(_MEDIA_NOISE_RE.search(title or "") or _CJK_RE.search(title or ""))


def _issue_num_present(t: str, s: str, num_int, raw_title: str | None = None) -> bool:
    """True only when the issue number sits in a comic-issue position: right
    after the series name (norm strips '#', so '#4' reads as 'series 4'), or as
    a zero-padded 3-digit issue token ('004'/'000')."""
    if float(num_int) == int(num_int):
        n = int(num_int)
        return bool(
            re.search(rf'{re.escape(s)}\s+0*{n}\b', t)
            or re.search(rf'\b{n:03d}\b', t)
        )
    return bool(re.search(rf'{re.escape(s)}\s+{re.escape(str(num_int))}\b', t))


# Series names that are also ordinary words usenet is full of. 'Saga' is not
# on the list: it's rare enough as a release title that its own number suffices.
_GENERIC_NAMES = {"die", "red", "hulk", "thor", "zombie", "ronin", "revolver", "predator", "alien", "aliens", "war", "fear"}


def is_generic_name(series: str) -> bool:
    """A series name that is also an ordinary word, or too short to be anything
    else ('Die', 'Red', 'Hulk'): usenet's title search returns hundreds of
    unrelated posts for these, so the comic-named source goes first."""
    s = _norm(_fold(series or ""))
    return len(s) <= 3 or s in _GENERIC_NAMES


def _fold(s: str) -> str:
    """'Sécret' reads as 'Secret': diacritics are spelling, not a different name."""
    import unicodedata
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


_SERIES_YEAR_TAIL_RE = re.compile(r"\s*\((?:19|20)\d{2}(?:\s*-\s*(?:(?:19|20)\d{2})?)?\)\s*$")


def _series_name(series: str) -> str:
    """'Deadpool (2023)' -> 'Deadpool'. The year tag is how WE tell volumes apart;
    a release says 'Deadpool 008 (2023)', year AFTER the number. Leave the tag on
    and the name never leads, every release scores zero, and thirty series
    (both Batmans, Avengers 2018, Deadpool 2023) go blind on usenet AND torrents.
    The year is still enforced, by _drop_year_mismatches."""
    return _SERIES_YEAR_TAIL_RE.sub("", series or "").strip() or (series or "")


# Allowed tokens immediately after the series name in a release title.
# Extra content words ("Classic", "Deluxe", "Iron Man", "Beyond", "Eternal")
# mark a different series/spinoff and disqualify the release (0 points).
# Allowed: issue number (#1, 001, 10), year ((1998)), volume (Vol 2, v1),
# or standard packaging noise (audio, digital, cbr, cbz).
_ALLOWED_AFTER_SERIES_RE = re.compile(
    r'^(?:'
    r'#?\s*\d+'                                # issue number or year (001, 1998, 10, #1)
    r'|(?:vol(?:ume)?|v)\.?\s*\d*'             # volume marker (vol 2, v1, volume, vol)
    r'|audio|digital|cbr|cbz'                  # format/packaging noise
    r')\b',
    re.I
)

_PACK_AFTER_SERIES_RE = re.compile(
    r'^(?:'
    r'#?\s*\d+'                                # issue number, range, or year (001-030, 1998)
    r'|(?:vol(?:ume)?|v)\.?\s*\d*'             # volume marker (vol 2, v1)
    r'|complete|collection|pack|omnibus|complet' # pack keywords
    r'|audio|digital|cbr|cbz'                  # format/packaging noise
    r')\b',
    re.I
)


def _nzb_score(nzb_title: str, series: str, issue_number: float) -> int:
    """Score an NZB title for relevance. Higher is better; 0 disqualifies."""
    if _looks_non_comic(nzb_title):
        return 0
    series = _series_name(series)
    t = _norm(_fold(nzb_title))
    s = _norm(_fold(series))
    num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
    # A short, ordinary word is not a series name on its own. 'Die' bought a
    # 2GB German audiobook ('Die Henkerstochter…') and a 466MB mystery for #9:
    # the word was in the title, the number was somewhere in it, score 15.
    # Short names need comic evidence beside them AND the number in an issue
    # position — same rule the pack scorer learned from the Beethoven box set.
    if (len(s) <= 3 or s in _GENERIC_NAMES) and not (_COMIC_EVIDENCE_RE.search(nzb_title) and _issue_num_present(t, s, num_int, raw_title=nzb_title)):
        return 0
    if not re.search(rf"\b{re.escape(s)}\b", t):      # whole words: 'die' is inside 'soldiers'
        return 0                                       # no series name, no candidate — a bare '001' is not evidence
    # The series name LEADS a release ('Nights.012.2025.Digital'); a name found
    # mid-title belongs to a different comic — '1001 Arabian Nights - The
    # Adventures of Sinbad 012' is not Nights #12, whatever the number says.
    # Any extra word immediately after the series name indicates a different series
    # ('Buffy the Vampire Slayer Classic', 'The Walking Dead Deluxe', 'Invincible Iron Man').
    lead = _norm(_fold(re.sub(r"^(?:\[[^\]]*\]\s*|\(?(?:19|20)\d{2}\)?[\s.]+)+", "", nzb_title))).strip()
    if not (lead == s or (lead.startswith(s + " ") and _ALLOWED_AFTER_SERIES_RE.match(lead[len(s) + 1:].strip()))):
        return 0
    score = 10
    if _issue_num_present(t, s, num_int, raw_title=nzb_title):
        score += 5
    return score


_PACK_KEYWORDS_RE = re.compile(r'\b(complete|collection|pack|omnibus|complet)\b', re.I)
_RANGE_RE = re.compile(r'#\s*\d+\s*[-–]\s*#?\s*\d+')
PACK_THRESHOLD = 5


# A comic pack is tens of MB an issue. 'Ludwig van Beethoven - Complete Works
# Brilliant Classics 100 CD Box' (28.6 GB) scored 24 for the series '100%'
# (2026-10-08): '100' was in the title, 'Complete' was in the title, and size
# only ever ADDED points. Now: the name must be a whole word, a short or numeric
# name needs comic evidence beside it, and a pack too big for the run is out.
_PACK_MB_PER_ISSUE = 150
_PACK_DEFAULT_MAX = 15 * 1024 ** 3
_COMIC_EVIDENCE_RE = re.compile(r'\b(cbr|cbz|comics?|digital|tpb|graphic novel|vol(?:ume)?\.?\s*\d+)\b|#\s*\d', re.I)


def _pack_score(nzb_title: str, series: str, size: int, missing: int | None = None) -> int:
    if _looks_non_comic(nzb_title):
        return 0
    series = _series_name(series)
    s = _norm(series)
    if not s:
        return 0
    # Pack releases must also lead with the series name and not add spinoff words:
    # "Buffy the Vampire Slayer Classic 001-030" is not Buffy the Vampire Slayer.
    lead = _norm(_fold(re.sub(r"^(?:\[[^\]]*\]\s*|\(?(?:19|20)\d{2}\)?[\s.]+)+", "", nzb_title))).strip()
    if not (lead == s or (lead.startswith(s + " ") and _PACK_AFTER_SERIES_RE.match(lead[len(s) + 1:].strip()))):
        return 0
    if (len(s) < 4 or s.replace(" ", "").isdigit()) and not (_RANGE_RE.search(nzb_title) or _COMIC_EVIDENCE_RE.search(nzb_title)):
        return 0        # '100', 'X', 'Y' — the name alone proves nothing
    max_size = (missing * _PACK_MB_PER_ISSUE * 1024 ** 2 + 1024 ** 3) if missing else _PACK_DEFAULT_MAX
    if size and size > max_size:
        return 0
    score = 10
    if _PACK_KEYWORDS_RE.search(nzb_title):
        score += 8
    if _RANGE_RE.search(nzb_title):
        score += 6
    if size > 50_000_000:
        score += 3
    if size > 200_000_000:
        score += 3
    return score
