"""Reader server half (docs/reader-spec.md): books, pages, progress.
Real CBZs with real images — the dimension scan and the resize are the point."""
import asyncio
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
    sid = db.add_series(title="Midnight Spider-Man", publisher="Marvel",
                        folder_path=str(folder), path=db_path)
    return db_path, sid


class TestBooks:
    def test_issue_resolves_to_a_book_in_natural_page_order(self, shelf):
        _, sid = shelf
        b = asyncio.run(rd.issue_book(sid, 1.0))
        assert b["page_count"] == 10                      # ComicInfo + __MACOSX junk excluded
        names = [p[0] for p in db.get_book(b["id"], shelf[0])["pages"]]
        assert names[:4] == ["p1.jpg", "p2.jpg", "p3.jpg", "p4.jpg"] and names[-1] == "p10.jpg"
        assert b["title"] == "Midnight Spider-Man" and b["series_id"] == sid and b["number"] == 1.0

    def test_payload_names_each_page_and_the_file_for_a_device_copy(self, shelf):
        b = asyncio.run(rd.issue_book(shelf[1], 1.0))
        assert [p["name"] for p in b["pages"]][:3] == ["p1.jpg", "p2.jpg", "p3.jpg"]
        assert b["file"] == "Midnight Spider-Man #001.cbz" and b["folder"] == "Midnight Spider-Man"

    def test_opens_on_the_page_list_and_flags_wide_spreads_once_sizes_land(self, shelf):
        b = asyncio.run(rd.issue_book(shelf[1], 1.0))
        assert b["page_count"] and not any(p["wide"] for p in b["pages"])      # open now, sizes still coming
        rd._dims_pool.submit(lambda: None).result()                              # background read done
        b = asyncio.run(rd.book_detail(b["id"]))
        assert [p["wide"] for p in b["pages"]][:4] == [False, False, True, False]

    def test_same_file_is_one_book_and_not_rescanned(self, shelf, monkeypatch):
        a = asyncio.run(rd.issue_book(shelf[1], 1.0))
        rd._dims_pool.submit(lambda: None).result()
        monkeypatch.setattr(rd, "_scan_pages", lambda *a, **k: pytest.fail("rescanned an unchanged file"))
        assert asyncio.run(rd.issue_book(shelf[1], 1.0))["id"] == a["id"]

    def test_a_counted_but_never_listed_book_is_listed_on_open_and_its_pages_render(self, shelf):
        # covers / OPDS fill page_count without the page list: 1,329 such rows (2026-10-26). Opened by
        # issue it skipped the listing, and every page failed 'list index out of range' — a black reader
        b = asyncio.run(rd.issue_book(shelf[1], 1.0))
        rd._dims_pool.submit(lambda: None).result()
        with db._connect(shelf[0]) as conn:
            conn.execute("UPDATE books SET pages_json = NULL WHERE id = ?", (b["id"],))
        assert not db.get_book(b["id"], shelf[0])["pages"] and db.get_book(b["id"], shelf[0])["page_count"] == 10
        again = asyncio.run(rd.issue_book(shelf[1], 1.0))
        assert len(again["pages"]) == 10
        with db._connect(shelf[0]) as conn:
            conn.execute("UPDATE books SET pages_json = NULL WHERE id = ?", (b["id"],))
        r = asyncio.run(rd.book_page(b["id"], 1, 480))                     # straight to a page: listed there too
        assert r.status_code == 200 and r.body[:2] == b"\xff\xd8"

    def test_issue_without_a_file_404s(self, shelf):
        with pytest.raises(HTTPException) as e:
            asyncio.run(rd.issue_book(shelf[1], 2.0))
        assert e.value.status_code == 404


class TestPages:
    def test_page_is_resized_to_the_bucket_and_cached(self, shelf, monkeypatch):
        b = asyncio.run(rd.issue_book(shelf[1], 1.0))
        resp = asyncio.run(rd.book_page(b["id"], 1, w=1000))
        with Image.open(io.BytesIO(resp.body)) as im:
            assert im.size[0] == 1080                     # 1000 → the 1080 bucket
        assert "immutable" in resp.headers["cache-control"]
        monkeypatch.setattr(rd, "_render", lambda *a: pytest.fail("cache miss on a cached page"))
        assert asyncio.run(rd.book_page(b["id"], 1, w=1000)).body == resp.body

    def test_never_upscaled(self, shelf):
        b = asyncio.run(rd.issue_book(shelf[1], 1.0))
        with Image.open(io.BytesIO(asyncio.run(rd.book_page(b["id"], 2, w=4000)).body)) as im:
            assert im.size[0] == 1600                     # source is 1600 < 2048 bucket

    def test_out_of_range_page_404s(self, shelf):
        b = asyncio.run(rd.issue_book(shelf[1], 1.0))
        for p in (0, 11):
            with pytest.raises(HTTPException) as e:
                asyncio.run(rd.book_page(b["id"], p))
            assert e.value.status_code == 404


class TestProgress:
    def _put(self, book_id, page, ts, **k):
        return rd.put_progress(book_id, rd.ProgressRequest(page=page, updated_at=ts, **k))

    def test_finished_within_last_three_pages(self, shelf):
        b = asyncio.run(rd.issue_book(shelf[1], 1.0))
        assert self._put(b["id"], 7, "2026-10-08T01:00:00Z")["progress"]["completed"] == 0
        assert self._put(b["id"], 8, "2026-10-08T01:01:00Z")["progress"]["completed"] == 1   # 10 pages: 8,9,10
        assert asyncio.run(rd.book_detail(b["id"]))["progress"]["page"] == 8

    def test_stale_write_is_refused_with_the_stored_progress(self, shelf):
        b = asyncio.run(rd.issue_book(shelf[1], 1.0))
        self._put(b["id"], 5, "2026-10-08T02:00:00Z")
        with pytest.raises(HTTPException) as e:
            self._put(b["id"], 2, "2026-10-08T01:00:00Z")    # older device, older clock
        assert e.value.status_code == 409 and e.value.detail["progress"]["page"] == 5

    def test_explicit_mark_as_read_wins(self, shelf):
        b = asyncio.run(rd.issue_book(shelf[1], 1.0))
        assert self._put(b["id"], 2, "2026-10-08T01:00:00Z", completed=True)["progress"]["completed"] == 1

    def test_bad_timestamp_and_page_rejected(self, shelf):
        b = asyncio.run(rd.issue_book(shelf[1], 1.0))
        for page, ts in ((3, "yesterday"), (3, "2026-10-08 01:00:00"), (99, "2026-10-08T01:00:00Z")):
            with pytest.raises(HTTPException) as e:
                self._put(b["id"], page, ts)
            assert e.value.status_code == 422

    def test_progress_is_per_reader(self, shelf):
        b = asyncio.run(rd.issue_book(shelf[1], 1.0))
        db.set_progress("someone-else", b["id"], 9, True, "2026-10-08T05:00:00Z", shelf[0])
        assert asyncio.run(rd.book_detail(b["id"]))["progress"] is None


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


class TestRenderAhead:
    """One cache miss opens the archive once and renders the next pages from that
    same open book; a request for a page already in flight waits instead of reopening."""

    def _book(self, shelf):
        b = asyncio.run(rd.issue_book(shelf[1], 1.0))
        return db.get_book(b["id"], shelf[0])

    @staticmethod
    def _count_opens(monkeypatch):
        opens = []
        real = rd._Book

        class Counting(real):
            def __init__(self, *a, **k):
                opens.append(1)
                super().__init__(*a, **k)
        monkeypatch.setattr(rd, "_Book", Counting)
        return opens

    @staticmethod
    def _join_ahead():
        import threading
        for t in threading.enumerate():
            if t.name == "page-ahead":
                t.join(30)

    def test_one_open_serves_the_page_and_fills_the_next_four(self, shelf, monkeypatch):
        book = self._book(shelf)
        opens = self._count_opens(monkeypatch)
        rd._inflight.clear()
        data = rd.get_page_bytes(book, 0, 1080)
        self._join_ahead()
        with Image.open(io.BytesIO(data)) as im:
            assert im.size[0] == 1080
        cached = [i for i in range(10) if __import__("os").path.exists(rd._page_cache_path(book, i, 1080))]
        assert cached == [0, 1, 2, 3, 4]
        assert sum(opens) == 1
        assert not rd._inflight

    def test_a_page_in_flight_is_waited_for_not_reopened(self, shelf, monkeypatch):
        import threading
        book = self._book(shelf)
        opens = self._count_opens(monkeypatch)
        key = rd._page_key(book, 3, 1080)
        ev = threading.Event()
        rd._inflight[key] = ev
        got = []
        t = threading.Thread(target=lambda: got.append(rd.get_page_bytes(book, 3, 1080)))
        t.start()
        t.join(0.3)
        assert t.is_alive() and not got                              # waiting on the render in flight
        rd._write_cached(rd._page_cache_path(book, 3, 1080), b"rendered-elsewhere")
        rd._finish(key, ev)
        t.join(5)
        assert got == [b"rendered-elsewhere"] and sum(opens) == 0

    def test_pages_are_baseline_jpegs_decoded_at_draft_scale(self, shelf, monkeypatch):
        book = self._book(shelf)
        rd._inflight.clear()
        from PIL import JpegImagePlugin
        drafts = []
        real = JpegImagePlugin.JpegImageFile.draft                   # the JPEG plugin overrides Image.draft
        monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", lambda im, mode, size: (drafts.append(size), real(im, mode, size))[1])
        data = rd._render(book, 0, 480)                               # 1600-wide source, 480 bucket
        self._join_ahead()
        with Image.open(io.BytesIO(data)) as im:
            assert im.size[0] == 480 and not im.info.get("progressive")
        assert drafts and drafts[0][0] == 480
        drafts.clear()
        rd._render(book, 0, 1080)                                     # 1600 < 2 x 1080: no draft, full decode
        assert not drafts
