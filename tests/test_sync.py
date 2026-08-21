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
            {"role": "Writer", "name": "Scott Snyder", "people_id": "179", "people_slug": "scott-snyder"},
            {"role": "Artist", "name": "Nick Dragotta", "people_id": "876", "people_slug": "nick-dragotta"},
        ]

    def test_no_creators_yields_empty_credits(self):
        d = locg_client._parse_issue_details('<div class="copy">Just a synopsis.</div>')
        assert d["desc"] == "Just a synopsis."
        assert d["credits"] == []


def _all_sources_off(monkeypatch, db_path):
    monkeypatch.setattr(sync, "DB_PATH", db_path)
    monkeypatch.setattr(sync, "_komga", lambda: None)
    # LOCG is keyless now. Stub the title→id resolver to None so a series
    # without a seeded locg_series_id doesn't reach out to the live site;
    # tests that DO seed one go straight through get_issues_anon.
    monkeypatch.setattr(sync, "find_series_id_anon", lambda *a, **k: None)


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

        sid = db.add_series(komga_series_id=None, title="Saga",
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

        sid = db.add_series(komga_series_id=None, title="Saga",
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

        sid = db.add_series(komga_series_id=None, title="Mystery",
                            publisher="Image", path=dbp)  # no locg_series_id
        sync.sync_one(db.get_series_by_id(sid, dbp))

        assert db.get_issues_for_series(sid, dbp) == []


class TestLastScheduledSyncSlot:
    def test_most_recent_slot_is_utc_recent_and_parseable(self):
        from datetime import datetime, timezone, timedelta
        from kometa.scheduler import last_scheduled_sync_utc
        s = last_scheduled_sync_utc()
        slot = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        # The most recent of three daily slots is never in the future and never
        # more than ~24h stale (the widest gap between slots is 5pm -> 5am + tz).
        assert slot <= now
        assert now - slot < timedelta(hours=24)

    def test_string_compare_semantics(self):
        # The lifespan catch-up does a plain string compare against the
        # last_full_sync stamp — both sides must be %Y-%m-%d %H:%M:%S UTC.
        from kometa.scheduler import last_scheduled_sync_utc
        s = last_scheduled_sync_utc()
        assert "" < s          # missing stamp always reads as "missed"
        assert s < "9999-01-01 00:00:00"


class TestEmptyRootGuard:
    """The dead-mount tripwire. /comics is an SMB share; a container that beats
    the mounter after a reboot sees an empty dir where the library lives. The
    full-sync job must refuse to scan/sweep against that void."""

    def _wire(self, monkeypatch, tmp_path, root):
        import kometa.main as main
        dbp = str(tmp_path / "kometa_test.db")
        db.init_db(dbp)
        monkeypatch.setattr(main, "DB_PATH", dbp)
        monkeypatch.setattr(main, "_comics_root", lambda: str(root))
        synced = []
        monkeypatch.setattr(main, "sync_one_guarded", lambda s, fn: synced.append(s) or True)
        monkeypatch.setattr(main.db, "get_all_series", lambda p: [{"id": 1, "title": "Saga"}])
        swept = []
        monkeypatch.setattr(main, "_sweep_missing", lambda: swept.append(True))
        return main, synced, swept

    def test_empty_root_refuses_to_sync_or_sweep(self, tmp_path, monkeypatch):
        root = tmp_path / "comics"
        root.mkdir()  # exists, readable, EMPTY — the unmounted-share signature
        main, synced, swept = self._wire(monkeypatch, tmp_path, root)
        main._sync_all_job()
        assert synced == [] and swept == []

    def test_missing_root_refuses_too(self, tmp_path, monkeypatch):
        main, synced, swept = self._wire(monkeypatch, tmp_path, tmp_path / "nope")
        main._sync_all_job()
        assert synced == [] and swept == []

    def test_refusal_leaves_last_full_sync_unstamped(self, tmp_path, monkeypatch):
        # The stamp is the catch-up's memory: an aborted run must still read
        # as "missed" so the next startup or cron slot retries it.
        root = tmp_path / "comics"
        root.mkdir()
        main, _, _ = self._wire(monkeypatch, tmp_path, root)
        main._sync_all_job()
        assert "last_full_sync" not in db.get_config(main.DB_PATH)

    def test_populated_root_syncs_normally(self, tmp_path, monkeypatch):
        root = tmp_path / "comics"
        (root / "Image" / "Saga").mkdir(parents=True)
        main, synced, swept = self._wire(monkeypatch, tmp_path, root)
        main._sync_all_job()
        assert len(synced) == 1 and swept == [True]
        assert db.get_config(main.DB_PATH).get("last_full_sync")


class TestEnrichTradesEditions:
    """Ownership is edition-aware: a plain Vol 1 TPB on disk must NOT stamp
    'Absolute Vol. 1 HC' owned (that false tick made the sweep skip the real
    book forever — the Transmetropolitan incident, 2026-08-21)."""

    def _series(self, folder):
        return {"id": 1, "title": "Transmetropolitan", "folder_path": str(folder), "komga_series_id": None}

    def test_tpb_does_not_claim_absolute(self, tmp_path):
        (tmp_path / "Transmetropolitan v01 - Back On the Street.cbz").write_bytes(b"x")
        trades = [
            {"title": "Transmetropolitan Vol. 1: Back on the Street TP", "vol": 1, "vol_range": None},
            {"title": "Absolute Transmetropolitan Vol. 1 HC", "vol": 1, "vol_range": None},
        ]
        out = sync.enrich_trades(self._series(tmp_path), trades, books=[])
        assert out[0]["owned"] is True
        assert out[1]["owned"] is False

    def test_absolute_file_claims_only_absolute(self, tmp_path):
        (tmp_path / "Absolute Transmetropolitan v01.cbz").write_bytes(b"x")
        trades = [
            {"title": "Transmetropolitan Vol. 1: Back on the Street TP", "vol": 1, "vol_range": None},
            {"title": "Absolute Transmetropolitan Vol. 1 HC", "vol": 1, "vol_range": None},
        ]
        out = sync.enrich_trades(self._series(tmp_path), trades, books=[])
        assert out[0]["owned"] is False
        assert out[1]["owned"] is True

    def test_komga_book_mapping_is_edition_aware(self, tmp_path):
        (tmp_path / "Transmetropolitan v01.cbz").write_bytes(b"x")
        (tmp_path / "Absolute Transmetropolitan v01.cbz").write_bytes(b"x")
        books = [
            {"id": "TPB1", "name": "Transmetropolitan v01"},
            {"id": "ABS1", "name": "Absolute Transmetropolitan v01"},
        ]
        trades = [
            {"title": "Transmetropolitan Vol. 1 TP", "vol": 1, "vol_range": None},
            {"title": "Absolute Transmetropolitan Vol. 1 HC", "vol": 1, "vol_range": None},
        ]
        out = sync.enrich_trades(self._series(tmp_path), trades, books=books)
        assert out[0]["komga_book_id"] == "TPB1"
        assert out[1]["komga_book_id"] == "ABS1"

    def test_vol_range_requires_all_same_edition(self, tmp_path):
        (tmp_path / "Transmetropolitan v01.cbz").write_bytes(b"x")
        (tmp_path / "Absolute Transmetropolitan v02.cbz").write_bytes(b"x")
        trades = [{"title": "Transmetropolitan Vol. 1-2", "vol": None, "vol_range": [1, 2]}]
        out = sync.enrich_trades(self._series(tmp_path), trades, books=[])
        # v02 on disk is the Absolute — doesn't complete a plain-edition range
        assert out[0]["owned"] is False
