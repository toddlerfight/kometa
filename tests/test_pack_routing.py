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

    def search_series_pack(self, title, issue_count, series_year=None, **kw):
        _FakeGC.calls.append((title, issue_count, series_year))
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
