import requests

BASE_URL = "https://comicvine.gamespot.com/api"


class ComicVineClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kometa/1.0"})

    def _params(self, extra: dict) -> dict:
        return {"api_key": self.api_key, "format": "json", **extra}

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
        if not results:
            return None

        if year:
            # prefer exact year match, fall back to any result
            match = next((v for v in results if str(v.get("start_year", "")) == str(year)), None)
            if not match:
                match = results[0]
        else:
            match = results[0]

        img = match.get("image") or {}
        return img.get("medium_url") or img.get("small_url") or img.get("thumb_url")
