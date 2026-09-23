"""Chunked GetComics pack posts — the Curse Words case.

GetComics never posted Curse Words #2-7 on their own. They live inside one post,
'Curse Words #1 – 25 + TPBs (2017-2019)', split into four ranged downloads. The
singles matcher rejected the post ('tpbs' reads as a spinoff), the extractor would
have grabbed the #1-5 chunk no matter which issue was asked for, and the
downloader ran single-issue checks on the zip wrapper before opening it.
"""
import io
import zipfile

import pytest
from bs4 import BeautifulSoup

from kometa import getcomics_client as gc
from kometa.getcomics_client import _issue_pack_post_covers, _chunk_links, _normalize

CW = _normalize("Curse Words")
PACK = "Curse Words #1 – 25 + TPBs (2017-2019)"

POST_HTML = """
<html><body><section class="post-contents">
<p>Curse Words #1 – 25 + TPBs (2017-2019) : The new ongoing series...</p>
<p><strong>Curse Words #1 – 25 + TPBs</strong> Language : English | Year : 2017-2019 | Size : 2.1 GB</p>
<ul>
<li>Curse Words #1 – 5 (2017) (479 MB)<strong> :</strong><br/><strong>
  <a href="https://getcomics.org/dls/chunk-1-5"><span>Main Server</span></a> |
  <a href="https://cloud.mail.ru/x"><span>CloudMail</span></a></strong></li>
<li>Curse Words #6 – 10 (2017) (555 MB)<strong> :</strong><br/><strong>
  <a href="https://getcomics.org/dls/chunk-6-10"><span>Main Server</span></a></strong></li>
<li>Curse Words #11 – 18 (2018) (489 MB)<strong> :</strong><br/><strong>
  <a href="https://getcomics.org/dls/chunk-11-18"><span>Main Server</span></a></strong></li>
<li>Curse Words #19 – 25 (2019) (419 MB)<strong> :</strong><br/><strong>
  <a href="https://getcomics.org/dls/chunk-19-25"><span>Main Server</span></a></strong></li>
<li>Curse Words Vol. 1 – The Devil's Devil (TPB) (2017) : <a href="https://getcomics.org/tpb1">Go to this post</a></li>
</ul>
</section></body></html>
"""


def _search_html(*titles):
    arts = "".join(
        f'<article class="post"><h1 class="post-title"><a href="https://getcomics.org/p/{i}">{t}</a></h1></article>'
        for i, t in enumerate(titles))
    return f"<html><body>{arts}</body></html>"


class _Resp:
    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        pass


class TestIssuePackPostCovers:
    def test_the_curse_words_pack_covers_a_middle_issue(self):
        assert _issue_pack_post_covers(CW, PACK, 2.0, series_year=2017)
        assert _issue_pack_post_covers(CW, PACK, 7.0, series_year=2017)

    def test_out_of_range_rejected(self):
        assert not _issue_pack_post_covers(CW, "Curse Words #1 – 8 (2017)", 12.0, series_year=2017)

    def test_different_run_by_year_rejected(self):
        assert not _issue_pack_post_covers(CW, "Curse Words #1 – 10 (2024)", 2.0, series_year=2017)

    def test_spinoff_still_rejected(self):
        assert not _issue_pack_post_covers(
            CW, "Curse Words Spring Has Sprung Special #1 – 2 (2019)", 2.0, series_year=2017)

    def test_single_issue_post_is_not_a_pack(self):
        assert not _issue_pack_post_covers(CW, "Curse Words #2 (2017)", 2.0, series_year=2017)

    def test_trade_volume_range_is_not_an_issue_range(self):
        assert not _issue_pack_post_covers(CW, "Curse Words Vol. 1 – 5 (TPB) (2017-2020)", 2.0,
                                           series_year=2017)

    def test_volume_ordinal_is_packaging(self):
        na = _normalize("New Avengers")
        assert _issue_pack_post_covers(na, "New Avengers Vol. 3 #1 – 33 + Extras (2013-2015)",
                                       14.0, series_year=2013)


class TestChunkLinks:
    def test_reads_each_ranged_line_and_skips_the_title_and_trade_lines(self):
        body = BeautifulSoup(POST_HTML, "lxml").find("section")
        assert _chunk_links(body) == [
            (1, 5, "https://getcomics.org/dls/chunk-1-5"),
            (6, 10, "https://getcomics.org/dls/chunk-6-10"),
            (11, 18, "https://getcomics.org/dls/chunk-11-18"),
            (19, 25, "https://getcomics.org/dls/chunk-19-25"),
        ]

    def test_ordinary_post_has_no_chunks(self):
        html = ('<section class="post-contents"><p>Language : English</p>'
                '<div class="aio-button-center"><a href="https://getcomics.org/dls/one">Download Now</a></div>'
                '</section>')
        assert _chunk_links(BeautifulSoup(html, "lxml").find("section")) == []


class TestExtractDownloadChunks:
    @pytest.fixture
    def client(self, monkeypatch):
        c = gc.GetComicsClient()
        monkeypatch.setattr(c, "_get", lambda url, **kw: _Resp(POST_HTML))
        return c

    @pytest.mark.parametrize("num,want", [(2.0, "chunk-1-5"), (7.0, "chunk-6-10"),
                                          (18.0, "chunk-11-18"), (25.0, "chunk-19-25")])
    def test_picks_the_chunk_that_covers_the_issue(self, client, num, want):
        url, _ = client._extract_download("https://getcomics.org/p/0", issue_number=num)
        assert url.endswith(want)

    def test_no_covering_chunk_means_nothing(self, client):
        assert client._extract_download("https://getcomics.org/p/0", issue_number=40.0) == (None, None)

    def test_whole_run_pack_path_refuses_a_chunked_post(self, client):
        assert client._extract_download("https://getcomics.org/p/0", allow_chunked=False) == (None, None)


class TestSearchFallsBackToPackPost:
    def test_issue_with_no_single_post_is_found_inside_the_pack(self, monkeypatch):
        c = gc.GetComicsClient()
        search = _search_html(
            "Curse Words – The Hole Damned Thing Omnibus (2022)",
            PACK,
            "Curse Words #25 (2019)",
            "Curse Words Spring Has Sprung Special #1 (2019)",
        )

        def fake_get(url, **kw):
            return _Resp(search if "params" in kw else POST_HTML)

        monkeypatch.setattr(c, "_get", fake_get)
        url, _ = c.search("Curse Words", 7.0, store_date="2017-08-16", series_year=2017)
        assert url == "https://getcomics.org/dls/chunk-6-10"

    def test_a_real_single_post_still_wins_over_the_pack(self, monkeypatch):
        c = gc.GetComicsClient()
        search = _search_html(PACK, "Curse Words #25 (2019)")
        single = ('<section class="post-contents"><p>Language : English</p>'
                  '<div class="aio-button-center"><a href="https://getcomics.org/dls/single-25">'
                  'Download Now</a></div></section>')

        def fake_get(url, **kw):
            if "params" in kw:
                return _Resp(search)
            return _Resp(single if url.endswith("/1") else POST_HTML)

        monkeypatch.setattr(c, "_get", fake_get)
        url, _ = c.search("Curse Words", 25.0, store_date="2019-10-02", series_year=2017)
        assert url == "https://getcomics.org/dls/single-25"


# ---------------------------------------------------------------- downloader


def _comic_bytes(number, pages=6):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(pages):
            z.writestr(f"p{i:03d}.jpg", b"\xff\xd8\xff\xe0" + b"x" * 400)
        z.writestr("ComicInfo.xml", f"<ComicInfo><Series>Curse Words</Series><Number>{number}</Number></ComicInfo>")
    return buf.getvalue()


def _chunk_zip(numbers):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in numbers:
            z.writestr(f"Curse Words 00{n} (2017) (digital) (Son of Ultron-Empire).cbz",
                       _comic_bytes(n))
    return buf.getvalue()


class _DlResp:
    def __init__(self, body):
        self.body = body
        self.headers = {"content-length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]


@pytest.fixture
def library(tmp_path, monkeypatch):
    from kometa import downloader
    staging = tmp_path / "staging"
    folder = tmp_path / "comics" / "Image Comics" / "Curse Words"
    folder.mkdir(parents=True)
    monkeypatch.setenv("KOMETA_DOWNLOADS", str(staging))
    monkeypatch.setattr(downloader, "_remote_content_length", lambda url: None)
    return folder


def _download(monkeypatch, folder, body, number):
    from kometa import downloader
    monkeypatch.setattr(downloader, "_get_with_retries", lambda url, label: _DlResp(body))
    return downloader.download_issue(
        url="https://getcomics.org/dls/chunk-1-5", title="Curse Words", publisher="Image Comics",
        issue_number=number, store_date="2017-02-22", hint_filename=None,
        komga_scan_fn=lambda: None, dest_dir=str(folder))


class TestDownloadIssueFromChunk:
    def test_pulls_the_issue_out_and_shelves_the_rest(self, library, monkeypatch):
        (library / "Curse Words #001.cbz").write_bytes(_comic_bytes(1))
        dest = _download(monkeypatch, library, _chunk_zip([1, 2, 3, 4, 5]), 2.0)
        assert dest.endswith("Curse Words #002.cbz")
        names = sorted(p.name for p in library.iterdir())
        assert names == [f"Curse Words #00{n}.cbz" for n in range(1, 6)]

    def test_second_grab_of_the_same_chunk_returns_the_shelved_issue(self, library, monkeypatch):
        _download(monkeypatch, library, _chunk_zip([1, 2, 3, 4, 5]), 2.0)
        dest = _download(monkeypatch, library, _chunk_zip([1, 2, 3, 4, 5]), 3.0)
        assert dest.endswith("Curse Words #003.cbz")
        # The wrapper never gets filed as a comic.
        assert not any(p.suffix == ".zip" or "001-005" in p.name for p in library.iterdir())

    def test_chunk_without_the_issue_is_wrong_issue(self, library, monkeypatch):
        from kometa.downloader import WrongIssueError
        with pytest.raises(WrongIssueError, match="did not contain"):
            _download(monkeypatch, library, _chunk_zip([6, 7, 8]), 2.0)


class TestBatchDedupeShelfCheck:
    def test_second_row_on_the_same_chunk_completes_from_the_shelf(self, db_path, tmp_path, monkeypatch):
        import kometa.acquisition as acq
        import kometa.db as db
        monkeypatch.setattr(acq, "DB_PATH", db_path)
        monkeypatch.setattr(acq, "_resync_after_placement", lambda *a, **k: None)
        folder = tmp_path / "Curse Words"
        folder.mkdir()
        (folder / "Curse Words #003.cbz").write_bytes(_comic_bytes(3))
        sid = db.add_series(komga_series_id=None, title="Curse Words", publisher="Image Comics",
                            year_began=2017, folder_path=str(folder), on_pull_list=True, path=db_path)
        db.queue_issue(sid, 3.0, db_path)
        item = next(q for q in db.get_queue(path=db_path) if q["issue_number"] == 3.0)
        item = {**item, "title": "Curse Words", "publisher": "Image Comics",
                "folder_path": str(folder), "year_began": 2017}

        class FakeGC:
            def search(self, *a, **kw):
                return "https://getcomics.org/dls/chunk-1-5", None

        handled, err = acq._try_getcomics(item, item["id"], FakeGC(),
                                          {"https://getcomics.org/dls/chunk-1-5"}, "2017-03-22")
        assert (handled, err) == (True, None)
        row = next(q for q in db.get_queue(path=db_path) if q["id"] == item["id"])
        assert row["state"] == "done"
