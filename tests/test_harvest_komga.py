"""Komga harvest: fills the record only where nothing better sits, pulls a
poster only when a person set one, never writes to Komga."""
import json
import os
import pytest

import kometa.db as db
import kometa.record as record
import kometa.covers as cv
import kometa.images as im
import kometa.harvest_komga as hk


class _Komga:
    def __init__(self):
        self.calls = []
        self.thumbs = [{"id": "T1", "type": "GENERATED", "selected": True}]
        self.books = [{"id": "B1", "name": "Run 001 (2024).cbz", "media": {"pagesCount": 24},
                       "metadata": {"summary": "Komga says", "authors": [{"name": "A. Writer", "role": "writer"}],
                                    "releaseDate": "2024-02-01", "isbn": None}},
                      {"id": "B2", "name": "Run 002 (2024).cbz", "media": {},
                       "metadata": {"summary": "Second", "authors": [], "releaseDate": None, "isbn": None}}]

    def get_series(self, sid):
        self.calls.append(("series", sid))
        return {"id": sid, "metadata": {"summary": "A run.", "publisher": "Image", "genres": ["crime"], "tags": ["noir"]}}

    def get_books(self, sid):
        self.calls.append(("books", sid))
        return self.books

    def get_series_thumbnails(self, sid):
        self.calls.append(("thumbs", sid))
        return self.thumbs

    def get_series_thumbnail_bytes(self, sid, tid):
        self.calls.append(("poster", sid, tid))
        return b"\xff\xd8poster"

    def __getattr__(self, name):          # any write (scan, analyze, set_book_number, PATCH) is a test failure
        raise AssertionError(f"harvest touched Komga.{name}")


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = str(tmp_path / "k.db")
    db.init_db(path)
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    monkeypatch.setattr(cv, "DB_PATH", path)
    sid = db.add_series(komga_series_id="K1", title="Run", publisher="Image", year_began=2024, path=path)
    db.upsert_issue_status(sid, 1.0, "2024-02-07", 1, path=path)
    db.upsert_issue_status(sid, 2.0, "2024-03-06", 1, path=path)
    return path, sid


def test_harvest_fills_open_rows_and_skips_catalogue_rows(world):
    path, sid = world
    record.write_issue(sid, 2, {"desc": "Metron says"}, "metron", "full", path=path)
    k = _Komga()
    out = hk.harvest(path=path, komga=k)
    assert out == {"series_seen": 1, "series": 1, "issues": 1, "posters": 0}
    r1 = record.get_issue(sid, 1, path)
    assert r1["source"] == "komga" and r1["fill_state"] == "partial" and r1["desc"] == "Komga says"
    assert r1["credits"] == [{"role": "writer", "name": "A. Writer", "metron_creator_id": None}]
    assert r1["store_date"] == "2024-02-01" and r1["page_count"] == 24
    assert record.get_issue(sid, 2, path)["desc"] == "Metron says"          # never overwritten
    s = record.get_series(sid, path)
    assert s["source"] == "komga" and s["desc"] == "A run." and json.loads(s["genres_json"]) == ["crime"]
    assert all(c[0] in ("series", "books", "thumbs") for c in k.calls)      # reads only


def test_generated_poster_is_not_a_poster_but_an_uploaded_one_is(world):
    path, sid = world
    k = _Komga()
    hk.harvest(path=path, komga=k)
    assert cv.local(cv.series_komga_key(sid), path) is None
    k.thumbs = [{"id": "T2", "type": "USER_UPLOADED", "selected": True}, {"id": "T1", "type": "GENERATED", "selected": False}]
    out = hk.harvest(path=path, komga=k)
    assert out["posters"] == 1
    p = cv.local(cv.series_komga_key(sid), path)
    assert p and os.path.exists(p)


def test_komga_row_gives_way_to_a_catalogue_but_not_to_comicinfo(world):
    path, sid = world
    record.write_issue(sid, 1, {"desc": "from the file"}, "comicinfo", "partial", path=path)
    hk.harvest(path=path, komga=_Komga())
    assert record.get_issue(sid, 1, path)["source"] == "comicinfo"          # equal rank: first in stays
    assert hk._open_for({"source": "komga", "fill_state": "partial"}, "metron")
    assert not hk._open_for({"source": "locg", "fill_state": "full"}, "komga")
