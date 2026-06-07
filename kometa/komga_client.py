import os
import requests

# Empty defaults — real config comes from the DB (sources.komga passes it in).
# No hardcoded host/library: this has to run on anyone's setup, not one NAS.
BASE_URL = os.environ.get("KOMGA_URL", "")
AUTH = (
    os.environ.get("KOMGA_USER", ""),
    os.environ.get("KOMGA_PASS", ""),
)
LIBRARY_ID = os.environ.get("KOMGA_LIBRARY_ID", "")


class KomgaClient:
    def __init__(self, base_url=BASE_URL, auth=AUTH, library_id=LIBRARY_ID):
        self.session = requests.Session()
        self.session.auth = auth
        self.base_url = base_url.rstrip("/")
        self.library_id = library_id

    def _get(self, path, params=None):
        r = self.session.get(f"{self.base_url}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def search_series(self, query):
        return self._get("/api/v1/series", params={"search": query, "size": 50})["content"]

    def get_series(self, series_id):
        return self._get(f"/api/v1/series/{series_id}")

    def get_books(self, series_id):
        books, page = [], 0
        while True:
            data = self._get(f"/api/v1/series/{series_id}/books",
                             params={"page": page, "size": 500, "sort": "metadata.numberSort,asc"})
            books.extend(data["content"])
            if data["last"]:
                break
            page += 1
        return books

    def get_thumbnail_url(self, series_id):
        return f"{self.base_url}/api/v1/series/{series_id}/thumbnail"

    def scan_library(self):
        r = self.session.post(f"{self.base_url}/api/v1/libraries/{self.library_id}/scan")
        r.raise_for_status()

    def set_series_links(self, series_id, links):
        r = self.session.patch(
            f"{self.base_url}/api/v1/series/{series_id}/metadata",
            json={"links": links, "linksLock": False},
        )
        r.raise_for_status()
