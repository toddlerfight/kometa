import os
import time
import requests

BASE_URL = "https://metron.cloud/api"
AUTH = (
    os.environ.get("METRON_USER", ""),
    os.environ.get("METRON_PASS", ""),
)


class MetronClient:
    def __init__(self, base_url=BASE_URL, auth=AUTH):
        self.session = requests.Session()
        self.session.auth = auth
        self.base_url = base_url.rstrip("/")

    def _get(self, path, params=None):
        r = self.session.get(f"{self.base_url}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def search_series(self, name):
        return self._get("/series/", params={"name": name})["results"]

    def get_series(self, series_id):
        return self._get(f"/series/{series_id}/")

    def get_issues(self, series_id):
        issues, page = [], 1
        while True:
            data = self._get("/issue/", params={"series_id": series_id, "page": page})
            issues.extend(data["results"])
            if not data["next"]:
                break
            page += 1
            time.sleep(0.3)
        return issues
