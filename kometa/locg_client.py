import re
import time
import logging
from copy import deepcopy
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE   = "https://leagueofcomicgeeks.com"
SEARCH = f"{BASE}/search/ajax_issues"
_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE + "/",
}


class LOCGClient:
    def __init__(self):
        import cloudscraper
        self._s = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "darwin", "mobile": False}
        )
        try:
            self._s.get(BASE, timeout=12)
            time.sleep(0.5)
        except Exception as e:
            logger.warning(f"LOCG: session seed failed: {e}")

    def search_series(self, query: str) -> list[dict]:
        try:
            r = self._s.get(SEARCH, params={"query": query}, headers=_HEADERS, timeout=15)
            time.sleep(0.5)
            if not r.ok:
                logger.warning(f"LOCG search HTTP {r.status_code} for {query!r}")
                return []
            return self._parse(r.text)
        except Exception as e:
            logger.warning(f"LOCG search error for {query!r}: {e}")
            return []

    def _parse(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        results, seen = [], set()

        for a in soup.find_all("a", href=True):
            m = re.search(r"/comics/series/(\d+)/", a["href"])
            if not m:
                continue
            sid = int(m.group(1))
            if sid in seen:
                continue
            seen.add(sid)

            pub_el  = a.find(class_=re.compile(r"\bpublisher\b"))
            date_el = a.find(class_=re.compile(r"\bdate\b"))
            publisher  = pub_el.get_text(strip=True) if pub_el else ""
            year_start = None
            if date_el:
                dm = re.match(r"(\d{4})", date_el.get_text(strip=True))
                if dm:
                    year_start = int(dm.group(1))

            # Title = link text minus the pub/date sub-elements
            a_copy = deepcopy(a)
            for sub in a_copy.find_all(class_=re.compile(r"\b(publisher|date)\b")):
                sub.decompose()
            title = a_copy.get_text(separator=" ", strip=True)

            if title and len(title) > 1:
                results.append({
                    "id":         sid,
                    "title":      title,
                    "publisher":  publisher,
                    "year_start": year_start,
                })

        return results[:10]
