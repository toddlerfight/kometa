import re
import time
import logging
import requests

logger = logging.getLogger(__name__)
BASE_URL = "https://comicvine.gamespot.com/api"


def _cv_num(issue_number_str) -> float | None:
    s = (issue_number_str or "").strip().lstrip("#")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        m = re.search(r"(\d+(?:\.\d+)?)", s)
        return float(m.group(1)) if m else None


class ComicVineClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kometa/1.0"})

    def _params(self, extra: dict) -> dict:
        return {"api_key": self.api_key, "format": "json", **extra}

    def _best_volume(self, results: list, title: str, year: int | None) -> dict | None:
        if not results:
            return None
        title_l = title.lower()

        def score(v):
            s = 0
            if (v.get("name") or "").lower() == title_l:
                s += 10
            if year and str(v.get("start_year", "")) == str(year):
                s += 5
            return s

        return max(results, key=score)

    def find_series_image(self, title: str, year: int | None = None) -> str | None:
        """Search CV volumes by title, optionally filter by start year. Returns image URL or None."""
        r = self.session.get(
            f"{BASE_URL}/search/",
            params=self._params({
                "resources": "volume",
                "query": title,
                "field_list": "id,name,image,start_year,count_of_issues",
                "limit": 10,
            }),
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        match = self._best_volume(results, title, year)
        if not match:
            return None
        img = match.get("image") or {}
        return img.get("medium_url") or img.get("small_url") or img.get("thumb_url")

    def get_volume_id(self, title: str, year: int | None = None) -> int | None:
        """Find CV volume ID for a series title+year."""
        try:
            r = self.session.get(
                f"{BASE_URL}/search/",
                params=self._params({
                    "resources": "volume",
                    "query": title,
                    "field_list": "id,name,start_year",
                    "limit": 10,
                }),
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("error") != "OK":
                return None
            match = self._best_volume(data.get("results", []), title, year)
            return match.get("id") if match else None
        except Exception as e:
            logger.warning(f"CV get_volume_id({title!r}) failed: {e}")
            return None

    def get_issues(self, volume_id: int) -> list[dict]:
        """Get all issues for a CV volume. Returns [{number, store_date, cover}]."""
        issues = []
        offset = 0
        limit = 100
        try:
            while True:
                r = self.session.get(
                    f"{BASE_URL}/issues/",
                    params=self._params({
                        "filter": f"volume:{volume_id}",
                        "field_list": "issue_number,store_date,image",
                        "limit": limit,
                        "offset": offset,
                        "sort": "issue_number:asc",
                    }),
                    timeout=15,
                )
                r.raise_for_status()
                data = r.json()
                if data.get("error") != "OK":
                    break
                batch = data.get("results", [])
                for ci in batch:
                    num = _cv_num(ci.get("issue_number"))
                    if num is None:
                        continue
                    img = ci.get("image") or {}
                    issues.append({
                        "number": num,
                        "store_date": ci.get("store_date") or None,
                        "cover": (img.get("medium_url") or img.get("small_url") or img.get("thumb_url")),
                    })
                total = data.get("number_of_total_results", 0)
                offset += limit
                if not batch or offset >= total:
                    break
                time.sleep(0.5)
        except Exception as e:
            logger.warning(f"CV get_issues({volume_id}) failed: {e}")
        return issues
