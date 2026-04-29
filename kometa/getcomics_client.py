import time
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE = "https://getcomics.info"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
EXTS = {".cbz", ".cbr", ".zip", ".rar"}


class GetComicsClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def search(self, title: str, issue_number: float) -> tuple[str | None, str | None]:
        """
        Returns (download_url, filename) or (None, None) if not found.
        download_url may be a direct file link or a GetComics redirect link.
        """
        num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
        num_str = f"{num_int:03d}" if isinstance(num_int, int) else str(num_int)
        query = f"{title} #{num_str}"
        logger.info(f"GetComics search: {query!r}")

        try:
            r = self.session.get(BASE, params={"s": query}, timeout=15)
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"GetComics search failed: {e}")
            return None, None

        soup = BeautifulSoup(r.text, "lxml")
        post_url = self._best_post(soup, title, num_str)
        if not post_url:
            logger.info("GetComics: no matching post found")
            return None, None

        time.sleep(0.75)
        return self._extract_download(post_url)

    def _best_post(self, soup: BeautifulSoup, title: str, num_str: str) -> str | None:
        title_lower = title.lower()
        num_needle = f"#{num_str}"

        # WordPress posts come in article tags or divs with class containing 'post'
        posts = soup.select("article") or soup.select(".post")
        if not posts:
            return None

        for post in posts:
            link = post.select_one("h1 a, h2 a, .post-title a, .entry-title a")
            if not link:
                continue
            text = link.get_text(strip=True).lower()
            href = link.get("href", "")
            if title_lower in text and num_needle.lower() in text:
                return href

        # fallback: first result
        link = posts[0].select_one("h1 a, h2 a, .post-title a, .entry-title a")
        return link["href"] if link else None

    def _extract_download(self, post_url: str) -> tuple[str | None, str | None]:
        try:
            r = self.session.get(post_url, timeout=15)
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"GetComics post fetch failed: {e}")
            return None, None

        soup = BeautifulSoup(r.text, "lxml")

        # Priority 1: direct file links (.cbz/.cbr/.zip/.rar)
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(href.lower().endswith(ext) for ext in EXTS):
                filename = href.rsplit("/", 1)[-1]
                return href, filename

        # Priority 2: GetComics redirect / download buttons
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True).lower()
            if ("getcomics.info" in href or "links." in href) and (
                "download" in text or "get" in text or "cbz" in text or "cbr" in text
            ):
                return href, None

        # Priority 3: any button-like link with download intent
        for a in soup.select(".wp-block-button a, .dlbutton, a.download-btn"):
            href = a.get("href", "")
            if href:
                return href, None

        logger.info(f"GetComics: no download link found on {post_url}")
        return None, None
