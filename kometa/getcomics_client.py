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

_ISSUE_NUM_RE = re.compile(r'#(\d+(?:\.\d+)?)')


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[–—‒\-]+', ' ', text)   # all dash variants → space
    text = re.sub(r'[^\w\s]', ' ', text)     # strip remaining punctuation
    return re.sub(r'\s+', ' ', text).strip() # collapse whitespace


class GCRateLimitError(Exception):
    pass


class GetComicsClient:
    def __init__(self):
        self.session = cloudscraper.create_scraper()
        self.session.headers.update(HEADERS)

    def search(self, title: str, issue_number: float, store_date: str | None = None) -> tuple[str | None, str | None]:
        """
        Returns (download_url, hint_filename) or (None, None).
        Tries progressively looser queries until a match is found.
        """
        num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
        num_str = str(num_int)  # no zero-padding — GC titles use "#25" not "#025"
        year = store_date[:4] if store_date else None

        queries = []
        if year:
            queries.append(f"{title} #{num_str} ({year})")
        queries += [
            f"{title} #{num_str}",
            f"{title} {num_str}",
            title,
        ]

        for query in queries:
            logger.info(f"GetComics search: {query!r}")
            post_url = self._search_page(query, title, issue_number)
            if post_url:
                time.sleep(0.75)
                url, fname = self._extract_download(post_url)
                if url:
                    return url, fname

        return None, None

    def _search_page(self, query: str, title: str, issue_number: float) -> str | None:
        try:
            r = self.session.get(BASE, params={"s": query}, timeout=15)
            if r.status_code == 429:
                raise GCRateLimitError("Rate limited by GetComics")
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

        # Best match: series name AND issue number both present (numeric, not string)
        for article in articles:
            h1 = article.find("h1", {"class": "post-title"})
            if not h1:
                continue
            a = h1.find("a")
            if not a:
                continue
            text = a.get_text(strip=True)
            href = a.get("href", "")
            if title_norm in _normalize(text):
                nums = _ISSUE_NUM_RE.findall(text)
                if any(float(n) == issue_number for n in nums):
                    logger.info(f"GetComics: matched post {text!r}")
                    return href

        # Fallback: first result whose title contains the series name
        for article in articles:
            h1 = article.find("h1", {"class": "post-title"})
            if not h1:
                continue
            a = h1.find("a")
            if not a:
                continue
            text = a.get_text(strip=True)
            href = a.get("href", "")
            if title_norm in _normalize(text):
                logger.info(f"GetComics: fallback post {text!r}")
                return href

        return None

    def _extract_download(self, post_url: str) -> tuple[str | None, str | None]:
        try:
            r = self.session.get(post_url, timeout=15)
            r.raise_for_status()
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

        logger.info(f"GetComics: no download link found on {post_url}")
        return None, None
