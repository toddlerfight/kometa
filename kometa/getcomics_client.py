import time
import logging
import requests
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

BASE = "https://getcomics.org"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Button text that signals a GetComics-hosted direct download
GC_DIRECT_TERMS = (
    "download now", "main download", "main server", "main link",
    "mirror download", "mirror server", "mirror link", "link 1", "link 2", "getcomics",
)


class GetComicsClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def search(self, title: str, issue_number: float) -> tuple[str | None, str | None]:
        """
        Returns (download_url, hint_filename) or (None, None).
        Tries progressively looser queries until a match is found.
        """
        num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
        num_str = f"{num_int:03d}" if isinstance(num_int, int) else str(num_int)

        queries = [
            f"{title} #{num_str}",
            f"{title} {num_str}",
            title,
        ]

        for query in queries:
            logger.info(f"GetComics search: {query!r}")
            post_url = self._search_page(query, title, num_str)
            if post_url:
                time.sleep(0.75)
                url, fname = self._extract_download(post_url)
                if url:
                    return url, fname

        return None, None

    def _search_page(self, query: str, title: str, num_str: str) -> str | None:
        try:
            r = self.session.get(BASE, params={"s": query}, timeout=15)
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"GetComics search request failed: {e}")
            return None

        soup = BeautifulSoup(r.text, "lxml")
        articles = soup.find_all("article", {"class": "post"})
        if not articles:
            logger.info(f"GetComics: no articles on search page for {query!r}")
            return None

        title_lower = title.lower()
        num_needle = f"#{num_str}"

        # Best match: title + issue number both in post title
        for article in articles:
            h1 = article.find("h1", {"class": "post-title"})
            if not h1:
                continue
            a = h1.find("a")
            if not a:
                continue
            text = a.get_text(strip=True).lower()
            href = a.get("href", "")
            if title_lower in text and num_needle.lower() in text:
                logger.info(f"GetComics: matched post {text!r}")
                return href

        # Fallback: first result whose title contains our series name
        for article in articles:
            h1 = article.find("h1", {"class": "post-title"})
            if not h1:
                continue
            a = h1.find("a")
            if not a:
                continue
            text = a.get_text(strip=True).lower()
            href = a.get("href", "")
            if title_lower in text:
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
            body = soup  # fallback to full page

        # Strategy 1: find download groups — <p> containing "Language" marks a group,
        # followed by <div class="aio-button-center"> siblings with actual links
        for p in body.find_all("p"):
            if "Language" not in p.get_text():
                continue
            # Walk siblings until we hit <hr> or run out
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
