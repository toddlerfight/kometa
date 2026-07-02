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

    def create_or_update_readlist(self, name, book_ids, summary=""):
        """Create an ordered readlist, or replace an existing one with the same
        name (so 'Rebuild' re-syncs instead of erroring on the duplicate name).
        Komga's ?search on readlists is unreliable, so page through them all and
        match the name ourselves — otherwise a rebuild POSTs a dupe and 400s."""
        match, page = None, 0
        while not match:
            data = self._get("/api/v1/readlists", params={"page": page, "size": 500})
            match = next((r for r in data["content"] if r["name"] == name), None)
            if data.get("last", True):
                break
            page += 1
        if match:
            r = self.session.patch(f"{self.base_url}/api/v1/readlists/{match['id']}",
                                   json={"bookIds": book_ids})
            r.raise_for_status()
            return {"id": match["id"], "updated": True}
        r = self.session.post(f"{self.base_url}/api/v1/readlists",
                             json={"name": name, "summary": summary,
                                   "ordered": True, "bookIds": book_ids})
        r.raise_for_status()
        return {"id": r.json().get("id"), "updated": False}

    def scan_library(self):
        r = self.session.post(f"{self.base_url}/api/v1/libraries/{self.library_id}/scan")
        r.raise_for_status()

    def analyze_book(self, book_id):
        """Re-analyze a single book so Komga re-extracts its cover/pages from the file
        on disk — needed after we rewrite a CBZ (variant cover inject), or Komga keeps
        serving the thumbnail it cached on its last scan."""
        r = self.session.post(f"{self.base_url}/api/v1/books/{book_id}/analyze")
        r.raise_for_status()

    def set_book_number(self, book_id, number, number_sort):
        """Correct a book's issue number IN Komga (and lock it, so a rescan can't revert).
        Komga's own filename/metadata number parsing is unreliable; Kometa derives the true
        number from the filename and pushes it here so Komga's labels AND ordering (which
        sorts by numberSort) are right."""
        r = self.session.patch(
            f"{self.base_url}/api/v1/books/{book_id}/metadata",
            json={
                "number": str(number), "numberLock": True,
                "numberSort": float(number_sort), "numberSortLock": True,
            },
        )
        r.raise_for_status()
