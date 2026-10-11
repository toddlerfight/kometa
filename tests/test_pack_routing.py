"""A big backlog should take the complete-run pack, not thirty-three chances to
grab the wrong volume. Born 2026-09-11: following New Avengers (2013) filled the
folder with the 2010, 2015 and 2025 runs because _sweep_missing's only pack route
was usenet-only, and the single-issue matcher rejects packs by design."""
import json

import pytest

import kometa.db as db
import kometa.acquisition as acq


@pytest.fixture
def big_backlog(db_path, tmp_path):
    """A tracked series with a folder on disk and 33 released, unowned issues."""
    folder = tmp_path / "New Avengers"
    folder.mkdir()
    sid = db.add_series(
        komga_series_id=None, title="New Avengers", publisher="Marvel Comics",
        year_began=2013, folder_path=str(folder), on_pull_list=True, path=db_path,
    )
    db.upsert_issue_status_many(
        [(sid, float(n), "2013-01-02", 0, None, None, None) for n in range(1, 34)],
        path=db_path)
    return sid


class _FakeGC:
    """Finds the complete-run pack and nothing else."""
    calls: list = []

    def __init__(self):
        pass

    def search_series_pack(self, title, last_issue, series_year=None, **kw):
        _FakeGC.calls.append((title, last_issue, series_year))
        return "https://getcomics.org/dls/THE-PACK", None


def _sweep(monkeypatch, db_path):
    monkeypatch.setattr(acq, "DB_PATH", db_path)
    monkeypatch.setattr(acq, "GetComicsClient", _FakeGC)
    # No usenet/torrent side — isolate the GetComics pack route.
    monkeypatch.setattr(acq, "_prowlarr", lambda: None)
    monkeypatch.setattr(acq, "_sabnzbd", lambda: None)
    acq._sweep_missing()


def test_backlog_queues_the_pack_not_every_issue(monkeypatch, db_path, big_backlog):
    _FakeGC.calls = []
    _sweep(monkeypatch, db_path)
    rows = db.get_all_queued(db_path) if hasattr(db, "get_all_queued") else None
    with db._connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM download_queue")]
    trades = [r for r in rows if r["kind"] == "trade"]
    issues = [r for r in rows if r["kind"] != "trade"]
    assert len(trades) == 1, f"expected one pack trade, got {rows}"
    assert not issues, f"single issues queued alongside the pack: {issues}"
    assert _FakeGC.calls == [("New Avengers", 33, 2013)]


def test_pack_url_is_pinned_for_the_worker(monkeypatch, db_path, big_backlog):
    _FakeGC.calls = []
    _sweep(monkeypatch, db_path)
    with db._connect(db_path) as conn:
        row = dict(conn.execute("SELECT * FROM download_queue WHERE kind='trade'").fetchone())
    assert json.loads(row["meta_json"])["pack_url"] == "https://getcomics.org/dls/THE-PACK"


def test_sweeping_twice_does_not_mint_a_duplicate(monkeypatch, db_path, big_backlog):
    _FakeGC.calls = []
    _sweep(monkeypatch, db_path)
    _sweep(monkeypatch, db_path)
    with db._connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM download_queue WHERE kind='trade'").fetchone()[0]
    assert n == 1, "a null-ish locg_id would insert a fresh pack row every sweep"


def test_series_off_the_pull_list_is_left_alone(monkeypatch, db_path, big_backlog):
    with db._connect(db_path) as conn:
        conn.execute("UPDATE tracked_series SET on_pull_list=0 WHERE id=?", (big_backlog,))
    _FakeGC.calls = []
    _sweep(monkeypatch, db_path)
    with db._connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM download_queue").fetchone()[0]
    assert n == 0 and _FakeGC.calls == []


class _FakeSab:
    def add_nzb_url(self, url, nzb_name=None):
        return "nzo-1"


def test_failed_usenet_pack_hands_off_to_getcomics(monkeypatch, db_path, big_backlog):
    """'Submitted' is not 'delivered'. A usenet pack that came back
    'Aborted, cannot be completed' must not be re-submitted on every sweep while
    the GetComics fallback sits locked out behind it."""
    with db._connect(db_path) as conn:
        conn.execute("""INSERT INTO download_queue (tracked_series_id, issue_number, kind, state)
                        VALUES (?, -1, 'issue', 'failed')""", (big_backlog,))
    usenet_calls = []
    monkeypatch.setattr(acq, "DB_PATH", db_path)
    monkeypatch.setattr(acq, "GetComicsClient", _FakeGC)
    monkeypatch.setattr(acq, "_prowlarr", lambda: object())
    monkeypatch.setattr(acq, "_sabnzbd", lambda: _FakeSab())
    monkeypatch.setattr(acq, "_prowlarr_on", lambda: True)
    monkeypatch.setattr(acq, "_usenet_on", lambda: True)
    monkeypatch.setattr(acq, "search_usenet_pack",
                        lambda *a, **k: usenet_calls.append(a) or "http://nzb/pack")
    _FakeGC.calls = []
    acq._sweep_missing()

    assert usenet_calls == [], "a spent usenet pack got another turn"
    with db._connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM download_queue WHERE kind='trade'").fetchone()[0]
    assert n == 1, "GetComics fallback never ran"
    assert _FakeGC.calls == [("New Avengers", 33, 2013)]


# --- dead packs retire once their series is whole -------------------------------
# The Sacrificers: pack q4527 failed 'Usenet: stalled', then all 21 issues landed
# one by one — and the failed pack sat in Activity forever, gate shut.

def _pack_row(db_path, sid, state, error="Usenet: stalled"):
    with db._connect(db_path) as conn:
        return conn.execute(
            """INSERT INTO download_queue (tracked_series_id, issue_number, kind, state, error)
               VALUES (?, -1, 'issue', ?, ?)""", (sid, state, error)).lastrowid


def _row(db_path, qid):
    with db._connect(db_path) as conn:
        return dict(conn.execute("SELECT * FROM download_queue WHERE id=?", (qid,)).fetchone())


def _own_all(db_path, sid):
    with db._connect(db_path) as conn:
        conn.execute("UPDATE issue_status SET owned=1 WHERE tracked_series_id=?", (sid,))


@pytest.mark.parametrize("state", ["failed", "not_found"])
def test_dead_pack_closes_once_every_issue_landed(db_path, big_backlog, state):
    qid = _pack_row(db_path, big_backlog, state)
    _own_all(db_path, big_backlog)
    assert db.pack_attempt_failed(big_backlog, db_path)

    assert db.retire_completed_packs(db_path) == 1
    row = _row(db_path, qid)
    assert row["state"] == "done"
    assert row["error"] == "Every issue landed"
    # the gate lifts — a future backlog gets a fresh pack shot
    assert not db.pack_attempt_failed(big_backlog, db_path)
    assert not db.has_active_pack(big_backlog, db_path)


def test_dead_pack_stays_while_issues_are_still_missing(db_path, big_backlog):
    qid = _pack_row(db_path, big_backlog, "failed")
    with db._connect(db_path) as conn:
        conn.execute("UPDATE issue_status SET owned=1 WHERE tracked_series_id=? AND number < 33",
                     (big_backlog,))
    assert db.retire_completed_packs(db_path) == 0
    assert _row(db_path, qid)["state"] == "failed"


def test_ignored_covered_and_unreleased_issues_dont_hold_a_pack_open(db_path, big_backlog):
    qid = _pack_row(db_path, big_backlog, "failed")
    with db._connect(db_path) as conn:
        conn.execute("UPDATE issue_status SET owned=1 WHERE tracked_series_id=? AND number <= 30",
                     (big_backlog,))
        conn.execute("UPDATE issue_status SET ignored=1 WHERE tracked_series_id=? AND number = 31",
                     (big_backlog,))
        conn.execute("UPDATE issue_status SET covered_by=1 WHERE tracked_series_id=? AND number = 32",
                     (big_backlog,))
        conn.execute("UPDATE issue_status SET store_date='2999-01-01' WHERE tracked_series_id=? AND number = 33",
                     (big_backlog,))
    assert db.retire_completed_packs(db_path) == 1
    assert _row(db_path, qid)["state"] == "done"


@pytest.mark.parametrize("state", ["pending_usenet", "downloading", "processing", "queued"])
def test_live_pack_is_never_touched(db_path, big_backlog, state):
    qid = _pack_row(db_path, big_backlog, state, error=None)
    _own_all(db_path, big_backlog)
    assert db.retire_completed_packs(db_path) == 0
    assert _row(db_path, qid)["state"] == state


def test_series_with_no_owned_issues_never_reads_complete(db_path, tmp_path):
    """An empty catalogue isn't a finished one."""
    sid = db.add_series(komga_series_id=None, title="Blank", publisher="Image",
                        year_began=2020, folder_path=None, on_pull_list=True, path=db_path)
    qid = _pack_row(db_path, sid, "failed")
    assert db.retire_completed_packs(db_path) == 0
    assert _row(db_path, qid)["state"] == "failed"


def test_sweep_retires_the_dead_pack(monkeypatch, db_path, big_backlog):
    qid = _pack_row(db_path, big_backlog, "failed")
    _own_all(db_path, big_backlog)
    _FakeGC.calls = []
    _sweep(monkeypatch, db_path)
    assert _row(db_path, qid)["state"] == "done"
    assert _FakeGC.calls == [], "nothing missing — no pack search should run"


def test_a_dead_issue_row_closes_once_the_issue_is_on_the_shelf(db_path, big_backlog):
    # JIM #626.1: 'No result' as a single, then it arrived inside a run pack
    db.queue_issue(big_backlog, 3.0, db_path)
    db.queue_issue(big_backlog, 4.0, db_path)
    db.queue_issue(big_backlog, 5.0, db_path)
    with db._connect(db_path) as conn:
        conn.execute("UPDATE download_queue SET state='not_found' WHERE tracked_series_id=? AND issue_number IN (3, 4)", (big_backlog,))
        conn.execute("UPDATE download_queue SET state='downloading' WHERE tracked_series_id=? AND issue_number=5", (big_backlog,))
        conn.execute("UPDATE issue_status SET owned=1 WHERE tracked_series_id=? AND number IN (3, 5)", (big_backlog,))
    assert db.close_rows_now_owned(big_backlog, db_path) == 1
    with db._connect(db_path) as conn:
        st = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT issue_number, state, error FROM download_queue WHERE tracked_series_id=? AND issue_number > 0", (big_backlog,))}
    assert st[3.0] == ("done", "On the shelf")
    assert st[4.0][0] == "not_found"          # still not here
    assert st[5.0][0] == "downloading"        # a working row is never touched
