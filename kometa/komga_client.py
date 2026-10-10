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
    # Komga is LAN-local so this is generous — but without SOME bound, a hung
    # Komga pins a threadpool worker per thumbnail request until the pool starves.
    # Every other client here passes a timeout; this one was the odd one out.
    TIMEOUT = 30

    def __init__(self, base_url=BASE_URL, auth=AUTH, library_id=LIBRARY_ID):
        self.session = requests.Session()
        self.session.auth = auth
        self.base_url = base_url.rstrip("/")
        self.library_id = library_id

    def _get(self, path, params=None):
        r = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.TIMEOUT)
        r.raise_for_status()
        return r.json()

    def get_all_series(self):
        """Every series in the library, paginated. Used for punctuation-proof
        local title matching — Komga's own /search is fussy about ':' vs '-' etc,
        so we pull the lot once and match normalised on our side instead."""
        series, page = [], 0
        while True:
            data = self._get("/api/v1/series",
                             params={"page": page, "size": 500, "sort": "metadata.titleSort,asc"})
            series.extend(data["content"])
            if data["last"]:
                break
            page += 1
        return series

    def get_series(self, series_id):
        return self._get(f"/api/v1/series/{series_id}")

    def get_series_thumbnails(self, series_id):
        """Every poster Komga holds for a series — type GENERATED (page 1),
        SIDECAR (a cover.jpg beside the files) or USER_UPLOADED, one `selected`."""
        return self._get(f"/api/v1/series/{series_id}/thumbnails")

    def get_series_thumbnail_bytes(self, series_id, thumbnail_id) -> bytes:
        r = self.session.get(f"{self.base_url}/api/v1/series/{series_id}/thumbnails/{thumbnail_id}", timeout=self.TIMEOUT)
        r.raise_for_status()
        return r.content

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

    def scan_library(self, deep=False):
        # deep=True makes Komga re-read every series folder instead of skipping
        # the ones whose directory mtime hasn't moved. Over SMB a new file does
        # NOT always bump the folder's mtime — a normal scan then walks right past it.
        r = self.session.post(f"{self.base_url}/api/v1/libraries/{self.library_id}/scan",
                              params={"deep": "true"} if deep else None,
                              timeout=self.TIMEOUT)
        r.raise_for_status()
