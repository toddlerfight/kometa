import re
import time
import logging
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE = "https://leagueofcomicgeeks.com"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
S3_THUMB = "https://s3.amazonaws.com/comicgeeks/comics/covers/medium-{}.jpg"
S3_LARGE = "https://s3.amazonaws.com/comicgeeks/comics/covers/large-{}.jpg"
_COVER_ID_RE = re.compile(r'covers/(?:large|medium|small)-(\d+)\.jpg')

def _parse_search_html(html: str) -> list[dict]:
    """Parse LOCG search AJAX response HTML into [{id, title, publisher, year}]."""
    soup = BeautifulSoup(html, "lxml")
    results, seen = [], set()
    # Series results live in <li class="media"> with /comics/series/ links.
    # Publisher/date are siblings of the <a> inside the <li>, not children of it.
    for li in soup.find_all("li", class_="media"):
        a = li.find("a", href=re.compile(r"/comics/series/\d+/"))
        if not a:
            continue
        m = re.search(r"/comics/series/(\d+)/", a["href"])
        if not m:
            continue
        sid = int(m.group(1))
        if sid in seen:
            continue
        seen.add(sid)
        title_el = li.find(class_="title")
        title_text = title_el.get_text(strip=True) if title_el else ""
        if not title_text:
            img = li.find("img")
            title_text = img.get("alt", "").strip() if img else ""
        if not title_text:
            continue
        pub_el = li.find(class_=re.compile(r"\bpublisher\b"))
        date_el = li.find(class_=re.compile(r"\bdate\b"))
        year = None
        if date_el:
            dm = re.match(r"(\d{4})", date_el.get_text(strip=True))
            if dm:
                year = int(dm.group(1))
        results.append({
            "id": sid,
            "title": title_text,
            "publisher": pub_el.get_text(strip=True) if pub_el else "",
            "year": year,
        })
    return results


def search_series_anon(title: str) -> list[dict]:
    """Search LOCG without credentials — the search endpoint works without auth."""
    try:
        r = requests.get(
            f"{BASE}/search/ajax_issues",
            params={"query": title},
            headers={
                "User-Agent": _UA,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": BASE + "/",
            },
            timeout=10,
        )
        r.raise_for_status()
        return _parse_search_html(r.text)
    except Exception as e:
        logger.warning(f"LoCG anon search({title!r}) failed: {e}")
        return []


def _parse_num(title: str) -> float | None:
    m = re.search(r'#(\d+(?:\.\d+)?)', title)
    if m:
        return float(m.group(1))
    m = re.search(r'\bVol\.?\s*(\d+)\b', title, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


class LOCGClient:
    def __init__(self, username: str, password: str, session: str | None = None):
        self._username = username
        self._password = password
        self._s = requests.Session()
        self._s.headers["User-Agent"] = _UA
        self._session_cookie = session

        if session:
            self._s.cookies.set("ci_session", session, domain="leagueofcomicgeeks.com")
        else:
            self._login()

    def _login(self):
        r = self._s.post(
            f"{BASE}/user/login",
            data={"username": self._username, "password": self._password},
            allow_redirects=True,
            timeout=15,
        )
        r.raise_for_status()
        cookie = self._s.cookies.get("ci_session")
        if not cookie:
            raise ValueError("LoCG login failed — no session cookie returned")
        self._session_cookie = cookie
        logger.info("LoCG: logged in, session acquired")

    @property
    def session_cookie(self) -> str | None:
        return self._session_cookie

    def _get(self, url: str, **kwargs) -> requests.Response:
        r = self._s.get(url, timeout=15, **kwargs)
        # Session expired if we get redirected to login page
        if r.ok and "/user/login" in r.url:
            logger.info("LoCG: session expired, re-logging in")
            self._login()
            r = self._s.get(url, timeout=15, **kwargs)
        r.raise_for_status()
        return r

    def search_series(self, title: str) -> list[dict]:
        """Search for series by title. Returns [{id, title, publisher, year}]."""
        try:
            r = self._get(
                f"{BASE}/search/ajax_issues",
                params={"query": title},
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/"},
            )
            return _parse_search_html(r.text)
        except Exception as e:
            logger.warning(f"LoCG search_series({title!r}) failed: {e}")
            return []

    def find_series_id(self, title: str, year: int | None = None) -> int | None:
        """Find best matching LoCG series ID for a title+year."""
        results = self.search_series(title)
        if not results:
            return None
        title_l = title.lower()
        _punct = re.compile(r"[\-\–\—\:\,\.\!\?\'\"]+")
        title_norm = _punct.sub(" ", title_l).split()

        def score(r):
            s = 0
            r_title = r["title"].lower()
            r_norm = _punct.sub(" ", r_title).split()
            if r_title == title_l:
                s += 10
            elif title_l in r_title or r_title in title_l:
                s += 4
            elif title_norm == r_norm:
                s += 9
            elif all(w in r_norm for w in title_norm):
                s += 3
            if year and r.get("year") == year:
                s += 5
            return s

        results.sort(key=score, reverse=True)
        best = results[0]
        if score(best) >= 4:
            return best["id"]
        return None

    def get_issues(self, series_id: int) -> list[dict]:
        """Get all issues for a LoCG series. Returns [{number, store_date, cover}]."""
        return _get_issues_with_get(series_id, self._get)

    def fetch_variants(self, locg_issue_id: str) -> dict:
        """Fetch variant covers for an issue using the authenticated session."""
        return _fetch_variants_with_get(locg_issue_id, self._get)


def _get_issues_with_get(series_id: int, get_fn) -> list[dict]:
    """Shared issue-list scraping. get_fn(url, **kwargs) must return a requests-like
    Response. Works with both the authed session and an anonymous cloudscraper."""
    try:
        r = get_fn(
            f"{BASE}/comic/get_comics",
            params={
                "list": "series",
                "series_id": series_id,
                "view": "thumbs",
                "format[]": 1,
                "order": "asc",
            },
        )
        data = r.json()
        soup = BeautifulSoup(data["list"], "lxml")
        issues = []
        for li in soup.find_all("li"):
            title_el = li.find(class_="title")
            date_el = li.find(class_="date")
            img_el = li.find("img")
            if not title_el:
                continue
            num = _parse_num(title_el.text.strip())
            if num is None:
                continue
            store_date = None
            if date_el:
                ts = date_el.get("data-date")
                if ts:
                    try:
                        store_date = datetime.fromtimestamp(
                            int(ts), tz=timezone.utc
                        ).strftime("%Y-%m-%d")
                    except (ValueError, OverflowError, OSError):
                        pass
            cover = img_el.get("data-src") if img_el else None
            locg_issue_id = None
            if cover:
                m = _COVER_ID_RE.search(cover)
                if m:
                    locg_issue_id = m.group(1)
            issues.append({"number": num, "store_date": store_date, "cover": cover, "locg_issue_id": locg_issue_id})
        time.sleep(0.3)
        return issues
    except Exception as e:
        logger.warning(f"LoCG get_issues({series_id}) failed: {e}")
        return []


def get_issues_anon(series_id: int) -> list[dict]:
    """Fetch a series' issue list with no login, via cloudscraper. This is what
    lets sync build an issue list — and therefore detect missing issues — for a
    keyless, Komga-less install. Mirrors search_series_anon / fetch_variants."""
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'darwin', 'mobile': False}
        )
    except ImportError:
        raise RuntimeError("cloudscraper not installed") from None

    scraper.get(BASE + '/')
    return _get_issues_with_get(series_id, scraper.get)


def _fetch_variants_with_get(locg_issue_id: str, get_fn) -> dict:
    """Shared variant-scraping logic. get_fn(url, **kwargs) must return a requests.Response."""
    url = f"{BASE}/comic/{locg_issue_id}/comic"
    r = get_fn(url, headers={'Referer': BASE + '/', 'Accept': 'text/html'})
    if hasattr(r, 'raise_for_status'):
        r.raise_for_status()

    soup = BeautifulSoup(r.text, 'html.parser')
    title_tag = soup.find('h1') or soup.find('title')
    raw_title = title_tag.get_text(strip=True) if title_tag else ''
    issue_title = re.split(r'[\n|]', raw_title)[0].strip()

    covers = [{'id': locg_issue_id, 'name': 'Cover A (Main)',
               'thumb': S3_THUMB.format(locg_issue_id),
               'large': S3_LARGE.format(locg_issue_id)}]

    variant_div = soup.find(class_='variant-cover-list')
    if variant_div:
        seen = set()
        for a in variant_div.find_all('a', href=re.compile(r'\?variant=')):
            m = re.search(r'\?variant=(\d+)', a.get('href', ''))
            if not m:
                continue
            vid = m.group(1)
            if vid in seen:
                continue
            seen.add(vid)
            img = a.find('img')
            name = img.get('alt', f'Variant {vid}') if img else f'Variant {vid}'
            name = re.sub(rf'^{re.escape(issue_title)}\s*', '', name).strip() or f'Variant {vid}'
            covers.append({'id': vid, 'name': name,
                           'thumb': S3_THUMB.format(vid),
                           'large': S3_LARGE.format(vid)})

    return {'title': issue_title, 'covers': covers}


def fetch_variants(locg_issue_id: str) -> dict:
    """Fetch variants anonymously via cloudscraper. Prefer LOCGClient.fetch_variants() when auth is available."""
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'darwin', 'mobile': False}
        )
    except ImportError:
        raise RuntimeError("cloudscraper not installed") from None

    scraper.get(BASE + '/')
    return _fetch_variants_with_get(locg_issue_id, scraper.get)
