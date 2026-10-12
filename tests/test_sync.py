"""Keyless sync — the keystone of zero-config onboarding. With no Metron, no
Komga, and no LOCG login, sync must still build a series' issue list (from the
anonymous LOCG path) so missing-issue detection and downloads work.
"""
import kometa.db as db
import kometa.sync as sync
from kometa import locg_client
from tests.conftest import make_cbz


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

    def test_reads_listing_description(self):
        # LOCG's live markup — the old selectors are all gone
        html = '<p class="col-12 listing-description">ENTER: GOLDFACE!</p>'
        assert locg_client._parse_issue_details(html)["desc"] == "ENTER: GOLDFACE!"

    def test_login_blurb_never_becomes_a_description(self):
        # The signup CTA is 113 chars and used to win the longest-paragraph
        # fallback against any solicit shorter than that.
        blurb = ("Already have an account with the League of Comic Geeks? "
                 "Let's get you back to tracking and discussing your comics!")
        assert locg_client._parse_issue_details(f"<p>{blurb}</p>")["desc"] == ""

    def test_real_description_wins_over_longer_boilerplate(self):
        blurb = ("Already have an account with the League of Comic Geeks? "
                 "Let's get you back to tracking and discussing your comics!")
        html = (f'<p class="listing-description">Short but real.</p><p>{blurb}</p>')
        assert locg_client._parse_issue_details(html)["desc"] == "Short but real."


def _all_sources_off(monkeypatch, db_path):
    monkeypatch.setattr(sync, "DB_PATH", db_path)
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
            make_cbz(folder / f"Saga #{n:03d}.cbz")

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
        return {"id": 1, "title": "Transmetropolitan", "folder_path": str(folder)}

    def test_tpb_does_not_claim_absolute(self, tmp_path):
        (tmp_path / "Transmetropolitan v01 - Back On the Street.cbz").write_bytes(b"x")
        trades = [
            {"title": "Transmetropolitan Vol. 1: Back on the Street TP", "vol": 1, "vol_range": None},
            {"title": "Absolute Transmetropolitan Vol. 1 HC", "vol": 1, "vol_range": None},
        ]
        out = sync.enrich_trades(self._series(tmp_path), trades)
        assert out[0]["owned"] is True
        assert out[1]["owned"] is False

    def test_absolute_file_claims_only_absolute(self, tmp_path):
        (tmp_path / "Absolute Transmetropolitan v01.cbz").write_bytes(b"x")
        trades = [
            {"title": "Transmetropolitan Vol. 1: Back on the Street TP", "vol": 1, "vol_range": None},
            {"title": "Absolute Transmetropolitan Vol. 1 HC", "vol": 1, "vol_range": None},
        ]
        out = sync.enrich_trades(self._series(tmp_path), trades)
        assert out[0]["owned"] is False
        assert out[1]["owned"] is True

    def test_file_mapping_is_edition_aware_and_carries_no_komga_link(self, tmp_path):
        (tmp_path / "Transmetropolitan v01.cbz").write_bytes(b"x")
        (tmp_path / "Absolute Transmetropolitan v01.cbz").write_bytes(b"x")
        trades = [
            {"title": "Transmetropolitan Vol. 1 TP", "vol": 1, "vol_range": None},
            {"title": "Absolute Transmetropolitan Vol. 1 HC", "vol": 1, "vol_range": None},
        ]
        out = sync.enrich_trades(self._series(tmp_path), trades)
        assert out[0]["file"] == "Transmetropolitan v01.cbz"
        assert out[1]["file"] == "Absolute Transmetropolitan v01.cbz"
        assert all("komga_book_id" not in t for t in out)      # Komga's read link is gone

    def test_vol_range_requires_all_same_edition(self, tmp_path):
        (tmp_path / "Transmetropolitan v01.cbz").write_bytes(b"x")
        (tmp_path / "Absolute Transmetropolitan v02.cbz").write_bytes(b"x")
        trades = [{"title": "Transmetropolitan Vol. 1-2", "vol": None, "vol_range": [1, 2]}]
        out = sync.enrich_trades(self._series(tmp_path), trades)
        # v02 on disk is the Absolute — doesn't complete a plain-edition range
        assert out[0]["owned"] is False


class TestSyncWithoutKomga:
    """Komga is retired (2026-10-12): sync is the folder plus the catalogues and
    nothing else. No Komga client exists to ask; a legacy komga_series_id on the
    row is inert data — not cleared, not relinked, not used."""

    def _setup(self, tmp_path, monkeypatch, komga_id=None, with_folder=True):
        dbp = str(tmp_path / "k.db")
        db.init_db(dbp)
        _all_sources_off(monkeypatch, dbp)
        monkeypatch.setattr(sync, "get_issues_anon", lambda sid: [])
        # the old Komga config rows, as a pre-retirement DB still has them
        db.set_config({"komga_url": "http://komga:8585", "komga_user": "u", "komga_pass": "p",
                       "komga_enabled": "1", "komga_covers": "1"}, dbp)
        import requests
        monkeypatch.setattr(requests.Session, "request",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("network call during sync")))
        folder = tmp_path / "Marvel Comics" / "Black Cat"
        folder.mkdir(parents=True)
        make_cbz(folder / "Black Cat #001.cbz")
        make_cbz(folder / "Black Cat #002.cbz")
        sid = db.add_series(komga_series_id=komga_id, title="Black Cat", publisher="Marvel",
                            folder_path=str(folder) if with_folder else None, path=dbp)
        return dbp, sid

    def test_ownership_comes_from_the_folder_alone(self, tmp_path, monkeypatch):
        dbp, sid = self._setup(tmp_path, monkeypatch)
        sync.sync_one(db.get_series_by_id(sid, dbp))
        owned = sorted(i["number"] for i in db.get_issues_for_series(sid, dbp) if i["owned"])
        assert owned == [1.0, 2.0]
        assert db.get_series_by_id(sid, dbp)["komga_series_id"] is None   # nothing links it

    def test_a_legacy_komga_id_is_left_as_data(self, tmp_path, monkeypatch):
        dbp, sid = self._setup(tmp_path, monkeypatch, komga_id="OLDID")
        sync.sync_one(db.get_series_by_id(sid, dbp))
        assert db.get_series_by_id(sid, dbp)["komga_series_id"] == "OLDID"
        assert len([i for i in db.get_issues_for_series(sid, dbp) if i["owned"]]) == 2

    def test_no_folder_is_not_looked_up_anywhere(self, tmp_path, monkeypatch):
        # Komga used to be asked for a folder; now a folder-less row just owns nothing
        dbp, sid = self._setup(tmp_path, monkeypatch, komga_id="OLDID", with_folder=False)
        sync.sync_one(db.get_series_by_id(sid, dbp))
        assert db.get_series_by_id(sid, dbp)["folder_path"] is None
        assert not any(i["owned"] for i in db.get_issues_for_series(sid, dbp))

    def test_komga_is_gone_from_the_code(self):
        import importlib.util
        from kometa import sources
        assert not hasattr(sources, "komga") and not hasattr(sync, "_komga")
        for mod in ("kometa.komga_client", "kometa.komga_import", "kometa.harvest_komga"):
            assert importlib.util.find_spec(mod) is None


def test_a_no_volume_edition_on_disk_matches_its_trade_whatever_year_the_file_carries(tmp_path):
    """'DIE HC' on LOCG, 'DIE HC (2022).cbz' on the shelf: the same book."""
    from kometa.sync import enrich_trades
    (tmp_path / "DIE HC (2022).cbz").write_bytes(b"PK\x03\x04")
    trades = [{"title": "DIE HC", "vol": None}, {"title": "DIE Vol. 1: Fantasy Heartbreaker TP", "vol": 1}]
    enrich_trades({"folder_path": str(tmp_path), "title": "Die"}, trades)
    assert [t["owned"] for t in trades] == [True, False]


def test_a_no_volume_edition_matches_loosely_when_the_words_agree_and_never_steals(tmp_path):
    """LOCG: 'Batman Year 100 Deluxe Edition HC'. Disk: 'Batman - Year 100 and Other
    Tales Deluxe Edition (2015).cbz'. Same book. The plain TP must not claim it
    (Deluxe ≠ plain), a single issue is never a trade, and a file goes to one trade."""
    from kometa.sync import enrich_trades
    for f in ("Batman - Year 100 and Other Tales Deluxe Edition (2015).cbz", "Batman - Year 100 #001 (2006).cbz"):
        (tmp_path / f).write_bytes(b"PK\x03\x04")
    trades = [{"title": "Batman: Year 100 TP", "vol": None}, {"title": "Batman: Year 100 TP2013 Edition", "vol": None},
              {"title": "Batman Year 100 Deluxe Edition HC", "vol": None}]
    enrich_trades({"folder_path": str(tmp_path), "title": "Batman - Year 100"}, trades)
    assert [t["owned"] for t in trades] == [False, False, True]
    assert trades[2]["file"].startswith("Batman - Year 100 and Other Tales")
    (tmp_path / "Heavy Liquid - Collected Edition (2019).cbz").write_bytes(b"PK\x03\x04")
    hl = [{"title": "Heavy Liquid TP", "vol": None}, {"title": "Heavy Liquid HC", "vol": None}]
    enrich_trades({"folder_path": str(tmp_path), "title": "Heavy Liquid"}, hl)
    assert [t["owned"] for t in hl] == [True, False]                       # one file, one trade


def test_trade_refresh_also_reconciles_single_issues_from_a_pack(tmp_path, db_path, monkeypatch):
    """A complete-run pack lands as singles: the trade row completes, and the
    issues must flip to owned right then, not at the next sync."""
    import kometa.sync as sync
    from tests.conftest import make_cbz
    monkeypatch.setattr(sync, "DB_PATH", db_path)
    folder = tmp_path / "Faithless"; folder.mkdir()
    sid = db.add_series(title="Faithless", publisher="Boom! Studios", folder_path=str(folder), on_pull_list=True, path=db_path)
    for n in (1, 2):
        db.upsert_issue_status(sid, float(n), "2019-04-01", 0, path=db_path)
        make_cbz(folder / f"Faithless #{n:03d}.cbz")
    db.set_trades(sid, [{"title": "Faithless TP", "vol": None, "owned": False, "file": None}], db_path)
    sync.refresh_trades_owned(sid)
    assert [i["owned"] for i in db.get_issues_for_series(sid, db_path)] == [1, 1]


def test_a_scene_tagged_one_word_trade_file_matches_its_run(tmp_path):
    """'100% TPB (2020) (Digital) (Mephisto-Empire).cbz' on a shelf called 100%:
    the tags are packaging and the one word IS the series, so the TP claims it."""
    from kometa.sync import enrich_trades
    (tmp_path / "100% TPB (2020) (Digital) (Mephisto-Empire).cbz").write_bytes(b"PK\x03\x04")
    trades = [{"title": "100% TP", "vol": None}, {"title": "100% HC", "vol": None}]
    enrich_trades({"folder_path": str(tmp_path), "title": "100%"}, trades)
    assert [t["owned"] for t in trades] == [True, False]
    # but one word that is NOT the series name still can't claim anything
    (tmp_path / "Batman Lost (2018).cbz").write_bytes(b"PK\x03\x04")
    other = [{"title": "Batman TP", "vol": None}]
    enrich_trades({"folder_path": str(tmp_path), "title": "Batman - Lost"}, other)
    assert other[0]["owned"] is False


def test_one_vol_file_makes_one_edition_owned_not_every_edition_with_that_vol(tmp_path):
    """Avengers Academy: the Complete Collection Vol. 2 TP and the Vol. 2 HC both
    read 'owned' off the one Complete Collection file (volume number alone). The
    file goes to the edition whose title fits it; the HC stays unowned."""
    import kometa.sync as sync
    (tmp_path / "Avengers Academy - The Complete Collection Vol. 02 (2018).cbz").write_bytes(b"x")
    series = {"title": "Avengers Academy", "folder_path": str(tmp_path)}
    hc = {"title": "Avengers Academy Vol. 2: Will We Use This in the Real World? HC", "vol": 2, "locg_id": "1485878"}
    cc = {"title": "Avengers Academy: The Complete Collection Vol. 2 TP", "vol": 2, "locg_id": "5484155"}
    sync.enrich_trades(series, [hc, cc])
    assert (hc["owned"], hc["file"]) == (False, None)
    assert cc["owned"] is True and cc["file"] == "Avengers Academy - The Complete Collection Vol. 02 (2018).cbz"
    # a bare 'Vol 02' says nothing about the edition: the one Kometa downloaded it AS wins
    for p in tmp_path.iterdir():
        p.unlink()
    (tmp_path / "Avengers Academy Vol 02.cbz").write_bytes(b"x")
    sync.enrich_trades(series, [cc, hc], landed={"avengers academy vol 02.cbz": "1485878"})
    assert hc["owned"] is True and cc["owned"] is False
    # two files, two editions: each owns its own
    (tmp_path / "Avengers Academy - The Complete Collection Vol. 02.cbz").write_bytes(b"x")
    sync.enrich_trades(series, [hc, cc])
    assert hc["file"] == "Avengers Academy Vol 02.cbz"
    assert cc["file"] == "Avengers Academy - The Complete Collection Vol. 02.cbz"


def test_refresh_trades_owned_hands_the_landed_file_to_its_edition(tmp_path, db_path, monkeypatch):
    import json as _json
    import kometa.sync as sync
    monkeypatch.setattr(sync, "DB_PATH", db_path)
    folder = tmp_path / "Avengers Academy"; folder.mkdir()
    sid = db.add_series(title="Avengers Academy", publisher="Marvel", folder_path=str(folder), on_pull_list=False, path=db_path)
    book = folder / "Avengers Academy Vol 02.cbz"; book.write_bytes(b"x")
    db.set_trades(sid, [{"title": "Avengers Academy: The Complete Collection Vol. 2 TP", "vol": 2, "locg_id": "5484155"},
                        {"title": "Avengers Academy Vol. 2: Will We Use This in the Real World? HC", "vol": 2, "locg_id": "1485878"}],
                  db_path)
    with db._connect(db_path) as c:
        qid = c.execute("INSERT INTO download_queue (tracked_series_id, kind, locg_id, meta_json, state) "
                        "VALUES (?, 'trade', '1485878', ?, 'queued')", (sid, _json.dumps({"vol": 2}))).lastrowid
    db.complete_trade(qid, db_path, placed=[str(book)])
    sync.refresh_trades_owned(sid)
    assert [t["locg_id"] for t in db.get_trades(sid, db_path)["trades"] if t["owned"]] == ["1485878"]
