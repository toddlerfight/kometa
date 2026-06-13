import re
import time
import logging
import cloudscraper
from bs4 import BeautifulSoup, Tag

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

_ISSUE_NUM_RE   = re.compile(r'#(\d+(?:\.\d+)?)')
_ISSUE_RANGE_RE = re.compile(r'#?\s*(\d+)\s*[-–—]\s*#?\s*(\d+)')
_ISSUE_WORD_RE  = re.compile(r'\bissues?\s+(\d+(?:\.\d+)?)\b', re.IGNORECASE)

# Words that pad a post title but don't indicate a different series
_TITLE_NOISE = frozenset({'the', 'a', 'an'})
# Words that indicate a collected/format edition — should NOT match a single-issue search
_FORMAT_WORDS = frozenset({'vol', 'volume', 'tpb', 'hc', 'omnibus', 'compendium',
                            'complete', 'collected', 'collection', 'fan', 'made'})


def _normalize(text: str) -> str:
    text = text.lower()
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


def _series_matches(title_norm: str, post_norm: str) -> bool:
    """True if post_norm is plausibly about title_norm (not a spinoff or format edition)."""
    if title_norm not in post_norm:
        return False
    # Strip numbers and years, find extra words in the post beyond the series title
    stripped = re.sub(r'\b\d+\b', '', post_norm)
    post_words = {w for w in stripped.split() if w}
    title_words = set(title_norm.split())
    extra = post_words - title_words - _TITLE_NOISE
    # Any format-edition word = different product, not a single-issue post
    if extra & _FORMAT_WORDS:
        return False
    # Any extra content word = spinoff ("Batman Eternal", "X-Men Gold") — reject
    return len(extra) == 0


def _trade_post_matches(title_norm: str, post_norm: str, vol=None, vol_range=None) -> bool:
    """Trade-aware post matcher — the mirror image of _series_matches, which
    REJECTS format editions. Here we REQUIRE one: the post must name the series,
    look like a collected edition, and (when we know it) cover the volume we want.
    Note _normalize already turned every dash into a space, so a ranged trade
    reads as 'vol 1 6', not 'vol 1-6'."""
    if title_norm not in post_norm:
        return False
    if not (_FORMAT_WORDS & set(post_norm.split())):
        return False
    if vol is not None:
        # Bundled range "vol 1 6" — two small numbers right after vol (guard the
        # second against being a year so "vol 1 2015" isn't read as 1..2015).
        m = re.search(r'vol(?:ume)?\s*(\d+)\s+(\d+)\b', post_norm)
        if m and int(m.group(2)) < 100 and int(m.group(1)) <= vol <= int(m.group(2)):
            return True
        return bool(re.search(rf'vol(?:ume)?\s*{vol}\b', post_norm))
    if vol_range is not None:
        nums = {int(n) for n in re.findall(r'\b(\d+)\b', post_norm) if int(n) < 100}
        return vol_range[0] in nums or vol_range[1] in nums
    return True


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

    def search(self, title: str, issue_number: float, store_date: str | None = None, series_year: int | None = None, status_fn=None) -> tuple[str | None, str | None]:
        """
        Returns (download_url, hint_filename) or (None, None).
        Tries progressively looser queries until a match is found.
        """
        num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
        num_str = str(num_int)  # no zero-padding — GC titles use "#25" not "#025"
        year = store_date[:4] if store_date else None
        # Strip trailing (YYYY) from title — GC posts never include the year in the series name
        title = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()
        # Use series start year as fallback anchor so "Batman #1" doesn't match the wrong era
        anchor_year = year or (str(series_year) if series_year else None)

        queries = []
        if anchor_year:
            queries.append(f"{title} #{num_str} ({anchor_year})")
        queries += [
            f"{title} #{num_str}",
            f"{title} {num_str}",
            title,
        ]

        for query in queries:
            logger.info(f"GetComics search: {query!r}")
            if status_fn:
                status_fn(f"GetComics: “{query}”")
            post_url = self._search_page(query, title, issue_number)
            if post_url:
                url, fname = self._extract_download(post_url)
                if url:
                    return url, fname

        return None, None

    def search_trade(self, title: str, vol=None, vol_range=None, status_fn=None) -> tuple[str | None, str | None]:
        """Find a collected edition on GetComics. Returns (download_url, filename)
        or (None, None). 'TPB' is the magic word — 'Volume' returns nothing — and a
        single 'Vol 1 – 6' post can be the jackpot covering many volumes at once."""
        title = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()
        queries = []
        if vol is not None:
            queries += [f"{title} TPB Vol {vol}", f"{title} Vol {vol}"]
        if vol_range is not None:
            queries.append(f"{title} Vol {vol_range[0]}-{vol_range[1]}")
        queries += [f"{title} TPB", title]

        title_norm = _normalize(title)
        seen = set()
        for query in queries:
            if query in seen:
                continue
            seen.add(query)
            logger.info(f"GetComics trade search: {query!r}")
            if status_fn:
                status_fn(f"GetComics: “{query}”")
            post_url = self._search_trade_page(query, title_norm, vol, vol_range)
            if post_url:
                url, fname = self._extract_download(post_url)
                if url:
                    return url, fname
        return None, None

    def _search_trade_page(self, query: str, title_norm: str, vol, vol_range) -> str | None:
        try:
            r = self._get(BASE, params={"s": query})
            r.raise_for_status()
        except GCRateLimitError:
            raise
        except Exception as e:
            logger.warning(f"GetComics trade search request failed: {e}")
            return None
        soup = BeautifulSoup(r.text, "lxml")
        for article in soup.find_all("article", {"class": "post"}):
            h1 = article.find("h1", {"class": "post-title"})
            a = h1.find("a") if h1 else None
            if not a:
                continue
            text = a.get_text(strip=True)
            if _trade_post_matches(title_norm, _normalize(text), vol, vol_range):
                logger.info(f"GetComics: matched trade post {text!r}")
                return a.get("href", "")
        return None

    def _search_page(self, query: str, title: str, issue_number: float) -> str | None:
        try:
            r = self._get(BASE, params={"s": query})
            r.raise_for_status()
        except GCRateLimitError:
            raise
        except Exception as e:
            logger.warning(f"GetComics search request failed: {e}")
            return None

        soup = BeautifulSoup(r.text, "lxml")
        articles = soup.find_all("article", {"class": "post"})
        if not articles:
            logger.info(f"GetComics: no articles on search page for {query!r}")
            return None

        title_norm = _normalize(title)

        # Best match: series name matches AND post explicitly covers our issue number
        for article in articles:
            h1 = article.find("h1", {"class": "post-title"})
            if not h1:
                continue
            a = h1.find("a")
            if not a:
                continue
            text = a.get_text(strip=True)
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
            text = a.get_text(strip=True)
            href = a.get("href", "")
            if _series_matches(title_norm, _normalize(text)):
                coverage = _post_covers_issue(text, issue_number)
                if coverage is False:
                    logger.info(f"GetComics: fallback skipped {text!r} (wrong issue)")
                    continue
                logger.info(f"GetComics: fallback post {text!r}")
                return href

        return None

    def _extract_download(self, post_url: str) -> tuple[str | None, str | None]:
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

        # Strategy 1: find download groups — <p> containing "Language" marks a group
        for p in body.find_all("p"):
            if "Language" not in p.get_text():
                continue
            for sibling in p.next_siblings:
                if not isinstance(sibling, Tag):
                    continue
                if sibling.name == "hr":
                    break
                for btn_wrap in sibling.find_all("div", {"class": "aio-button-center"}):
                    a = btn_wrap.find("a", href=True)
                    if not a:
                        continue
                    href = a["href"]
                    text = a.get_text(strip=True).lower()
                    if any(t in text for t in GC_DIRECT_TERMS):
                        logger.info(f"GetComics: direct download {href[:80]}")
                        return href, None

        # Strategy 2: any aio-button-center on the page with direct-download text
        for btn_wrap in body.find_all("div", {"class": "aio-button-center"}):
            a = btn_wrap.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            text = a.get_text(strip=True).lower()
            if any(t in text for t in GC_DIRECT_TERMS):
                logger.info(f"GetComics: strategy-2 direct download {href[:80]}")
                return href, None

        # Strategy 3: any link ending in a comic file extension
        for a in body.find_all("a", href=True):
            href = a["href"]
            if any(href.lower().endswith(ext) for ext in (".cbz", ".cbr", ".zip")):
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
