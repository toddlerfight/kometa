"""db.py — the stuff that bites if it's wrong: the atomic complete_download
(the double-download fix lives or dies here), queue requeue rules, and the
fresh-install migration that once dropped columns on its way to nullable.
"""
import os
import sqlite3

import kometa.db as db


def _qid_for(db_path, series_id, number):
    return next(q["id"] for q in db.get_queue(db_path)
               if q["tracked_series_id"] == series_id and q["issue_number"] == number)


class TestCompleteDownload:
    def test_marks_done_and_owns_issue_in_one_shot(self, db_path, series):
        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)

        db.complete_download(
            qid, series, 1.0, store_date="2012-03-14",
            filename="/comics/Image/Saga/Saga #001.cbz",
            set_folder_path="/comics/Image/Saga", path=db_path,
        )

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"
        assert q["filename"] == "/comics/Image/Saga/Saga #001.cbz"

        issue = next(i for i in db.get_issues_for_series(series, db_path) if i["number"] == 1.0)
        assert issue["owned"] == 1
        assert issue["store_date"] == "2012-03-14"

        # folder_path stamped on the series in the same transaction
        assert db.get_series_by_id(series, db_path)["folder_path"] == "/comics/Image/Saga"

    def test_completed_issue_is_no_longer_missing(self, db_path, series):
        """The whole point of the atomic write: once done, no ghost re-download."""
        db.upsert_issue_status(series, 1.0, "2012-03-14", owned=False, path=db_path)

        # missing before we touch it (no live queue row yet)
        before = {r["number"] for r in db.get_missing_for_monitored(db_path)}
        assert 1.0 in before

        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        db.complete_download(qid, series, 1.0, "2012-03-14",
                             filename="/x/Saga #001.cbz", path=db_path)

        after = {r["number"] for r in db.get_missing_for_monitored(db_path)}
        assert 1.0 not in after

    def test_no_set_folder_path_leaves_series_untouched(self, db_path, series):
        db.queue_issue(series, 2.0, db_path)
        qid = _qid_for(db_path, series, 2.0)
        db.complete_download(qid, series, 2.0, None,
                             filename="/x/Saga #002.cbz", path=db_path)
        assert db.get_series_by_id(series, db_path)["folder_path"] is None


class TestQueueRequeue:
    def test_failed_item_requeues_and_clears_error(self, db_path, series):
        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        db.update_queue_state(qid, "failed", error="boom", path=db_path)

        db.queue_issue(series, 1.0, db_path)  # re-request

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "queued"
        assert q["error"] is None

    def test_done_item_is_not_requeued(self, db_path, series):
        db.queue_issue(series, 1.0, db_path)
        qid = _qid_for(db_path, series, 1.0)
        db.update_queue_state(qid, "done", path=db_path)

        db.queue_issue(series, 1.0, db_path)  # should be a no-op on state

        q = next(x for x in db.get_queue(db_path) if x["id"] == qid)
        assert q["state"] == "done"


class TestResetStuckQueueItems:
    def test_searching_and_downloading_reset_to_queued(self, db_path, series):
        db.queue_issue(series, 1.0, db_path)
        db.queue_issue(series, 2.0, db_path)
        ids = {1.0: _qid_for(db_path, series, 1.0), 2.0: _qid_for(db_path, series, 2.0)}
        db.update_queue_state(ids[1.0], "searching", path=db_path)
        db.update_queue_state(ids[2.0], "downloading", path=db_path)

        db.reset_stuck_queue_items(db_path)

        states = {q["id"]: q["state"] for q in db.get_queue(db_path)}
        assert states[ids[1.0]] == "queued"
        assert states[ids[2.0]] == "queued"

    def test_a_pack_cut_off_mid_placing_goes_back_to_its_finished_download(self, db_path, series):
        with db._connect(db_path) as c:
            c.execute("""INSERT INTO download_queue (tracked_series_id, issue_number, kind, state, torrent_hash)
                VALUES (?, -1220, 'list_pack', 'processing', 'abc')""", (series,))
            c.execute("""INSERT INTO download_queue (tracked_series_id, issue_number, kind, state, sab_nzo_id)
                VALUES (?, -1221, 'list_pack', 'processing', 'nzo')""", (series,))
            c.execute("""INSERT INTO download_queue (tracked_series_id, issue_number, kind, state)
                VALUES (?, -1222, 'list_pack', 'processing')""", (series,))
        db.reset_stuck_queue_items(db_path)
        states = {q["issue_number"]: q["state"] for q in db.get_queue(db_path)}
        assert states == {-1220: "pending_torrent", -1221: "pending_usenet", -1222: "failed"}


class TestEnvConfigSeeding:
    """Env vars provision config on first boot (so compose-only setup works),
    but never override what's already in the DB."""

    def test_env_seeded_on_fresh_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COMICS_ROOT", "/srv/comics")
        p = str(tmp_path / "k.db")
        db.init_db(p)
        assert db.get_config(p)["comics_root"] == "/srv/comics"

    def test_komga_env_is_ignored(self, tmp_path, monkeypatch):
        # Komga is retired (2026-10-12): a leftover KOMGA_* in someone's .env
        # must not mint config rows nothing reads
        monkeypatch.setenv("KOMGA_URL", "http://example:8585")
        monkeypatch.setenv("KOMGA_USER", "kuser")
        p = str(tmp_path / "k.db")
        db.init_db(p)
        cfg = db.get_config(p)
        assert "komga_url" not in cfg and "komga_user" not in cfg

    def test_unset_env_seeds_nothing(self, tmp_path, monkeypatch):
        for v in ("COMICS_ROOT", "CV_API_KEY"):
            monkeypatch.delenv(v, raising=False)
        p = str(tmp_path / "k.db")
        db.init_db(p)
        assert "comics_root" not in db.get_config(p)

    def test_env_does_not_override_existing_ui_value(self, tmp_path, monkeypatch):
        p = str(tmp_path / "k.db")
        monkeypatch.delenv("COMICS_ROOT", raising=False)
        db.init_db(p)
        db.set_config({"comics_root": "/ui/set"}, p)  # user configured via UI

        monkeypatch.setenv("COMICS_ROOT", "/env/changed")
        db.init_db(p)  # restart with a different env value

        assert db.get_config(p)["comics_root"] == "/ui/set"  # UI wins


class TestKomgaLeavesTheSchema:
    def _old_db(self, p):
        """A DB as the Komga years left it: Komga's columns, its version table, its
        config (a password among it), issue_status pointing at the "_ts_old" table an
        old rebuild left behind, and id counters running ahead of the highest ids."""
        conn = sqlite3.connect(p)
        conn.executescript("""
            CREATE TABLE tracked_series (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                komga_series_id   TEXT UNIQUE,
                metron_series_id  INTEGER NOT NULL,
                title             TEXT NOT NULL,
                publisher         TEXT,
                year_began        INTEGER,
                added_at          TEXT DEFAULT (datetime('now')),
                last_synced       TEXT,
                on_pull_list      INTEGER NOT NULL DEFAULT 1,
                monitor_status    TEXT NOT NULL DEFAULT 'monitored',
                folder_path       TEXT,
                cv_volume_id      TEXT,
                locg_series_id    INTEGER);
            CREATE TABLE issue_status (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                tracked_series_id INTEGER NOT NULL REFERENCES "_ts_old"(id),
                number            REAL NOT NULL,
                store_date        TEXT,
                owned          INTEGER NOT NULL DEFAULT 0,
                komga_book_id     TEXT, metron_image TEXT,
                UNIQUE(tracked_series_id, number));
            CREATE TABLE arc_issues (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                arc_series_id   INTEGER NOT NULL REFERENCES tracked_series(id),
                reading_order   INTEGER NOT NULL,
                source_title    TEXT NOT NULL,
                number          TEXT,
                story_title     TEXT,
                cv_issue_id     TEXT,
                komga_book_id   TEXT,
                owned           INTEGER NOT NULL DEFAULT 0,
                UNIQUE(arc_series_id, reading_order));
            CREATE TABLE komga_book_version (book_id TEXT, v INTEGER);
            CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO tracked_series (id, komga_series_id, metron_series_id, title, folder_path)
                VALUES (5, 'K5', 1, 'Saga', '/c/Saga'), (9, NULL, 2, 'Low', NULL);
            INSERT INTO issue_status (id, tracked_series_id, number, owned, komga_book_id, metron_image)
                VALUES (40, 5, 1.0, 1, 'B1', 'http://img/1'), (41, 5, 2.0, 0, NULL, NULL);
            INSERT INTO arc_issues (id, arc_series_id, reading_order, source_title, komga_book_id, owned)
                VALUES (3, 9, 1, 'Low', 'B9', 1);
            INSERT INTO komga_book_version VALUES ('B1', 2);
            INSERT INTO config VALUES ('komga_url', 'http://komga:8585'), ('komga_pass', 'secret'), ('sync_hours', '6');
            UPDATE sqlite_sequence SET seq = 900 WHERE name = 'tracked_series';
            UPDATE sqlite_sequence SET seq = 700 WHERE name = 'issue_status';
        """)
        conn.commit()
        conn.close()

    def test_an_old_db_loses_komga_and_keeps_everything_else(self, tmp_path):
        p = str(tmp_path / "old.db")
        self._old_db(p)
        db.init_db(p)
        c = sqlite3.connect(p)
        assert [n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE sql LIKE '%komga%' OR sql LIKE '%_ts_old%'")] == []
        assert c.execute("SELECT key FROM config WHERE key LIKE 'komga%'").fetchall() == []
        assert c.execute("SELECT value FROM config WHERE key = 'sync_hours'").fetchone() == ("6",)
        assert [r[:3] for r in c.execute("SELECT id, title, folder_path FROM tracked_series ORDER BY id")] == [
            (5, "Saga", "/c/Saga"), (9, "Low", None)]
        assert c.execute("SELECT id, number, owned, metron_image FROM issue_status ORDER BY id").fetchall() == [
            (40, 1.0, 1, "http://img/1"), (41, 2.0, 0, None)]
        assert c.execute("SELECT id, owned FROM arc_issues").fetchall() == [(3, 1)]
        # the counters ran ahead of the highest ids: they still do
        assert dict(c.execute("SELECT name, seq FROM sqlite_sequence WHERE name IN ('tracked_series', 'issue_status')")) == {
            "tracked_series": 900, "issue_status": 700}
        assert db.add_series(title="New", path=p) == 901
        fk = c.execute("PRAGMA foreign_key_list(issue_status)").fetchall()
        assert [r[2] for r in fk] == ["tracked_series"]
        notnull = {r[1]: r[3] for r in c.execute("PRAGMA table_info(tracked_series)")}
        assert notnull["metron_series_id"] == 0                                 # an unmatched series can be added
        assert db._drop_komga(p) is False                                       # once: a second start does nothing

    def test_a_fresh_db_never_had_komga(self, tmp_path):
        p = str(tmp_path / "fresh.db")
        db.init_db(p)
        c = sqlite3.connect(p)
        assert [n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE sql LIKE '%komga%'")] == []


class TestFreshInstallMigration:
    def test_nullable_rebuild_keeps_locg_column(self, tmp_path):
        """Regression: an old nullable rebuild used to drop locg_series_id,
        crashing fresh installs on first write. (cv_volume_id is
        a vestigial column kept after the Comic Vine removal — no setter.)"""
        p = str(tmp_path / "fresh.db")
        db.init_db(p)

        cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(tracked_series)")}
        assert "locg_series_id" in cols
        assert "cv_volume_id" in cols  # still present, just unused now

        sid = db.add_series(title="X", publisher="Y", path=p)
        db.set_locg_series_id(sid, 9999, p)
        row = db.get_series_by_id(sid, p)
        assert row["locg_series_id"] == 9999


def test_rename_book_path_rereads_the_number_from_the_new_name(db_path, tmp_path):
    """Dr. Manhattan: 'TPB 01' had no number; renamed to '#001' it must get one,
    or the series page shows the same file as #1 AND under 'Also on the shelf'."""
    sid = db.add_series(title="Before Watchmen: Dr. Manhattan", publisher="DC", folder_path=str(tmp_path),
                        on_pull_list=False, path=db_path)
    sh = db.upsert_shelf_series(str(tmp_path), "Before Watchmen- Dr. Manhattan", "DC", sid, 1, "2026-10-09T00:00:00Z", db_path)
    old = str(tmp_path / "Dr. Manhattan TPB 01.cbz"); new = str(tmp_path / "Before Watchmen - Dr. Manhattan #001 (2012).cbz")
    db.index_books([(old, 10, 1.0, None, sh, sid)], db_path)
    assert db.rename_book_path(old, new, db_path) == 1
    assert db.get_book_by_path(new, db_path)["number"] == 1.0
    # and the heal for rows renamed before this: a null that parses now
    db.index_books([(str(tmp_path / "Before Watchmen - Dr. Manhattan #002 (2012).cbz"), 10, 1.0, None, sh, sid)], db_path)
    assert db.reparse_null_numbers(db_path) == 1
    assert db.reparse_null_numbers(db_path) == 0


def test_variant_cover_url_is_the_chosen_variants_image(db_path, series):
    import json
    with db._connect(db_path) as c:
        c.execute("INSERT INTO variant_prefs (tracked_series_id, number, selected, primary_id) VALUES (?, 2.0, ?, 'v2')",
                  (series, json.dumps([{"id": "v1", "large": "https://x/v1.jpg"}, {"id": "v2", "large": "https://x/v2.jpg"}])))
    assert db.variant_cover_url(series, 2.0, db_path) == "https://x/v2.jpg"
    assert db.variant_cover_url(series, 3.0, db_path) is None


class TestConnectionReuse:
    def test_one_connection_per_thread_and_nested_blocks_share_the_transaction(self, db_path, series):
        with db._connect(db_path) as a:
            with db._connect(db_path) as b:
                assert a is b
        try:
            with db._connect(db_path) as outer:
                outer.execute("UPDATE tracked_series SET title = 'Changed' WHERE id = ?", (series,))
                with db._connect(db_path) as inner:
                    inner.execute("UPDATE tracked_series SET publisher = 'Changed' WHERE id = ?", (series,))
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        s = db.get_series_by_id(series, db_path)
        assert (s["title"], s["publisher"]) == ("Saga", "Image")         # both rolled back together

    def test_another_thread_sees_committed_writes(self, db_path, series):
        import threading
        with db._connect(db_path) as c:
            c.execute("UPDATE tracked_series SET title = 'Seen' WHERE id = ?", (series,))
        got = []
        t = threading.Thread(target=lambda: got.append(db.get_series_by_id(series, db_path)["title"]))
        t.start(); t.join()
        assert got == ["Seen"]


class TestRestartMidPlacing:
    def test_an_issue_cut_off_mid_placing_goes_back_to_its_download(self, db_path, series):
        db.queue_issue(series, 1.0, db_path)
        db.queue_issue(series, 2.0, db_path)
        with db._connect(db_path) as c:
            c.execute("UPDATE download_queue SET state = 'processing', sab_nzo_id = 'nzo' WHERE issue_number = 1")
            c.execute("UPDATE download_queue SET state = 'processing' WHERE issue_number = 2")
        db.reset_stuck_queue_items(db_path)
        states = {q["issue_number"]: q["state"] for q in db.get_queue(db_path)}
        assert states == {1.0: "pending_usenet", 2.0: "queued"}       # no handle: nothing to go back to


class TestRemoveSeries:
    def test_nothing_of_a_removed_series_is_left_behind(self, db_path, series):
        db.queue_issue(series, 1.0, db_path)
        db.set_trades(series, [{"locg_id": "1", "title": "Saga Vol. 1"}], db_path)
        other = db.add_series(title="Paper Girls", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
        db.queue_issue(other, 1.0, db_path)
        db.remove_series(series, db_path)
        assert db.get_trades(series, db_path) is None
        with db._connect(db_path) as c:
            assert c.execute("SELECT COUNT(*) FROM download_queue WHERE tracked_series_id = ?", (series,)).fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM download_queue WHERE tracked_series_id = ?", (other,)).fetchone()[0] == 1


def _nfs_spelling(monkeypatch):
    """This Mac's disk treats both spellings of an accent as one folder; NFS on
    ginbako doesn't. Make isdir answer like NFS: only the NFC spelling exists."""
    import unicodedata
    real = os.path.isdir
    monkeypatch.setattr(db.os.path, "isdir", lambda p: unicodedata.is_normalized("NFC", p) and real(p))


class TestFolderSpellings:
    def test_an_old_spelling_points_at_the_folder_and_its_duplicate_folds_back(self, db_path, tmp_path, monkeypatch):
        import unicodedata
        _nfs_spelling(monkeypatch)
        good = str(tmp_path / "Image Comics" / unicodedata.normalize("NFC", "Stillwater by Zdarsky & Pérez"))
        old = unicodedata.normalize("NFD", good)
        os.makedirs(good)
        orig = db.add_series(title="Stillwater", publisher="Image", folder_path=None, on_pull_list=False,
                             locg_series_id=147740, path=db_path)
        with db._connect(db_path) as c:                      # stored the old share's way
            c.execute("UPDATE tracked_series SET folder_path = ?, match_status = 'manual' WHERE id = ?", (old, orig))
        dup = db.add_series(title="Stillwater", publisher="Image", folder_path=good, on_pull_list=True, path=db_path)
        db.set_match_status(dup, "needs_match", db_path)
        sh = db.upsert_shelf_series(good, "Stillwater", "Image Comics", dup, 1, "2026-10-11T00:00:00Z", db_path)
        db.index_books([(good + "/Stillwater #001.cbz", 1, 1.0, 1.0, sh, dup)], db_path)
        did = db.heal_folder_spellings(db_path)
        assert any("folded duplicate" in d for d in did)
        assert db.get_series_by_id(dup, db_path) is None
        assert db.get_series_by_id(orig, db_path)["folder_path"] == good
        assert db.get_book_by_path(good + "/Stillwater #001.cbz", db_path)["tracked_series_id"] == orig
        assert db.heal_folder_spellings(db_path) == []         # once

    def test_a_matched_second_series_is_never_folded(self, db_path, tmp_path, monkeypatch):
        import unicodedata
        _nfs_spelling(monkeypatch)
        good = str(tmp_path / unicodedata.normalize("NFC", "Pérez"))
        os.makedirs(good)
        a = db.add_series(title="A", publisher="X", folder_path=None, on_pull_list=False, path=db_path)
        with db._connect(db_path) as c:
            c.execute("UPDATE tracked_series SET folder_path = ? WHERE id = ?", (unicodedata.normalize("NFD", good), a))
        b = db.add_series(title="B", publisher="X", folder_path=good, on_pull_list=False, locg_series_id=9, path=db_path)
        did = db.heal_folder_spellings(db_path)
        assert db.get_series_by_id(b, db_path) and did and did[0].startswith("left")


def test_clear_history_takes_only_finished_downloads(db_path, series):
    for n, state in ((1.0, "done"), (2.0, "failed"), (3.0, "not_found")):
        db.queue_issue(series, n, db_path)
        with db._connect(db_path) as c:
            c.execute("UPDATE download_queue SET state = ? WHERE issue_number = ?", (state, n))
    assert db.clear_queue_history(db_path) == 1
    assert sorted(q["state"] for q in db.get_queue(db_path)) == ["failed", "not_found"]
