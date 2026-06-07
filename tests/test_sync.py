"""Keyless sync — the keystone of zero-config onboarding. With no Metron, no
Komga, and no LOCG login, sync must still build a series' issue list (from the
anonymous LOCG path) so missing-issue detection and downloads work.
"""
import kometa.db as db
import kometa.sync as sync
from kometa import locg_client


class _FakeResp:
    def __init__(self, payload):
        self._p = payload
    def json(self):
        return self._p


class TestGetIssuesParser:
    """The shared scraper logic, driven by a fake getter — no network."""

    def test_parses_number_date_and_cover_id(self):
        html = (
            '<li>'
            '<span class="title">Saga #1</span>'
            '<span class="date" data-date="1577836800"></span>'  # 2020-01-01 UTC
            '<img data-src="https://s3.amazonaws.com/comicgeeks/comics/covers/medium-555.jpg">'
            '</li>'
        )
        issues = locg_client._get_issues_with_get(100002, lambda url, **kw: _FakeResp({"list": html}))
        assert issues == [{
            "number": 1.0, "store_date": "2020-01-01",
            "cover": "https://s3.amazonaws.com/comicgeeks/comics/covers/medium-555.jpg",
            "locg_issue_id": "555",
        }]

    def test_skips_rows_without_a_parseable_number(self):
        html = '<li><span class="title">Some Trade Paperback</span></li>'
        issues = locg_client._get_issues_with_get(1, lambda url, **kw: _FakeResp({"list": html}))
        assert issues == []


class TestSearchParser:
    """LOCG search results carry the series cover — extract it so the wizard shows it."""

    def test_extracts_cover_title_publisher_year(self):
        html = (
            '<li class="media">'
            '<a href="/comics/series/100002/saga"></a>'
            '<div class="title">Saga</div>'
            '<div class="publisher">Image Comics</div>'
            '<div class="date">2012</div>'
            '<img src="https://s3.amazonaws.com/comicgeeks/comics/covers/medium-555.jpg">'
            '</li>'
        )
        r = locg_client._parse_search_html(html)[0]
        assert r == {
            "id": 100002, "title": "Saga", "publisher": "Image Comics", "year": 2012,
            "cover": "https://s3.amazonaws.com/comicgeeks/comics/covers/medium-555.jpg",
        }

    def test_drops_non_cover_placeholder_images(self):
        html = (
            '<li class="media">'
            '<a href="/comics/series/1/x"></a>'
            '<div class="title">X</div>'
            '<img src="/assets/spacer.gif">'
            '</li>'
        )
        assert locg_client._parse_search_html(html)[0]["cover"] is None

    def test_prefers_lazy_data_src(self):
        html = (
            '<li class="media">'
            '<a href="/comics/series/2/y"></a>'
            '<div class="title">Y</div>'
            '<img src="/assets/spacer.gif" data-src="https://s3.amazonaws.com/comicgeeks/comics/covers/medium-9.jpg">'
            '</li>'
        )
        assert locg_client._parse_search_html(html)[0]["cover"].endswith("medium-9.jpg")


class TestIssueDetailsParser:
    """Issue desc + credits-with-roles from a LOCG issue page (keyless Details +
    the creator signal for recommendations)."""

    def test_parses_desc_and_roled_credits(self):
        html = (
            '<div class="copy">A bold reimagining of the Dark Knight.</div>'
            '<div class="d-flex flex-column">'
            '  <div class="role">Writer</div>'
            '  <div class="name"><a href="/people/179/scott-snyder">Scott Snyder</a></div>'
            '</div>'
            '<div class="d-flex flex-column">'
            '  <div class="role">Artist</div>'
            '  <div class="name"><a href="/people/876/nick-dragotta">Nick Dragotta</a></div>'
            '</div>'
        )
        d = locg_client._parse_issue_details(html)
        assert d["desc"] == "A bold reimagining of the Dark Knight."
        assert d["credits"] == [
            {"role": "Writer", "name": "Scott Snyder", "people_id": "179"},
            {"role": "Artist", "name": "Nick Dragotta", "people_id": "876"},
        ]

    def test_no_creators_yields_empty_credits(self):
        d = locg_client._parse_issue_details('<div class="copy">Just a synopsis.</div>')
        assert d["desc"] == "Just a synopsis."
        assert d["credits"] == []


def _all_sources_off(monkeypatch, db_path):
    monkeypatch.setattr(sync, "DB_PATH", db_path)
    monkeypatch.setattr(sync, "_komga", lambda: None)
    monkeypatch.setattr(sync, "_metron", lambda: None)
    monkeypatch.setattr(sync, "_comicvine", lambda: None)
    monkeypatch.setattr(sync, "_locg", lambda: None)   # no LOCG creds


class TestKeylessSync:
    def test_builds_issue_list_from_locg_anon(self, tmp_path, monkeypatch):
        dbp = str(tmp_path / "k.db")
        db.init_db(dbp)
        _all_sources_off(monkeypatch, dbp)
        # anon issue list returns 1,2,3 — no login involved
        monkeypatch.setattr(sync, "get_issues_anon", lambda sid: [
            {"number": float(n), "store_date": "2020-01-01", "cover": f"c{n}", "locg_issue_id": str(n)}
            for n in (1, 2, 3)
        ])

        sid = db.add_series(komga_series_id=None, metron_series_id=None, title="Saga",
                            publisher="Image", locg_series_id=100002, path=dbp)
        sync.sync_one(db.get_series_by_id(sid, dbp))

        issues = db.get_issues_for_series(sid, dbp)
        assert sorted(i["number"] for i in issues) == [1.0, 2.0, 3.0]
        # nothing on disk -> everything is missing, ready to queue
        assert all(i["owned"] == 0 for i in issues)

    def test_owned_on_disk_are_marked_not_missing(self, tmp_path, monkeypatch):
        dbp = str(tmp_path / "k.db")
        db.init_db(dbp)
        _all_sources_off(monkeypatch, dbp)
        monkeypatch.setattr(sync, "get_issues_anon", lambda sid: [
            {"number": float(n), "store_date": "2020-01-01", "cover": f"c{n}", "locg_issue_id": str(n)}
            for n in (1, 2, 3)
        ])
        # issues 1 and 2 already on disk
        folder = tmp_path / "Image Comics" / "Saga"
        folder.mkdir(parents=True)
        for n in (1, 2):
            (folder / f"Saga #{n:03d}.cbz").write_bytes(b"PK\x03\x04")

        sid = db.add_series(komga_series_id=None, metron_series_id=None, title="Saga",
                            publisher="Image", folder_path=str(folder),
                            locg_series_id=100002, path=dbp)
        sync.sync_one(db.get_series_by_id(sid, dbp))

        owned = {i["number"] for i in db.get_issues_for_series(sid, dbp) if i["owned"]}
        missing = {i["number"] for i in db.get_issues_for_series(sid, dbp) if not i["owned"]}
        assert owned == {1.0, 2.0}
        assert missing == {3.0}

    def test_no_locg_id_and_no_creds_yields_no_issues_without_crashing(self, tmp_path, monkeypatch):
        dbp = str(tmp_path / "k.db")
        db.init_db(dbp)
        _all_sources_off(monkeypatch, dbp)
        # if anon were called it'd explode — proves it is NOT called without an id
        monkeypatch.setattr(sync, "get_issues_anon",
                            lambda sid: (_ for _ in ()).throw(AssertionError("should not run")))

        sid = db.add_series(komga_series_id=None, metron_series_id=None, title="Mystery",
                            publisher="Image", path=dbp)  # no locg_series_id
        sync.sync_one(db.get_series_by_id(sid, dbp))

        assert db.get_issues_for_series(sid, dbp) == []
