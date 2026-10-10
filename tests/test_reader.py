"""Reader server half (docs/reader-spec.md): books, pages, progress.
Real CBZs with real images — the dimension scan and the resize are the point."""
import io
import zipfile

import pytest
from fastapi import HTTPException
from PIL import Image

import kometa.db as db
import kometa.reader as rd


def _img(w, h, color=(120, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


def make_book(path, pages):
    """pages = [(name, w, h), ...] written in the given (deliberately unsorted) order."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, w, h in pages:
            zf.writestr(name, _img(w, h))
        zf.writestr("ComicInfo.xml", "<ComicInfo/>")
        zf.writestr("__MACOSX/._p1.jpg", b"junk")
    return path


@pytest.fixture
def shelf(tmp_path, db_path, monkeypatch):
    monkeypatch.setattr(rd, "DB_PATH", db_path)
    monkeypatch.setattr(rd, "PAGE_CACHE_DIR", str(tmp_path / "page-cache"))
    folder = tmp_path / "comics" / "Marvel Comics" / "Midnight Spider-Man"
    folder.mkdir(parents=True)
    make_book(folder / "Midnight Spider-Man #001.cbz",
              [("p10.jpg", 1600, 2460), ("p2.jpg", 1600, 2460), ("p1.jpg", 1600, 2460),
               ("p3.jpg", 3200, 2460)] + [(f"p{i}.jpg", 1600, 2460) for i in range(4, 10)])
    sid = db.add_series(komga_series_id=None, title="Midnight Spider-Man", publisher="Marvel",
                        folder_path=str(folder), path=db_path)
    return db_path, sid


class TestBooks:
    def test_issue_resolves_to_a_book_in_natural_page_order(self, shelf):
        _, sid = shelf
        b = rd.issue_book(sid, 1.0)
        assert b["page_count"] == 10                      # ComicInfo + __MACOSX junk excluded
        names = [p[0] for p in db.get_book(b["id"], shelf[0])["pages"]]
        assert names[:4] == ["p1.jpg", "p2.jpg", "p3.jpg", "p4.jpg"] and names[-1] == "p10.jpg"
        assert b["title"] == "Midnight Spider-Man" and b["series_id"] == sid and b["number"] == 1.0

    def test_wide_spread_is_flagged(self, shelf):
        b = rd.issue_book(shelf[1], 1.0)
        assert [p["wide"] for p in b["pages"]][:4] == [False, False, True, False]

    def test_same_file_is_one_book_and_not_rescanned(self, shelf, monkeypatch):
        a = rd.issue_book(shelf[1], 1.0)
        monkeypatch.setattr(rd, "_scan_pages", lambda *a, **k: pytest.fail("rescanned an unchanged file"))
        assert rd.issue_book(shelf[1], 1.0)["id"] == a["id"]

    def test_issue_without_a_file_404s(self, shelf):
        with pytest.raises(HTTPException) as e:
            rd.issue_book(shelf[1], 2.0)
        assert e.value.status_code == 404


class TestPages:
    def test_page_is_resized_to_the_bucket_and_cached(self, shelf, monkeypatch):
        b = rd.issue_book(shelf[1], 1.0)
        resp = rd.book_page(b["id"], 1, w=1000)
        with Image.open(io.BytesIO(resp.body)) as im:
            assert im.size[0] == 1080                     # 1000 → the 1080 bucket
        assert "immutable" in resp.headers["cache-control"]
        monkeypatch.setattr(rd, "_render", lambda *a: pytest.fail("cache miss on a cached page"))
        assert rd.book_page(b["id"], 1, w=1000).body == resp.body

    def test_never_upscaled(self, shelf):
        b = rd.issue_book(shelf[1], 1.0)
        with Image.open(io.BytesIO(rd.book_page(b["id"], 2, w=4000).body)) as im:
            assert im.size[0] == 1600                     # source is 1600 < 2048 bucket

    def test_out_of_range_page_404s(self, shelf):
        b = rd.issue_book(shelf[1], 1.0)
        for p in (0, 11):
            with pytest.raises(HTTPException) as e:
                rd.book_page(b["id"], p)
            assert e.value.status_code == 404


class TestProgress:
    def _put(self, book_id, page, ts, **k):
        return rd.put_progress(book_id, rd.ProgressRequest(page=page, updated_at=ts, **k))

    def test_finished_within_last_three_pages(self, shelf):
        b = rd.issue_book(shelf[1], 1.0)
        assert self._put(b["id"], 7, "2026-10-08T01:00:00Z")["progress"]["completed"] == 0
        assert self._put(b["id"], 8, "2026-10-08T01:01:00Z")["progress"]["completed"] == 1   # 10 pages: 8,9,10
        assert rd.book_detail(b["id"])["progress"]["page"] == 8

    def test_stale_write_is_refused_with_the_stored_progress(self, shelf):
        b = rd.issue_book(shelf[1], 1.0)
        self._put(b["id"], 5, "2026-10-08T02:00:00Z")
        with pytest.raises(HTTPException) as e:
            self._put(b["id"], 2, "2026-10-08T01:00:00Z")    # older device, older clock
        assert e.value.status_code == 409 and e.value.detail["progress"]["page"] == 5

    def test_explicit_mark_as_read_wins(self, shelf):
        b = rd.issue_book(shelf[1], 1.0)
        assert self._put(b["id"], 2, "2026-10-08T01:00:00Z", completed=True)["progress"]["completed"] == 1

    def test_bad_timestamp_and_page_rejected(self, shelf):
        b = rd.issue_book(shelf[1], 1.0)
        for page, ts in ((3, "yesterday"), (3, "2026-10-08 01:00:00"), (99, "2026-10-08T01:00:00Z")):
            with pytest.raises(HTTPException) as e:
                self._put(b["id"], page, ts)
            assert e.value.status_code == 422

    def test_progress_is_per_reader(self, shelf):
        b = rd.issue_book(shelf[1], 1.0)
        db.set_progress("someone-else", b["id"], 9, True, "2026-10-08T05:00:00Z", shelf[0])
        assert rd.book_detail(b["id"])["progress"] is None


def test_a_truncated_cover_still_renders(tmp_path, monkeypatch):
    """A JPEG short by a few bytes is still a cover, not a 422."""
    import io, zipfile
    from PIL import Image
    from kometa import reader
    monkeypatch.setattr(reader, "PAGE_CACHE_DIR", str(tmp_path / "cache"))
    buf = io.BytesIO(); Image.new("RGB", (400, 600), "red").save(buf, "JPEG"); data = buf.getvalue()[:-5]
    p = tmp_path / "Short #001.cbz"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Short 001-000.jpg", data); z.writestr("Short 001-001.jpg", buf.getvalue())
    out = reader.get_cover_bytes(str(p))
    assert out[:2] == b"\xff\xd8" and len(out) > 500


def test_a_cut_short_first_page_is_not_served_as_the_cover(tmp_path, db_path, monkeypatch):
    """City of Madness #001: the scan's page 1 ends halfway. The reader can show it
    (grey below the break), but the cover routes must fall through to the catalogue."""
    import pytest
    monkeypatch.setattr(rd, "DB_PATH", db_path)
    monkeypatch.setattr(rd, "PAGE_CACHE_DIR", str(tmp_path / "page-cache"))
    whole = _img(600, 900)
    cut = whole[: len(whole) // 2]
    with zipfile.ZipFile(tmp_path / "bad.cbz", "w") as zf:
        zf.writestr("p1.jpg", cut); zf.writestr("p2.jpg", whole)
    with pytest.raises(rd.TruncatedCover):
        rd.get_cover_bytes(str(tmp_path / "bad.cbz"))
    with zipfile.ZipFile(tmp_path / "good.cbz", "w") as zf:
        zf.writestr("p1.jpg", whole)
    assert len(rd.get_cover_bytes(str(tmp_path / "good.cbz"))) > 100
    from PIL import ImageFile
    assert ImageFile.LOAD_TRUNCATED_IMAGES is True                       # the reader's leniency restored
