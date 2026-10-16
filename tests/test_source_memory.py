"""Source memory per series (kometa/acquisition.py): a series whose usenet downloads
keep failing sends its next issues to GetComics first (Puzzlebox, 2026-10-16)."""
import pytest

import kometa.acquisition as acq
import kometa.db as db


@pytest.fixture
def sid(db_path, monkeypatch):
    monkeypatch.setattr(acq, "DB_PATH", db_path)
    return db.add_series(title="The Joker Presents: A Puzzlebox", publisher="DC Comics", folder_path=None,
                         on_pull_list=True, path=db_path)


def test_two_failures_distrust_usenet_and_clean_downloads_earn_it_back(sid, db_path):
    assert not acq.distrusted(sid, "usenet")
    acq.note_strike(sid, "usenet", "only 3 pages")
    assert not acq.distrusted(sid, "usenet")                                   # one bad post can be a fluke
    acq.note_strike(sid, "usenet", "page 007 is cut short")
    assert acq.distrusted(sid, "usenet") and not acq.distrusted(sid, "torrent")
    for _ in range(4):
        acq.note_delivered(sid, "usenet")
    assert acq.distrusted(sid, "usenet")                                       # 2 of 6 failed: still a third
    acq.note_delivered(sid, "usenet")
    assert not acq.distrusted(sid, "usenet")                                   # 2 of 7: trusted again
    other = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    assert not acq.distrusted(other, "usenet")


def test_the_window_expires(sid, db_path):
    acq.note_strike(sid, "usenet", "bad CRC")
    acq.note_strike(sid, "usenet", "bad CRC")
    with db._connect(db_path) as c:
        c.execute("UPDATE source_strikes SET first_at = datetime('now', '-31 days'), last_at = datetime('now', '-31 days')")
    assert not acq.distrusted(sid, "usenet")
    assert acq.note_strike(sid, "usenet", "fragment") == 1                     # a fresh window, counting from one


def test_a_distrusted_series_tries_getcomics_first_for_back_issues(sid, monkeypatch):
    order = []
    monkeypatch.setattr(acq, "_is_old_issue", lambda d: True)
    monkeypatch.setattr(acq, "_issue_store_date", lambda item: "2021-01-01")
    monkeypatch.setattr(acq, "_try_getcomics", lambda *a, **k: (order.append("getcomics") or (True, None)))
    monkeypatch.setattr(acq, "_fallback_usenet_torrent", lambda *a, **k: order.append("usenet") or True)
    item = {"tracked_series_id": sid, "title": "The Joker Presents: A Puzzlebox", "issue_number": 7.0}
    acq._acquire_issue(item, 1, None, set())
    assert order == ["usenet"]                                                 # trusted: back issues go usenet first
    acq.note_strike(sid, "usenet", "only 2 pages")
    acq.note_strike(sid, "usenet", "page cut short")
    order.clear()
    acq._acquire_issue(item, 1, None, set())
    assert order == ["getcomics"]


def test_failed_usenet_jobs_are_what_count(sid, db_path, monkeypatch):
    monkeypatch.setattr(acq, "_rescue_later", lambda *a, **k: None)
    db.queue_issue(sid, 2.0, db_path)
    qid = next(q["id"] for q in db.get_queue(db_path))
    item = {"id": qid, "tracked_series_id": sid, "title": "Puzzlebox", "issue_number": 2.0, "kind": "issue",
            "source_url": "http://nzb/2"}
    acq._usenet_cascade(item, qid, "nzo2", "incomplete: 1 article missing and no repair")
    acq._usenet_cascade(dict(item, source_url="http://nzb/3"), qid, "nzo3", "incomplete: 2 articles missing")
    assert acq.distrusted(sid, "usenet")
    pack = dict(item, kind="list_pack")
    other = db.add_series(title="Other", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    acq._usenet_cascade(dict(pack, tracked_series_id=other), qid, "nzo4", "failed")
    acq._usenet_cascade(dict(pack, tracked_series_id=other), qid, "nzo5", "failed")
    assert not acq.distrusted(other, "usenet")                                 # a list pack isn't the series' uploads
