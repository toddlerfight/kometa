"""Every series is a Kometa series (docs/reader-spec.md, 2026-10-08): import,
confident-only LOCG matching, pull list as a switch, weekly cadence."""
from datetime import datetime, timedelta, timezone

import pytest

import kometa.db as db
import kometa.main as main
import kometa.shelf as sh
import kometa.shelf_import as si
import kometa.sync as sync
from tests.test_reader import make_book


@pytest.fixture
def shelf(tmp_path, db_path, monkeypatch):
    for mod in (sh, si, sync, main):
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    root = tmp_path / "comics"
    for pub, ser, files in [("Marvel Comics", "Hawkeye", ["Hawkeye #001 (2016).cbz", "Hawkeye #002 (2017).cbz"]),
                            ("DC Comics", "Batman - Knightfall", ["Batman - Knightfall Omnibus.cbz"])]:
        d = root / pub / ser
        d.mkdir(parents=True)
        for f in files:
            make_book(d / f, [("p1.jpg", 600, 900)])
    sh.scan_shelf(str(root))
    return root


def _row(id_, title, pub, year, comic=False):
    return {"id": id_, "title": title, "publisher": pub, "year": year, **({"comic": True} if comic else {})}


class TestImport:
    def test_folders_become_series_pull_off_pending_with_owned_from_disk(self, shelf):
        ids = si.import_new_folders()
        assert len(ids) == 2
        hk = next(s for s in db.get_all_series(si.DB_PATH) if s["title"] == "Hawkeye")
        assert hk["on_pull_list"] == 0 and hk["match_status"] == "pending"
        assert hk["year_began"] is None                      # folder carries no year
        owned = {i["number"] for i in db.get_issues_for_series(hk["id"], si.DB_PATH) if i["owned"]}
        assert owned == {1.0, 2.0}
        assert sh.list_shelf()["series"] == []               # nothing left untracked
        assert si.import_new_folders() == []                  # idempotent

    def test_duplicate_folder_never_mints_a_twin_series(self, shelf):
        d = shelf / "Mirage" / "Hawkeye"                     # same series, second folder
        d.mkdir(parents=True)
        make_book(d / "Hawkeye #003.cbz", [("p1.jpg", 600, 900)])
        q = shelf / "_misgrabbed" / "Saga"                  # quarantine: not the shelf at all
        q.mkdir(parents=True)
        make_book(q / "Saga #001.cbz", [("p1.jpg", 600, 900)])
        sh.scan_shelf(str(shelf))
        si.import_new_folders()
        titles = [s["title"] for s in db.get_all_series(si.DB_PATH)]
        assert titles.count("Hawkeye") == 1 and "Saga" not in titles
        left = [s["folder_path"] for s in sh.list_shelf()["series"]]
        assert left == [str(d)]                              # still on the shelf, readable

    def test_leading_dash_scar_is_cleaned(self, shelf):
        d = shelf / "DC Comics" / "- Batman - Lost"
        d.mkdir()
        make_book(d / "Batman - Lost #001.cbz", [("p1.jpg", 600, 900)])
        sh.scan_shelf(str(shelf))
        si.import_new_folders()
        assert "Batman - Lost" in {s["title"] for s in db.get_all_series(si.DB_PATH)}


class TestConfidentMatch:
    def _series(self, shelf, title, pub="Marvel Comics", folder="Marvel Comics/Hawkeye"):
        return {"title": title, "publisher": pub, "folder_path": str(shelf / folder)}

    def test_single_agreeing_candidate_matches(self, shelf):
        rows = [_row(1, "Hawkeye", "Marvel Comics", 2016), _row(9, "Hawkeye #1", "Marvel Comics", 2016, comic=True)]
        assert si.find_confident_match(self._series(shelf, "Hawkeye"), search=lambda q: rows) == 1

    def test_several_runs_disambiguated_by_file_years(self, shelf):
        rows = [_row(1, "Hawkeye", "Marvel Comics", 2012), _row(2, "Hawkeye", "Marvel Comics", 2016),
                _row(3, "Hawkeye", "Marvel Comics", 2021)]
        assert si.find_confident_match(self._series(shelf, "Hawkeye"), search=lambda q: rows) == 2

    def test_several_runs_and_no_year_evidence_is_not_a_match(self, shelf):
        rows = [_row(1, "Batman: Knightfall", "DC Comics", 1993), _row(2, "Batman: Knightfall", "DC Comics", 2012)]
        s = self._series(shelf, "Batman - Knightfall", "DC Comics", "DC Comics/Batman - Knightfall")
        assert si.find_confident_match(s, search=lambda q: rows) is None

    def test_punctuation_insensitive_title(self, shelf):
        rows = [_row(5, "Batman: Knightfall", "DC Comics", 1993)]
        s = self._series(shelf, "Batman - Knightfall", "DC Comics", "DC Comics/Batman - Knightfall")
        assert si.find_confident_match(s, search=lambda q: rows) == 5

    def test_other_publisher_and_other_title_rejected(self, shelf):
        rows = [_row(1, "Hawkeye", "IDW Publishing", 2016), _row(2, "Hawkeye: Kate Bishop", "Marvel Comics", 2016)]
        assert si.find_confident_match(self._series(shelf, "Hawkeye"), search=lambda q: rows) is None

    def test_folder_year_beats_file_years(self, shelf):
        rows = [_row(1, "Batman", "DC Comics", 1940), _row(2, "Batman", "DC Comics", 2016)]
        s = self._series(shelf, "Batman (2016)", "DC Comics", "DC Comics/Batman - Knightfall")
        assert si.find_confident_match(s, search=lambda q: rows) == 2


class TestMatchPending:
    def test_statuses_set_and_unmatched_never_title_guessed(self, shelf, monkeypatch):
        si.import_new_folders()
        monkeypatch.setattr(si, "find_confident_match",
                            lambda s, **k: 77 if s["title"] == "Hawkeye" else None)
        guessed = []
        monkeypatch.setattr(sync, "find_series_id_anon", lambda *a, **k: guessed.append(a) or 999)
        monkeypatch.setattr(sync, "get_issues_anon", lambda sid: [])
        monkeypatch.setattr(sync, "get_trades_anon", lambda sid: [])
        monkeypatch.setattr(sync, "_komga", lambda: None)
        r = si.match_pending(sleep=lambda s: None)
        assert r == {"auto": 1, "needs_match": 1, "failed": 0}
        by = {s["title"]: s for s in db.get_all_series(si.DB_PATH)}
        assert (by["Hawkeye"]["locg_series_id"], by["Hawkeye"]["match_status"]) == (77, "auto")
        assert (by["Batman - Knightfall"]["locg_series_id"], by["Batman - Knightfall"]["match_status"]) == (None, "needs_match")
        assert guessed == []                                   # sync never guessed the unmatched one

    def test_locg_not_answering_is_not_a_no_match_and_stops_the_run(self, shelf, monkeypatch):
        # 2026-10-08: Cloudflare 403'd every search and the first cut marked each
        # series 'needs_match' — a decision handed to you for a lookup that failed.
        si.import_new_folders()
        calls = []
        def boom(s, **k):
            calls.append(s["title"])
            raise RuntimeError("403 Forbidden")
        monkeypatch.setattr(si, "find_confident_match", boom)
        monkeypatch.setattr(si, "MAX_FAILURES_IN_A_ROW", 2)
        r = si.match_pending(sleep=lambda s: None)
        assert r["failed"] == 2 and r["needs_match"] == 0 and len(calls) == 2
        assert {s["match_status"] for s in db.get_all_series(si.DB_PATH)} == {"pending"}

    def test_manual_pick_links_and_marks_manual(self, shelf, monkeypatch):
        si.import_new_folders()
        monkeypatch.setattr(main, "sync_one_guarded", lambda *a, **k: None)
        kn = next(s for s in db.get_all_series(si.DB_PATH) if s["title"] == "Batman - Knightfall")
        out = main.set_series_locg(kn["id"], main.MatchRequest(locg_id=4242))
        assert (out["locg_series_id"], out["match_status"]) == (4242, "manual")


class TestPullSwitchAndCadence:
    def test_pull_off_cancels_waiting_queue_rows_only(self, db_path, series, monkeypatch):
        monkeypatch.setattr(main, "DB_PATH", db_path)
        for n in (1.0, 2.0, 3.0):
            db.queue_issue(series, n, db_path)
        rows = {q["issue_number"]: q["id"] for q in db.get_queue(db_path)}
        db.update_queue_state(rows[2.0], "downloading", path=db_path)
        db.update_queue_state(rows[3.0], "not_found", path=db_path)
        out = main.toggle_pull_list(series, main.PullListRequest(on_pull_list=False))
        assert out["cancelled"] == 2 and out["on_pull_list"] == 0
        assert [q["state"] for q in db.get_queue(db_path)] == ["downloading"]

    def test_not_pulled_series_sync_weekly_stalest_first_capped(self, monkeypatch):
        now = datetime.now(timezone.utc)
        ts = lambda d: (now - timedelta(days=d)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
        series = [{"id": 1, "on_pull_list": 1, "last_synced": ts(0)}] + \
                 [{"id": 10 + i, "on_pull_list": 0, "last_synced": ts(8 + i)} for i in range(5)] + \
                 [{"id": 99, "on_pull_list": 0, "last_synced": ts(2)}, {"id": 98, "on_pull_list": 0, "last_synced": None}]
        monkeypatch.setattr(main, "NOT_PULLED_PER_RUN", 3)
        picked = [s["id"] for s in main._series_due_for_sync(series)]
        assert picked == [1, 98, 14, 13]                       # pulled always; never-synced, then stalest


class TestPoliteness:
    """2026-10-08: LOCG had started challenging us; the first matcher would have
    sent ~360 calls an hour into that. These pin the manners."""

    def test_refusal_pauses_all_locg_traffic_and_persists(self, db_path, monkeypatch):
        import time as _t
        import kometa.locg_client as lc
        monkeypatch.setattr(db, "DB_PATH", db_path)
        monkeypatch.setattr(lc, "_pause", {"until": 0.0})
        class R:
            status_code = 403
            headers = {"cf-mitigated": "challenge", "server": "cloudflare"}
        lc._note_refusal(R())
        assert lc.locg_paused() and lc.locg_paused() > _t.time() + 3600
        with pytest.raises(lc.LocgPaused):
            lc._check_paused()
        monkeypatch.setattr(lc, "_pause", {"until": None})          # fresh process
        assert lc.locg_paused()                                     # read back from config

    def test_ordinary_404_is_not_a_refusal(self, monkeypatch):
        import kometa.locg_client as lc
        monkeypatch.setattr(lc, "_pause", {"until": 0.0})
        class R:
            status_code = 404
            headers = {"server": "nginx"}
        lc._note_refusal(R())
        assert lc.locg_paused() is None

    def test_trickle_is_one_series_daytime_only_and_respects_the_pause(self, shelf, monkeypatch):
        import kometa.locg_client as lc
        si.import_new_folders()
        seen = []
        monkeypatch.setattr(si, "match_pending", lambda limit=None, sleep=None: seen.append(limit))
        monkeypatch.setattr(lc, "_pause", {"until": 0.0})
        si.trickle_tick(now_hour=3)
        assert seen == []                                          # night: nothing
        si.trickle_tick(now_hour=11)
        assert seen == [1]                                         # day: exactly one
        monkeypatch.setattr(lc, "_pause", {"until": 4102444800.0})  # paused
        si.trickle_tick(now_hour=11)
        assert seen == [1]

    def test_match_now_links_or_hands_you_the_choice(self, shelf, monkeypatch):
        si.import_new_folders()
        monkeypatch.setattr(si, "find_confident_match", lambda s, **k: 55 if s["title"] == "Hawkeye" else None)
        monkeypatch.setattr("kometa.sync.sync_one_guarded", lambda *a, **k: None)
        by = {s["title"]: s["id"] for s in db.get_all_series(si.DB_PATH)}
        assert si.match_one(by["Hawkeye"]) == "auto"
        assert si.match_one(by["Batman - Knightfall"]) == "needs_match"


class TestCoversWithoutKomgaOrLocg:
    def test_series_card_and_owned_issue_tile_fall_back_to_the_file(self, shelf, monkeypatch):
        import kometa.thumbnails as th
        import kometa.reader as rd
        monkeypatch.setattr(th, "DB_PATH", si.DB_PATH)
        monkeypatch.setattr(rd, "DB_PATH", si.DB_PATH)
        monkeypatch.setattr(rd, "PAGE_CACHE_DIR", str(shelf.parent / "page-cache"))
        monkeypatch.setattr(th, "_komga", lambda: None)
        si.import_new_folders()
        hk = next(s for s in db.get_all_series(si.DB_PATH) if s["title"] == "Hawkeye")
        card = th.series_thumbnail(hk["id"])
        assert card.media_type == "image/jpeg" and len(card.body) > 100
        tile = th.issue_thumbnail(hk["id"], 1.0)
        assert tile.media_type == "image/jpeg" and len(tile.body) > 100
