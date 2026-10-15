import re
import requests
import time
import logging
from datetime import date
import cloudscraper
from bs4 import BeautifulSoup, Tag

from kometa.naming import PIPELINE_EXTS

logger = logging.getLogger(__name__)

BASE = "https://getcomics.org"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

GC_DIRECT_TERMS = (
    "download now", "main download", "main server", "main link",
    "mirror download", "mirror server", "mirror link", "link 1", "link 2", "getcomics",
)

# Mirror buttons a post lists beside its main host. Only the ones with a plain
# file API make the list — Mega/Mediafire/Terabox want a browser.
_PIXELDRAIN_RE = re.compile(r'https?://pixeldrain\.com/u/([A-Za-z0-9]+)')
_MEDIAFIRE_RE = re.compile(r'https?://(?:www\.)?mediafire\.com/file(?:_premium)?/')
_WETRANSFER_HOP_RE = re.compile(r'https?://(?:www\.)?(?:wetransfer\.com|we\.tl)/')

# A group's buttons, best first. Pixeldrain is a straight API; MediaFire a page
# with one link on it; WeTransfer wants its API asked politely. The main host is
# tried separately (it's the primary link). MEGA is missing on purpose: since
# 2026-10 its API answers every public-file request with -6, from any IP.
_MIRROR_KINDS = (("pixeldrain", ("pixeldrain", "pixel drain")), ("mediafire", ("mediafire",)),
                 ("wetransfer", ("wetransfer", "we transfer")))


def _mirror_kind(label: str) -> int | None:
    label = label.strip().lower()
    for i, (_, terms) in enumerate(_MIRROR_KINDS):
        if any(label == t or label.startswith(t + " ") for t in terms):
            return i
    return None


def _quality_rank(group_label: str) -> int:
    """A download group's scan quality, lower is better: true HD, then a group
    that doesn't say, then SD, then 'HD-Upscaled' (an SD scan blown up: bigger
    file, no more detail, sometimes smeared)."""
    t = group_label.upper()
    if "UPSCALE" in t:
        return 3
    if re.search(r"\bHD\b", t):
        return 0
    if re.search(r"\bSD\b", t):
        return 2
    return 1


def mediafire_direct(page_url: str) -> str | None:
    """MediaFire's file page carries a plain direct link on its download
    button — no account, no API. (2026-10-08: Deadly Duo #7 and the Hellboy
    Yule Cat both sat behind the fs2 wall with a MediaFire button right there.)
    Some pages hide it: in a window.location line, or base64'd in the button's
    data-scrambled-url (Kapowarr found both)."""
    import base64
    r = requests.get(page_url, headers={"User-Agent": HEADERS.get("User-Agent", "Mozilla/5.0")}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    a = soup.find("a", id="downloadButton") or soup.find("a", href=re.compile(r"^https?://download\d*\.mediafire\.com/"))
    href = a.get("href") if a else None
    if href and href.startswith("http"):
        return href
    m = re.search(r'https?://download\d*\.mediafire\.com/[^"\'\s]+', r.text)
    if m:
        return m.group(0)
    scrambled = a.get("data-scrambled-url") if a else None
    if scrambled:
        try:
            out = base64.b64decode(scrambled).decode("utf-8")
            if out.startswith("http"):
                return out
        except Exception:
            pass
    return None

_ISSUE_NUM_RE   = re.compile(r'#(\d+(?:\.\d+)?)')
# the end of a range can be a point-one: 'Fear Itself #1 – 7.3' ends at #7.3, not #7
# (reading it as #1–7 skipped the one post holding #7.1–7.3 as 'wrong issue')
_ISSUE_RANGE_RE = re.compile(r'#?\s*(\d+(?:\.\d+)?)\s*[-–—]\s*#?\s*(\d+(?:\.\d+)?)')
_ISSUE_WORD_RE  = re.compile(r'\bissues?\s+(\d+(?:\.\d+)?)\b', re.IGNORECASE)

# Words that pad a post title but don't indicate a different series
_TITLE_NOISE = frozenset({'the', 'a', 'an'})
# Words that indicate a collected/format edition — should NOT match a single-issue search
_FORMAT_WORDS = frozenset({'vol', 'volume', 'tpb', 'hc', 'omnibus', 'compendium',
                            'complete', 'collected', 'collection', 'fan', 'made'})


def _normalize(text: str) -> str:
    text = text.lower()
    # '&' is a word, not punctuation: our folder says 'Batman & the Joker', the
    # post says 'Batman and The Joker', and stripping the ampersand left us
    # comparing 'batman the joker' to 'batman and the joker' — seven issues
    # sitting on GetComics, 'not found' every time.
    text = re.sub(r'\s*&\s*', ' and ', text)
    text = re.sub(r'[–—‒\-]+', ' ', text)   # all dash variants → space
    text = re.sub(r'[^\w\s]', ' ', text)     # strip remaining punctuation
    return re.sub(r'\s+', ' ', text).strip() # collapse whitespace


def _post_covers_issue(text: str, issue_number: float) -> bool | None:
    """
    True  = post explicitly covers issue_number (exact or within range).
    False = post explicitly covers a different issue.
    None  = no issue number info found — caller decides.
    """
    # Ranges: "#130-136", "Issues 130–136", "#130 – 136"
    # Collect all plausible ranges; True if any covers us, False if all explicitly don't.
    ranges = [
        (float(m.group(1)), float(m.group(2)))
        for m in _ISSUE_RANGE_RE.finditer(text)
        if float(m.group(2)) > float(m.group(1)) and float(m.group(1)) < 1000
    ]
    if ranges:
        return any(lo <= issue_number <= hi for lo, hi in ranges)

    # Single-issue numbers from all formats: "#135", "Issue 135"
    nums = {float(n) for n in _ISSUE_NUM_RE.findall(text)}
    nums |= {float(m.group(1)) for m in _ISSUE_WORD_RE.finditer(text)}
    if nums:
        return issue_number in nums

    return None


# A bare number in a post title is usually packaging — the issue number, the
# publication year — and gets stripped before we look for words that would make
# this a DIFFERENT book. But Marvel names books after far-future years, and
# 'Secret Wars 2099' is not Secret Wars. Stripping every digit deleted the one
# token that told them apart, so the spinoff matched and got downloaded. Only
# strip what is actually incidental: issue numbers, and years a comic could
# plausibly have been published in. 2099 is neither — it's the title.
_PLAUSIBLE_YEAR_MIN = 1930


def _strip_incidental_numbers(text: str) -> str:
    max_year = date.today().year + 2   # solicitations run ahead of the calendar

    def _repl(m: re.Match) -> str:
        n = int(m.group(0))
        if n < _PLAUSIBLE_YEAR_MIN:                     # issue number — legacy counts run past 1000
            return ''                                   # (Amazing Spider-Man #1000, Detective #1100)
        if n <= max_year:                               # publication year
            return ''
        return m.group(0)                               # 2099, 3000 — part of the name

    return re.sub(r'\b\d+\b', _repl, text)


# 'Batman Day 2026: Batman of Two Worlds' is how LOCG files the one-shot.
# GetComics calls it 'Batman of Two Worlds #1 (2026)' — the 'Batman Day 2026:'
# bit is the EVENT, stamped on the front like a wristband, and every query that
# dragged it along came back with nothing. The tell is the year: a prefix with a
# year in it is branding ('Free Comic Book Day 2025:'). A prefix without one is
# part of the actual name ('Star Wars: Legacy of Vader') and stays glued on.
_EVENT_PREFIX_RE = re.compile(r'^[^:]*\b(?:19|20)\d{2}\b[^:]*:\s*(.+)$')


def _event_subtitle(title: str) -> str | None:
    """The title minus a year-stamped event prefix, or None if it has none."""
    m = _EVENT_PREFIX_RE.match(title or "")
    return m.group(1).strip() if m else None


# 'The Infinity Gauntlet' is what the tracker calls it; GetComics files it as
# 'Infinity Gauntlet #1 – 6 (1991-1992)'. A substring test for 'the infinity
# gauntlet' in 'infinity gauntlet 1 6 1991 1992' fails on the article alone, and
# the one post holding the missing #1 came back 'no result'. A leading 'The' is
# optional on BOTH sides: drop it from our name and the post's copy still
# contains the rest; the post's own 'the' is _TITLE_NOISE downstream.
def _core_title(title_norm: str) -> str:
    """Normalised title minus a leading article ('the infinity gauntlet' -> 'infinity gauntlet')."""
    core = re.sub(r'^the\s+', '', title_norm)
    return core or title_norm


# 'Where Monsters Dwell Vol 2 #2 (2015)' is a single issue — GetComics tags the
# volume when a name has run more than once. 'vol' reads as a collected edition
# to _series_matches, so all three missing issues sat there 'not found'
# (2026-10-11). A volume tag right before an issue number is just the run's
# label: drop it — but only when the post's year fits our run, or Vol 1 #2 (1970)
# would pass for the 2015 #2.
_VOL_SINGLE_RE = re.compile(r'\s+vol(?:ume)?\.?\s*\d+(?=\s*#\s*\d)', re.I)


def _drop_volume_tag(post_raw: str, series_year=None) -> str:
    if not _VOL_SINGLE_RE.search(post_raw):
        return post_raw
    if series_year:
        years = [int(y) for y in re.findall(r'\((\d{4})', post_raw)]
        if not years or not any(abs(y - int(series_year)) <= 1 for y in years):
            return post_raw
    return _VOL_SINGLE_RE.sub('', post_raw, count=1)


def _series_matches(title_norm: str, post_norm: str) -> bool:
    """True if post_norm is plausibly about title_norm (not a spinoff or format edition)."""
    title_norm = _core_title(title_norm)
    if title_norm not in post_norm:
        return False
    # Strip incidental numbers, find extra words in the post beyond the series title
    stripped = _strip_incidental_numbers(post_norm)
    post_words = {w for w in stripped.split() if w}
    title_words = set(title_norm.split())
    extra = post_words - title_words - _TITLE_NOISE
    # Any format-edition word = different product, not a single-issue post
    if extra & _FORMAT_WORDS:
        return False
    # Any extra content word = spinoff ("Batman Eternal", "X-Men Gold") — reject
    return len(extra) == 0


# A complete-run pack is the right answer for a big backlog, and the single-issue
# matcher can never find one — _series_matches rejects 'vol' as a format word, by
# design. Following Hickman's New Avengers (2013) therefore fell through to 33
# separate single-issue searches, each free to grab whichever volume of the name
# it liked, while 'New Avengers Vol. 3 #1 - 33 + Extras (2013-2015)' sat on
# GetComics the whole time.
#
# Identifying WHICH volume a pack is can't lean on the volume ordinal: ComicVine
# numbers Hickman's Avengers vol 4, GetComics doesn't number it at all (it files
# the run under the creator), and the two disagree on start year besides. What
# both ends DO agree on is the shape of the run — it starts at #1, it has N
# issues, and it ran in a particular window. That's what we match on.
_PACK_ISSUE_RANGE_RE = re.compile(r'#\s*(\d+(?:\.\d+)?)\s*[-–—]\s*#?\s*(\d+(?:\.\d+)?)')
_POST_YEAR_RANGE_RE = re.compile(r'\((\d{4})(?:\s*[-–—]\s*(\d{4}))?\)')
# .NOW issues, point-ones and 'extras' make our issue count and the pack's differ
# by a couple either way. More than a few apart is a different run, not drift.
_PACK_COUNT_TOLERANCE = 3


def _pack_post_matches(title_norm: str, post_raw: str, last_issue: int,
                       series_year=None) -> bool:
    """True if this post is a complete-run pack for OUR volume of the title.

    last_issue is the HIGHEST issue number in the run, not how many rows the
    series has. Those differ whenever Marvel slots in a point-one: Avengers (2012)
    ends at #44 but carries 46 issues, because 34.1 and 34.2 exist. Measured
    against the count, the real 'Avengers Vol. 5 #1 - 41' pack looks five short
    and gets thrown out; measured against the last issue it's off by three, which
    is ordinary pack drift (the tail of a run often lags a collection)."""
    if _core_title(title_norm) not in _normalize(post_raw):
        return False
    # Read the range off the RAW title — _normalize eats the '#', and without it
    # a collected-edition range ('Vol. 1 - 3') is indistinguishable from issues.
    m = _PACK_ISSUE_RANGE_RE.search(post_raw)
    if not m:
        return False
    lo, hi = float(m.group(1)), float(m.group(2))
    # #0 is a legitimate first issue — Secret Wars (2015) opens on one, and
    # demanding lo == 1 rejected its complete collection outright.
    if lo not in (0, 1) or hi <= lo:
        return False
    if last_issue and abs(hi - last_issue) > _PACK_COUNT_TOLERANCE:
        return False          # a partial run, or a different run of the same name
    ym = _POST_YEAR_RANGE_RE.search(post_raw)
    if ym and series_year and abs(int(ym.group(1)) - int(series_year)) > 1:
        return False
    return True


def _issue_pack_post_covers(title_norm: str, post_raw: str, issue_number: float,
                            series_year=None) -> bool:
    """True if this is a multi-issue post for OUR series whose range covers the
    issue we want. _series_matches throws these out on sight — 'Curse Words #1 – 25
    + TPBs' has 'tpbs' in it, which reads as a spinoff — and that's correct for the
    first two tiers. But when GetComics never posted #2 on its own, this is the only
    place #2 lives, and refusing to look at it left six issues dead as 'not found'
    while the post that had them sat on page one of the results.

    The spinoff guard still runs, on what's left after the packaging is peeled off:
    the range, the '+ TPBs' tail, the parentheticals, the volume ordinal. 'Curse
    Words Spring Has Sprung Special #1 – 2' keeps 'spring has sprung special' and
    stays rejected."""
    m = _PACK_ISSUE_RANGE_RE.search(post_raw)
    if not m:
        return False
    lo, hi = float(m.group(1)), float(m.group(2))
    if hi <= lo or not lo <= issue_number <= hi:
        return False
    ym = _POST_YEAR_RANGE_RE.search(post_raw)
    if ym and series_year and abs(int(ym.group(1)) - int(series_year)) > 1:
        return False           # same name, different run
    core = post_raw[:m.start()] + " " + post_raw[m.end():]
    core = re.sub(r'\+[^()]*', ' ', core)                          # '+ TPBs', '+ Extras'
    core = re.sub(r'\([^)]*\)', ' ', core)                         # (2017-2019), (Digital)
    core = re.sub(r'\bvol(?:ume)?\.?\s*\d+\b', ' ', core, flags=re.IGNORECASE)
    return _series_matches(title_norm, _normalize(core))


# A big pack post doesn't hand you one file — it hands you a shelf. 'Curse Words
# #1 – 25 + TPBs' is four separate downloads, one per line: '#1 – 5 (2017) (479 MB)
# : Main Server | Mega | ...'. The old extractor grabbed the first Main Server on
# the page, which is the #1-5 chunk no matter what you asked for. Ask for #7 and
# you'd get half a gig of the wrong five comics.
_CHUNK_RANGE_RE = re.compile(r'#\s*(\d+)\s*[-–—]\s*#?\s*(\d+)')


def _chunk_links(body) -> list[tuple[int, int, str]]:
    """(lo, hi, href) for every download line on a post that labels its own issue
    range. Empty for an ordinary single-download post."""
    chunks = []
    for el in body.find_all(["li", "p"]):
        a = next((a for a in el.find_all("a", href=True)
                  if any(t in a.get_text(strip=True).lower() for t in GC_DIRECT_TERMS)), None)
        if not a:
            continue
        # The label is the text BEFORE the first link — the post's own title line
        # also says '#1 – 25', and it isn't a download.
        label = ""
        for node in el.children:
            if isinstance(node, Tag) and (node.name == "a" or node.find("a")):
                break
            label += node.get_text(" ") if isinstance(node, Tag) else str(node)
        m = _CHUNK_RANGE_RE.search(label)
        if m and int(m.group(2)) > int(m.group(1)):
            chunks.append((int(m.group(1)), int(m.group(2)), a["href"]))
    return chunks


# What may follow the series name in a trade post's title: the edition, the
# volume, the year — not another word. 'DIE!namite Vol. 1 (TPB)' normalises to
# 'die namite vol 1 tpb' and the substring test took it for Die; what follows
# 'die' there is 'namite', so it isn't.
_AFTER_TITLE_RE = (r"(?=\s*(?:$|vol(?:ume)?\b|v\d|tpb\b|hc\b|hardcover\b|omnibus\b|deluxe\b|compendium\b|"
                   r"absolute\b|library\b|collection\b|collected\b|complete\b|the complete\b|book\b|part\b|"
                   r"edition\b|anniversary\b|gn\b|graphic novel\b|\d{4}\b|\(|#))")


def _title_leads(title_norm: str, post_norm: str) -> bool:
    """The series name, as whole words, followed by nothing but trade-speak."""
    return bool(re.search(rf"\b{re.escape(_core_title(title_norm))}\b" + _AFTER_TITLE_RE, post_norm))


_STOP = {"the", "a", "an", "and", "of", "vol", "volume", "tpb", "hc"}


def _trade_post_matches_loose(title_norm: str, post_norm: str, post_raw: str = "") -> bool:
    """For a name-only search whose result WAITS FOR CONFIRMATION: every word of
    the name present in the post, any order ('Hellboy Vol. 3 – The Chained Coffin
    and Others' for 'Hellboy: The Chained Coffin and Others'), plus a format word
    or a year, and not a single issue."""
    words = [w for w in title_norm.split() if w not in _STOP]
    if not words or not all(re.search(rf"\b{re.escape(w)}\b", post_norm) for w in words):
        return False
    if re.search(r'#\s*\d', post_raw):
        return False
    return bool(_FORMAT_WORDS & set(post_norm.split())) or bool(re.search(r'\b(19|20)\d{2}\b', post_norm))


def _trade_post_matches(title_norm: str, post_norm: str, vol=None, vol_range=None, post_raw: str = "") -> bool:
    """Trade-aware post matcher — the mirror image of _series_matches, which
    REJECTS format editions. Here we REQUIRE one: the post must name the series,
    look like a collected edition, and (when we know it) cover the volume we want.
    Note _normalize already turned every dash into a space, so a ranged trade
    reads as 'vol 1 6', not 'vol 1-6'."""
    if not _title_leads(title_norm, post_norm):
        return False
    has_format = bool(_FORMAT_WORDS & set(post_norm.split()))
    if vol is not None:
        if not has_format:
            return False
        # Bundled range "vol 1 6" — two small numbers right after vol (guard the
        # second against being a year so "vol 1 2015" isn't read as 1..2015).
        m = re.search(r'vol(?:ume)?\s*(\d+)\s+(\d+)\b', post_norm)
        if m and int(m.group(2)) < 100 and int(m.group(1)) <= vol <= int(m.group(2)):
            return True
        return bool(re.search(rf'vol(?:ume)?\s*{vol}\b', post_norm))
    if vol_range is not None:
        if not has_format:
            return False
        nums = {int(n) for n in re.findall(r'\b(\d+)\b', post_norm) if int(n) < 100}
        return vol_range[0] in nums or vol_range[1] in nums
    # No volume → OGN / one-shot. GetComics posts these under the BARE series title
    # ("Gigs (2026)") with no format word, so the collected-edition gate above would
    # wrongly reject them. A format word still counts as a trade; otherwise accept only
    # when the post isn't a single issue — no "#N" in the raw title, and no leftover
    # small number once the series name + 4-digit year are stripped. download_trade has
    # no content verification, so this no-single-issue guard is the only backstop. See
    # memory: getcomics-ogn-trade-match.
    if has_format:
        return True
    if re.search(r'#\s*\d', post_raw):
        return False
    leftover = re.sub(r'\b\d{4}\b', ' ', post_norm)
    leftover = re.sub(rf'\b{re.escape(_core_title(title_norm))}\b', ' ', leftover)
    return not re.search(r'\b\d{1,3}\b', leftover)


class GCRateLimitError(Exception):
    """GetComics said slow down — or we're still inside the cooldown from the
    last time it did. retry_after = seconds the caller should park the job."""
    def __init__(self, msg, retry_after=None, from_gate=False):
        super().__init__(msg)
        self.retry_after = retry_after
        self.from_gate = from_gate   # gate refusal = no HTTP was actually made


# Be a polite guest. Space every GetComics request, and after a real 429 shut
# the whole pipeline up for a cooldown instead of letting the next queue item
# walk face-first into the same wall. Module-level on purpose: one gate for
# every client instance in the process.
_MIN_REQUEST_GAP = 3.0      # seconds between any two GetComics requests
_RL_COOLDOWN = 15 * 60      # default cooldown after a real 429 (Retry-After wins)
_last_request = 0.0
_cooldown_until = 0.0


class GetComicsClient:
    def __init__(self):
        self.session = cloudscraper.create_scraper()
        self.session.headers.update(HEADERS)
        # primary download href -> mirror button hrefs from the same post group
        self._mirrors: dict[str, list[str]] = {}

    def mirror_urls(self, dl_url: str) -> list[str]:
        """Direct-file mirrors for a link this client handed out, resolved NOW.
        The main host is comicfiles.ru, and its fs2 box sits behind a Cloudflare
        wall that 403s every request style we own — while fs1 serves fine and the
        same post's Pixeldrain button hands over the exact file, no challenge
        (Last Ronin II #2-4, 2026-10-03). /dls/ hops are resolved at failure time,
        not search time: they go stale within hours."""
        out = []
        for href in self._mirrors.get(dl_url, []):
            try:
                loc = href
                if "getcomics.org/dls/" in href:
                    loc = self._get(href, allow_redirects=False).headers.get("location", "")
                m = _PIXELDRAIN_RE.match(loc)
                if m:
                    out.append(f"https://pixeldrain.com/api/file/{m.group(1)}?download")
                    continue
                if _MEDIAFIRE_RE.match(loc):
                    direct = self._mediafire_direct(loc)
                    if direct:
                        out.append(direct)
                    continue
                if _WETRANSFER_HOP_RE.match(loc) or "pixeldrain.com/l/" in loc:
                    out.append(loc)        # the downloader knows how to open these
            except GCRateLimitError:
                raise
            except Exception as e:
                logger.info(f"GetComics: mirror resolve failed for {href[:80]}: {e}")
        return out

    def _mediafire_direct(self, page_url: str) -> str | None:
        return mediafire_direct(page_url)

    def _remember_mirrors(self, primary: str, scope) -> None:
        """scope: a tag (searched with its descendants) or a list of sibling tags —
        a post's buttons are siblings, so 'same group' means the whole run.
        Every host in the group, best first (_MIRROR_KINDS). Returns the most
        buttons any one host had: more than one means several groups got mixed."""
        tags = scope if isinstance(scope, list) else [scope]
        links = []
        for t in tags:
            links += [a for a in t.select("a[href]")]
            if t.name == "a" and t.get("href"):
                links.append(t)
        picked = [(k, a["href"]) for a in links
                  if (k := _mirror_kind(a.get_text(strip=True))) is not None]
        self._mirrors[primary] = [h for _, h in sorted(picked, key=lambda x: x[0])]
        per_kind = [sum(1 for k, _ in picked if k == i) for i in range(len(_MIRROR_KINDS))]
        return max(per_kind, default=0)

    def _prefer_pixeldrain(self, href: str) -> str:
        """A post with a Pixeldrain button: take THAT first (a direct API, no page
        to scrape, more reliable than the main host — 2026-10-15). The main link
        becomes this pick's mirror, so a dead Pixeldrain still falls back to it."""
        for m in self.mirror_urls(href):
            if "pixeldrain.com/api/file/" in m:
                self._mirrors[m] = [href] + [x for x in self._mirrors.get(href, []) if x != m]
                logger.info("GetComics: Pixeldrain first, main host as the mirror")
                return m
        return href

    def _get(self, url, **kw):
        """Every GetComics request goes through here: cooldown gate first,
        polite spacing second, and a real 429 arms the gate for everyone."""
        global _last_request, _cooldown_until
        remaining = _cooldown_until - time.time()
        if remaining > 0:
            raise GCRateLimitError("GetComics cooling down after rate limit",
                                   retry_after=int(remaining), from_gate=True)
        wait = _last_request + _MIN_REQUEST_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        r = self.session.get(url, timeout=15, **kw)
        if r.status_code == 429:
            try:
                cooldown = max(int(r.headers.get("Retry-After") or _RL_COOLDOWN), 60)
            except ValueError:
                cooldown = _RL_COOLDOWN
            _cooldown_until = time.time() + cooldown
            logger.warning(f"GetComics 429 — pipeline cooling down for {cooldown}s")
            raise GCRateLimitError("Rate limited by GetComics", retry_after=cooldown)
        return r

    def search(self, title: str, issue_number: float, store_date: str | None = None, series_year: int | None = None, status_fn=None,
               exclude_urls=None) -> tuple[str | None, str | None]:
        """
        Returns (download_url, hint_filename) or (None, None).
        Tries progressively looser queries until a match is found.
        exclude_urls: download links already proven worthless for this row
        (dead delivery, lying pack) — a post resolving to one is skipped, so a
        retry buys a DIFFERENT release instead of the same corpse on a loop.
        """
        exclude_urls = exclude_urls or set()
        num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
        num_str = str(num_int)  # no zero-padding — GC titles use "#25" not "#025"
        year = store_date[:4] if store_date else None
        # Strip trailing (YYYY) from title — GC posts never include the year in the series name
        title = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()
        # Use series start year as fallback anchor so "Batman #1" doesn't match the wrong era
        anchor_year = year or (str(series_year) if series_year else None)

        # (query, the name a post must match) — the full title first, then the
        # event-stripped subtitle as a fallback when there is one.
        queries = []
        for name in filter(None, [title, _event_subtitle(title)]):
            if anchor_year:
                queries.append((f"{name} #{num_str} ({anchor_year})", name))
            queries += [(f"{name} #{num_str}", name), (f"{name} {num_str}", name), (name, name)]

        packs: list[str] = []   # multi-issue posts that cover us — last resort
        for query, name in queries:
            logger.info(f"GetComics search: {query!r}")
            if status_fn:
                status_fn(f"GetComics: “{query}”")
            post_url = self._search_page(query, name, issue_number,
                                         series_year=series_year, packs=packs)
            if post_url:
                # The singles tiers take a range post too ('Infinity Gauntlet #1 – 6'
                # reads as covering #1). Hand over the issue so a CHUNKED range post
                # gives up the chunk with our issue in it, not whatever's listed first.
                url, fname = self._extract_download(post_url, issue_number=issue_number)
                if url and url in exclude_urls:
                    logger.info(f"GetComics: {post_url} resolves to an excluded download — skipping")
                    continue
                if url:
                    return url, fname

        # Nobody posted this issue by itself. A pack that covers it is still a
        # delivery — the downloader pulls the one issue out and shelves the rest.
        for post_url in packs:
            url, fname = self._extract_download(post_url, issue_number=issue_number)
            if url and url in exclude_urls:
                logger.info(f"GetComics: pack {post_url} resolves to an excluded download — skipping")
                continue
            if url:
                logger.info(f"GetComics: #{num_str} via pack post {post_url}")
                return url, fname

        return None, None

    def search_series_pack(self, title: str, last_issue: int, series_year=None,
                           status_fn=None, exclude_urls=None) -> tuple[str | None, str | None]:
        """Find the complete-run pack for this volume — the one grab that settles a
        whole backlog. Returns (download_url, filename) or (None, None). Deliberately
        NOT reachable from the single-issue path, which rejects packs on purpose;
        this is the route a big backlog should take before it resorts to N separate
        single-issue searches that can each land a different volume of the name."""
        exclude_urls = exclude_urls or set()
        title = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()
        title_norm = _normalize(title)
        # '+ Extras' is how these packs are actually titled, and it outperforms
        # everything else tried: it surfaced the New Avengers pack at result 2 and
        # the Secret Wars one at result 1, where '#1 - N' and 'complete' returned
        # nothing at all.
        queries = [f"{title} + Extras", f"{title} #1 - {last_issue}",
                   f"{title} complete", title]
        seen = set()
        for query in queries:
            if query in seen:
                continue
            seen.add(query)
            logger.info(f"GetComics pack search: {query!r}")
            if status_fn:
                status_fn(f"GetComics pack: \u201c{query}\u201d")
            post_url = self._search_pack_page(query, title_norm, last_issue, series_year)
            if post_url:
                # A chunked post can't settle the run in one grab — queued as a
                # pack it'd land the first chunk and call the backlog handled.
                # Refuse it here; the single-issue path takes it chunk by chunk.
                url, fname = self._extract_download(post_url, allow_chunked=False)
                if url and url in exclude_urls:
                    logger.info(f"GetComics pack: {post_url} is an excluded download — skipping")
                    continue
                if url:
                    return url, fname
        return None, None

    def _search_articles(self, query: str, what: str, page: int = 1) -> tuple[list | None, bool]:
        """One page of GetComics search results: (articles, more_pages). News and
        Sponsored posts are dropped: they name series without carrying them.
        None on a failed request (rate limits still raise)."""
        url = BASE if page == 1 else f"{BASE.rstrip('/')}/page/{page}/"
        try:
            r = self._get(url, params={"s": query})
            r.raise_for_status()
        except GCRateLimitError:
            raise
        except Exception as e:
            logger.warning(f"GetComics {what} request failed: {e}")
            return None, False
        soup = BeautifulSoup(r.text, "lxml")
        out = []
        for article in soup.find_all("article", {"class": "post"}):
            cat = article.find("a", {"class": "post-category"})
            if cat and cat.get_text(strip=True) in ("News", "Sponsored"):
                continue
            out.append(article)
        nums = soup.find_all(["a", "span"], {"class": "page-numbers"})
        more = bool(nums) and not (nums[-1].name == "span" and "current" in (nums[-1].get("class") or []))
        return out, more

    def _search_pack_page(self, query: str, title_norm: str, last_issue: int,
                          series_year) -> str | None:
        articles, _ = self._search_articles(query, "pack search")
        for article in articles or []:
            h1 = article.find("h1", {"class": "post-title"})
            a = h1.find("a") if h1 else None
            if not a:
                continue
            text = a.get_text(strip=True)
            if _pack_post_matches(title_norm, text, last_issue, series_year):
                logger.info(f"GetComics pack: matched {text!r}")
                return a.get("href", "")
        return None

    def search_trade(self, title: str, vol=None, vol_range=None, status_fn=None,
                     exclude_urls=None, loose: bool = False) -> tuple[str | None, str | None]:
        """Find a collected edition on GetComics. Returns (download_url, filename)
        or (None, None). 'TPB' is the magic word — 'Volume' returns nothing — and a
        single 'Vol 1 – 6' post can be the jackpot covering many volumes at once.
        exclude_urls: downloads already proven worthless for this row — above all
        the lying range pack ('Vol 1-10' shipping five files) that a retry would
        otherwise re-download in full, verify, and reject, forever."""
        exclude_urls = exclude_urls or set()
        title = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()
        queries = []
        if vol is not None:
            queries += [f"{title} TPB Vol {vol}", f"{title} Vol {vol}"]
        if vol_range is not None:
            queries.append(f"{title} Vol {vol_range[0]}-{vol_range[1]}")
        queries += [f"{title} TPB", title]
        if loose:
            # 'Hellboy: The Chained Coffin and Others' is posted as 'Hellboy Vol. 3 –
            # The Chained Coffin…': the subtitle alone finds it; the matcher still
            # wants every word of the full name in the post.
            sub = re.split(r"\s*[:\u2013\u2014-]\s+", title, maxsplit=1)
            if len(sub) == 2 and len(sub[1]) >= 6:
                queries.append(sub[1])

        title_norm = _normalize(title)
        seen = set()
        for query in queries:
            if query in seen:
                continue
            seen.add(query)
            logger.info(f"GetComics trade search: {query!r}")
            if status_fn:
                status_fn(f"GetComics: “{query}”")
            post_url = self._search_trade_page(query, title_norm, vol, vol_range, loose=loose)
            if post_url:
                url, fname = self._extract_download(post_url)
                if url and url in exclude_urls:
                    logger.info(f"GetComics trade: {post_url} resolves to an excluded download — skipping")
                    continue
                if url:
                    return url, fname
        return None, None

    def _search_trade_page(self, query: str, title_norm: str, vol, vol_range, loose: bool = False) -> str | None:
        articles, _ = self._search_articles(query, "trade search")
        for article in articles or []:
            h1 = article.find("h1", {"class": "post-title"})
            a = h1.find("a") if h1 else None
            if not a:
                continue
            text = a.get_text(strip=True)
            ok = (_trade_post_matches_loose(title_norm, _normalize(text), post_raw=text) if loose
                  else _trade_post_matches(title_norm, _normalize(text), vol, vol_range, post_raw=text))
            if ok:
                logger.info(f"GetComics: matched trade post {text!r}{' (loose)' if loose else ''}")
                return a.get("href", "")
        return None

    def _search_page(self, query: str, title: str, issue_number: float,
                     series_year=None, packs: list | None = None) -> str | None:
        articles, more = self._search_articles(query, "search")
        if articles is None:
            return None
        if not articles:
            logger.info(f"GetComics: no articles on search page for {query!r}")
            return None

        title_norm = _normalize(title)

        def _explicit(arts):
            for article in arts:
                h1 = article.find("h1", {"class": "post-title"})
                a = h1.find("a") if h1 else None
                if a:
                    text = _drop_volume_tag(a.get_text(strip=True), series_year)
                    if _series_matches(title_norm, _normalize(text)) and _post_covers_issue(text, issue_number) is True:
                        return True
            return False

        # An older issue of a long run can sit on page 2 behind a wall of newer
        # posts. Only asked for when page 1 has no post naming this issue.
        if more and not _explicit(articles):
            page2, _ = self._search_articles(query, "search", page=2)
            articles = articles + (page2 or [])

        # Park any pack post that covers us for the caller's last-resort pass —
        # only after every query's single-issue tiers have come up dry.
        if packs is not None:
            for article in articles:
                h1 = article.find("h1", {"class": "post-title"})
                a = h1.find("a") if h1 else None
                if not a:
                    continue
                href = a.get("href", "")
                if href not in packs and _issue_pack_post_covers(
                        title_norm, a.get_text(strip=True), issue_number, series_year):
                    packs.append(href)

        # Best match: series name matches AND post explicitly covers our issue number
        for article in articles:
            h1 = article.find("h1", {"class": "post-title"})
            if not h1:
                continue
            a = h1.find("a")
            if not a:
                continue
            text = _drop_volume_tag(a.get_text(strip=True), series_year)
            href = a.get("href", "")
            if _series_matches(title_norm, _normalize(text)):
                if _post_covers_issue(text, issue_number) is True:
                    logger.info(f"GetComics: matched post {text!r}")
                    return href

        # Fallback: series name matches but no explicit issue number in post title.
        # Skip posts that explicitly cover a different issue or a format edition.
        for article in articles:
            h1 = article.find("h1", {"class": "post-title"})
            if not h1:
                continue
            a = h1.find("a")
            if not a:
                continue
            text = _drop_volume_tag(a.get_text(strip=True), series_year)
            href = a.get("href", "")
            if _series_matches(title_norm, _normalize(text)):
                coverage = _post_covers_issue(text, issue_number)
                if coverage is False:
                    logger.info(f"GetComics: fallback skipped {text!r} (wrong issue)")
                    continue
                logger.info(f"GetComics: fallback post {text!r}")
                return href

        return None

    def _extract_download(self, post_url: str, issue_number: float | None = None,
                          allow_chunked: bool = True) -> tuple[str | None, str | None]:
        """issue_number: when the post is split into ranged chunks, take the chunk
        that covers it — and NOTHING if none does. allow_chunked=False refuses a
        chunked post outright (the whole-run pack path wants every issue, and one
        chunk is not that)."""
        try:
            r = self._get(post_url)
            r.raise_for_status()
        except GCRateLimitError:
            raise
        except Exception as e:
            logger.warning(f"GetComics post fetch failed: {e}")
            return None, None

        soup = BeautifulSoup(r.text, "lxml")
        body = soup.find("section", {"class": "post-contents"})
        if not body:
            body = soup

        chunks = _chunk_links(body)
        if len(chunks) > 1:
            if not allow_chunked:
                logger.info(f"GetComics: {post_url} is split into {len(chunks)} chunks — "
                            f"not a single complete-run download")
                return None, None
            if issue_number is not None:
                hit = next((c for c in chunks if c[0] <= issue_number <= c[1]), None)
                if not hit:
                    logger.info(f"GetComics: no chunk on {post_url} covers #{issue_number:g}")
                    return None, None
                logger.info(f"GetComics: chunk #{hit[0]}-{hit[1]} for #{issue_number:g} {hit[2][:80]}")
                return hit[2], None

        # Strategy 1: find download groups — <p> containing "Language" marks a group.
        # HD first: posts carry an HD and an SD group side by side, and we took
        # whichever came first. Stable sort, so a post with no labels keeps its order.
        groups = [p for p in body.find_all("p") if "Language" in p.get_text()]
        groups.sort(key=lambda p: _quality_rank(p.get_text(" ", strip=True)))
        for p in groups:
            for sibling in p.next_siblings:
                if not isinstance(sibling, Tag):
                    continue
                if sibling.name == "hr":
                    break
                # The button div is often the sibling ITSELF, not a child of it.
                # find_all only digs downward, so it walked straight past every
                # button on the page, fell to strategy 2, saw two groups, binned
                # every mirror, and slammed into the fs2 wall. Deadpool #8-#10,
                # 2026-10-15. Look at the thing in front of you first.
                wraps = ([sibling] if sibling.name == "div" and "aio-button-center" in (sibling.get("class") or [])
                         else sibling.find_all("div", {"class": "aio-button-center"}))
                for btn_wrap in wraps:
                    a = btn_wrap.find("a", href=True)
                    if not a:
                        continue
                    href = a["href"]
                    text = a.get_text(strip=True).lower()
                    if any(t in text for t in GC_DIRECT_TERMS):
                        logger.info(f"GetComics: direct download {href[:80]}")
                        group = []
                        for g in p.next_siblings:
                            if isinstance(g, Tag):
                                if g.name == "hr":
                                    break
                                group.append(g)
                        self._remember_mirrors(href, group)
                        return self._prefer_pixeldrain(href), None

        # Strategy 2: any aio-button-center on the page with direct-download text
        for btn_wrap in body.find_all("div", {"class": "aio-button-center"}):
            a = btn_wrap.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            text = a.get_text(strip=True).lower()
            if any(t in text for t in GC_DIRECT_TERMS):
                logger.info(f"GetComics: strategy-2 direct download {href[:80]}")
                # whole-page scan: TWO of the same kind means several download
                # groups and we can't tell whose is whose. One of each is a single
                # group with several mirrors — keep them.
                if self._remember_mirrors(href, body) > 1:
                    self._mirrors[href] = []
                return self._prefer_pixeldrain(href), None

        # Strategy 3: any link ending in a comic file extension
        for a in body.find_all("a", href=True):
            href = a["href"]
            if any(href.lower().endswith(ext) for ext in PIPELINE_EXTS):
                fname = href.rsplit("/", 1)[-1]
                logger.info(f"GetComics: direct file link {href[:80]}")
                return href, fname

        # Strategy 4: plain <a> links (no aio-button-center) whose text matches terms
        # and href is an on-site GC download URL — catches pack pages with bare link markup
        for a in body.find_all("a", href=True):
            href = a["href"]
            if "getcomics.org/dls/" not in href:
                continue
            text = a.get_text(strip=True).lower()
            if any(t in text for t in GC_DIRECT_TERMS):
                logger.info(f"GetComics: strategy-4 plain link {href[:80]}")
                return href, None

        logger.info(f"GetComics: no download link found on {post_url}")
        return None, None
