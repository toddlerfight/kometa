"""Activity's X on a running download. Before 2026-10-18 removing a running row
only deleted its record — the Loveless pack carried on for 871 MB with nothing on
screen — and a running row had no X at all. Now the download stops with its row:
a GetComics stream unwinds at its next chunk (no mirror, no usenet fallback), a
SABnzbd / qBittorrent job is cancelled there, and a row already placing its files
says it's too late."""
from datetime import date

import pytest
from fastapi import HTTPException

import kometa.db as db
import kometa.acquisition as acq
import kometa.downloader as dl


@pytest.fixture
def wired(db_path, series, monkeypatch):
    monkeypatch.setattr(acq, "DB_PATH", db_path)
    monkeypatch.setattr("kometa.main.DB_PATH", db_path)
    monkeypatch.setattr(acq, "_resync_after_placement", lambda *a, **k: None)
    monkeypatch.setattr(acq, "_prowlarr", lambda: None)
    monkeypatch.setattr(acq, "_qbittorrent", lambda: None)
    acq._stop_requested.clear()
    return db_path, series


def _qid(db_path, series, number):
    return next(q["id"] for q in db.get_queue(db_path)
                if q["tracked_series_id"] == series and q["issue_number"] == number)


def _set_state(db_path, qid, state, **cols):
    sets = ", ".join(["state = ?"] + [f"{k} = ?" for k in cols])
    with db._connect(db_path) as conn:
        conn.execute(f"UPDATE download_queue SET {sets} WHERE id = ?", (state, *cols.values(), qid))


def _press_x(qid):
    """What Activity's X does: DELETE /api/queue/<id>."""
    from kometa.main import delete_queue_item
    delete_queue_item(qid)


class _FakeGC:
    def search(self, title, number, *a, **k):
        return (f"http://dl/saga-{number}.cbz", "saga.cbz")

    def mirror_urls(self, url):
        raise AssertionError("a stop went looking for a mirror")


def test_x_mid_download_stops_it_and_the_pass_carries_on(wired, monkeypatch):
    db_path, series = wired
    for n in (1.0, 2.0):
        db.upsert_issue_status(series, n, str(date.today()), owned=False, path=db_path)
        db.queue_issue(series, n, db_path)
    first = _qid(db_path, series, 1.0)
    fetched = []

    def download_issue(**kw):
        fetched.append(kw["issue_number"])
        report = kw["progress_fn"]
        report(10, 100)
        if kw["issue_number"] == 1.0:
            _press_x(first)              # the owner taps X while it's running
        report(20, 100)                  # the next chunk sees the stop
        return f"/comics/Image/Saga/Saga #{int(kw['issue_number']):03d}.cbz"

    monkeypatch.setattr(acq, "GetComicsClient", _FakeGC)
    monkeypatch.setattr(acq.downloader, "download_issue", download_issue)
    monkeypatch.setattr(acq, "_fallback_usenet_torrent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fell back to usenet")))
    acq._process_queue()

    assert fetched == [1.0, 2.0], "the stop took the rest of the pass down with it"
    ids = {q["id"]: q for q in db.get_queue(db_path)}
    assert first not in ids
    assert ids[_qid(db_path, series, 2.0)]["state"] == "done"
    assert acq.get_progress(first) is None and first not in acq._stop_requested


def test_x_mid_trade_download_skips_the_usenet_fallback(wired, monkeypatch, tmp_path):
    db_path, series = wired
    with db._connect(db_path) as conn:
        conn.execute("UPDATE tracked_series SET folder_path = ? WHERE id = ?", (str(tmp_path), series))
    db.queue_trade(series, "loc1", "Saga", vol=1, path=db_path)
    qid = next(q["id"] for q in db.get_queue(db_path) if q["kind"] == "trade")

    class GC(_FakeGC):
        def search_trade(self, *a, **k):
            return ("http://dl/saga-v1.cbz", None)

    def download_trade(url, dest_dir, progress_fn=None, **k):
        progress_fn(10, 100)
        _press_x(qid)
        progress_fn(20, 100)
        raise AssertionError("kept downloading after the stop")

    monkeypatch.setattr(acq, "GetComicsClient", GC)
    monkeypatch.setattr(acq.downloader, "download_trade", download_trade)
    monkeypatch.setattr(acq, "_fallback_usenet_torrent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fell back to usenet")))
    acq._process_queue()
    assert all(q["id"] != qid for q in db.get_queue(db_path))


def test_the_stream_bins_its_partial_on_a_stop(tmp_path):
    acq._stop_requested.clear()

    class R:
        def iter_content(self, chunk_size):
            yield b"x" * 10
            acq._stop_requested.add(7)
            yield b"y" * 10

    with pytest.raises(acq.DownloadStopped):
        dl._stream_to(R(), str(tmp_path / "Saga #001.cbz"), 100, progress_fn=acq._progress_for(7))
    assert list(tmp_path.iterdir()) == []
    acq._stop_requested.clear()


def test_x_on_a_usenet_job_cancels_it_in_sabnzbd(wired, monkeypatch):
    db_path, series = wired
    db.queue_issue(series, 1.0, db_path)
    qid = _qid(db_path, series, 1.0)
    _set_state(db_path, qid, "pending_usenet", sab_nzo_id="nzo-9")
    deleted = []

    class Sab:
        def delete_job(self, nzo):
            deleted.append(nzo)
            return True
    monkeypatch.setattr(acq, "_sabnzbd", lambda: Sab())
    _press_x(qid)
    assert deleted == ["nzo-9"]
    assert all(q["id"] != qid for q in db.get_queue(db_path))


def test_x_on_a_torrent_cancels_it_in_qbittorrent(wired, monkeypatch):
    db_path, series = wired
    db.queue_issue(series, 1.0, db_path)
    qid = _qid(db_path, series, 1.0)
    _set_state(db_path, qid, "pending_torrent", torrent_hash="abc123")
    deleted = []

    class QB:
        def delete_torrent(self, h, delete_files=True):
            deleted.append((h, delete_files))
            return True
    from kometa import sources
    monkeypatch.setattr(sources, "qbittorrent", lambda: QB())
    _press_x(qid)
    assert deleted == [("abc123", True)]


def test_a_row_placing_its_files_is_past_stopping(wired):
    db_path, series = wired
    db.queue_issue(series, 1.0, db_path)
    qid = _qid(db_path, series, 1.0)
    _set_state(db_path, qid, "processing")
    with pytest.raises(HTTPException) as e:
        _press_x(qid)
    assert e.value.status_code == 409
    assert any(q["id"] == qid for q in db.get_queue(db_path)), "the row went anyway"


def test_x_on_an_idle_row_touches_no_job(wired, monkeypatch):
    """A queued row can still carry an old job id from an earlier attempt."""
    db_path, series = wired
    db.queue_issue(series, 1.0, db_path)
    qid = _qid(db_path, series, 1.0)
    _set_state(db_path, qid, "queued", sab_nzo_id="old-job")
    monkeypatch.setattr(acq, "_sabnzbd", lambda: (_ for _ in ()).throw(AssertionError("touched SABnzbd")))
    _press_x(qid)
    assert not acq._stop_requested
    assert all(q["id"] != qid for q in db.get_queue(db_path))


def test_a_fresh_retry_forgets_the_issues_remembered_failures_too(wired, monkeypatch):
    """fresh=1 means the rejection's cause was fixed. It used to clear only the
    row's own list while the per-issue memory kept excluding the same releases —
    Dark Ark #2–10, refused by a page check that misread merged spreads."""
    db_path, series = wired
    db.queue_issue(series, 2.0, db_path)
    db.queue_issue(series, 3.0, db_path)
    two, three = _qid(db_path, series, 2.0), _qid(db_path, series, 3.0)
    db.add_failed_source(two, "https://getcomics.org/dls/two", path=db_path)
    db.add_failed_source(three, "https://getcomics.org/dls/three", path=db_path)
    _set_state(db_path, two, "failed")
    monkeypatch.setattr("kometa.main._process_queue", lambda: None)
    from kometa.main import retry_queue_item

    retry_queue_item(two, fresh=1)
    assert db.failed_releases_for(series, 2.0, db_path) == set()
    assert db.failed_releases_for(series, 3.0, db_path) == {"https://getcomics.org/dls/three"}
    item = next(i for i in db.get_queued_items(db_path) if i["id"] == two)
    assert acq._failed_sources(item) == set()


def test_a_plain_retry_still_remembers(wired, monkeypatch):
    db_path, series = wired
    db.queue_issue(series, 2.0, db_path)
    two = _qid(db_path, series, 2.0)
    db.add_failed_source(two, "https://getcomics.org/dls/two", path=db_path)
    monkeypatch.setattr("kometa.main._process_queue", lambda: None)
    from kometa.main import retry_queue_item
    retry_queue_item(two)
    assert db.failed_releases_for(series, 2.0, db_path) == {"https://getcomics.org/dls/two"}
