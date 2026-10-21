"""The full-library retag batch (kometa/retag_batch.py, enrichment 3f). Everything on
throwaway trees under tmp_path: a fake /comics, a fake /retag with its snapshot, tiny
STORED CBZs whose pages are fixed-length bytes (so a different page can keep the
same file size), and a stubbed RAR read path."""
import hashlib
import os
import shutil
import time
import zipfile

import pytest
from fastapi import HTTPException

import kometa.db as db
from kometa import comicinfo, downloader, reader, record, retag, retag_batch as rb

OLD_CI = b"<?xml version='1.0'?><ComicInfo><Series>Saga</Series><Notes>scanned by ZZZ</Notes></ComicInfo>"


def _pages(seed: int):
    return [(f"p{i:02d}.jpg", bytes([seed + i]) * 300) for i in range(3)]


def _cbz(p, pages):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("ComicInfo.xml", OLD_CI)
        for name, data in pages:
            zf.writestr(name, data)
    return p


def _tree(root):
    out = {}
    for r, _ds, fs in os.walk(root):
        for f in fs:
            p = os.path.join(r, f)
            out[p] = (os.stat(p).st_mtime_ns, hashlib.sha256(open(p, "rb").read()).hexdigest())
    return out


def _snap(bat, nas_path):
    return os.path.join(bat["source"], os.path.relpath(nas_path, bat["root"]))


@pytest.fixture
def bat(tmp_path, db_path, series, monkeypatch):
    """A comics root with two tracked CBZs (records, books rows), and /retag with
    an rsync -a style snapshot of it (copy2 keeps the mtimes)."""
    root = tmp_path / "comics"
    folder = root / "Image" / "Saga (2012)"
    batch_root = tmp_path / "retag"
    monkeypatch.setattr(reader, "PAGE_CACHE_DIR", str(tmp_path / "page-cache"))
    monkeypatch.setattr(retag, "DB_PATH", db_path)
    monkeypatch.setattr(rb, "DB_PATH", db_path)
    monkeypatch.setattr(rb, "BATCH_ROOT", str(batch_root))
    monkeypatch.setattr(downloader, "_rar_entry_count", lambda p: None)
    # plenty of room: the build's disk-full stop read the machine running the tests, and a
    # laptop under 20 GB free failed 13 of these (2026-10-22). The stop has its own test below.
    monkeypatch.setattr(rb, "_free_bytes", lambda: 1 << 40)
    from kometa import sources
    monkeypatch.setattr(sources, "comics_root", lambda: str(root))
    rb._STOP.clear()
    ids, files = [], []
    for n in (1, 2):
        f = _cbz(str(folder / f"Saga #00{n}.cbz"), _pages(10 * n))
        record.write_issue(series, float(n), {"desc": f"Issue {n} happens.", "cover_date": "2012-03-01"},
                           "metron", "full", path=db_path)
        st = os.stat(f)
        ids.append(db.upsert_book(f, st.st_size, st.st_mtime, [[name, 8, 12] for name, _d in _pages(0)],
                                  tracked_series_id=series, number=float(n), path=db_path))
        files.append(f)
    shutil.copytree(root, batch_root / "source")
    return {"root": str(root), "folder": str(folder), "db": db_path, "ids": ids, "files": files,
            "series": series, "batch_root": str(batch_root), "source": str(batch_root / "source"),
            "build": str(batch_root / "build"), "tmp": tmp_path}


def _queue(bat):
    return rb.batch_queue(bat["db"])


def _build(bat, **kw):
    return rb.batch_build(bat["db"], root=bat["root"], workers=2, **kw)


def _swap(bat, **kw):
    return rb.batch_swap(bat["db"], root=bat["root"], yield_to_reader=lambda sleep=None: False, **kw)


def _spy_builds(monkeypatch):
    read = []
    real = retag.build_one

    def spy(book, *a, **k):
        read.append(book["path"])
        return real(book, *a, **k)
    monkeypatch.setattr(retag, "build_one", spy)
    return read


# --- build: where the bytes come from ------------------------------------------------
def test_build_reads_the_snapshot_and_journals_the_nas_stat(bat, monkeypatch):
    read = _spy_builds(monkeypatch)
    _queue(bat)
    out = _build(bat)
    assert out == {"from_snapshot": 2, "from_nas": 0, "stopped": None}
    assert sorted(read) == sorted(_snap(bat, f) for f in bat["files"])
    for bid, f in zip(bat["ids"], bat["files"]):
        j = retag.journal(bid, bat["db"])
        st = os.stat(f)
        assert j["state"] == "verified" and j["build_source"] == "snapshot"
        assert (j["old_size"], j["old_mtime"]) == (st.st_size, st.st_mtime)          # the NAS file's, not the copy's
        staged = os.path.join(bat["build"], f"{bid}.cbz")
        retag.verify(staged, retag._want(j))


def test_build_stops_before_the_disk_is_full(bat, monkeypatch):
    monkeypatch.setattr(rb, "_free_bytes", lambda: rb.MIN_FREE_BYTES - 1)
    _queue(bat)
    out = _build(bat)
    assert out["from_snapshot"] == 0 and out["from_nas"] == 0
    assert "nearly full" in out["stopped"]
    assert not os.listdir(bat["build"]) if os.path.isdir(bat["build"]) else True


def test_build_falls_back_to_the_nas_when_the_copy_differs(bat, monkeypatch):
    read = _spy_builds(monkeypatch)
    a, b = bat["files"]
    # a: the copy's mtime is off by a minute → NAS
    st = os.stat(_snap(bat, a))
    os.utime(_snap(bat, a), (st.st_atime, st.st_mtime - 60))
    # b: same size, same mtime, a different page — only the central directory tells
    st_b = os.stat(b)
    _cbz(_snap(bat, b), [(n, bytes([99]) * 300) if n == "p01.jpg" else (n, d) for n, d in _pages(20)])
    os.utime(_snap(bat, b), (st_b.st_atime, st_b.st_mtime))
    assert os.path.getsize(_snap(bat, b)) == st_b.st_size
    _queue(bat)
    out = _build(bat)
    assert out["from_nas"] == 2 and out["from_snapshot"] == 0
    assert sorted(read) == sorted(bat["files"])
    for bid in bat["ids"]:
        j = retag.journal(bid, bat["db"])
        assert j["state"] == "verified" and j["build_source"] == "nas"
    with zipfile.ZipFile(os.path.join(bat["build"], f"{bat['ids'][1]}.cbz")) as zf:
        assert zf.read("p01.jpg") == _pages(20)[1][1]                                 # the NAS page, not the copy's


def test_a_missing_copy_falls_back(bat, monkeypatch):
    os.remove(_snap(bat, bat["files"][0]))
    _queue(bat)
    assert _build(bat)["from_nas"] == 1


def test_build_refuses_a_missing_or_empty_source(bat):
    _queue(bat)
    shutil.rmtree(bat["source"])
    with pytest.raises(rb.BatchRefused, match="doesn't exist"):
        _build(bat)
    os.makedirs(bat["source"])
    with pytest.raises(rb.BatchRefused, match="empty"):
        _build(bat)
    assert all(retag.journal(b, bat["db"])["state"] == "pending" for b in bat["ids"])


def test_build_never_writes_outside_retag(bat, monkeypatch):
    cbr = os.path.join(bat["folder"], "Saga #003.cbr")
    open(cbr, "wb").write(b"Rar!\x1a\x07\x01\x00 not really a rar")
    record.write_issue(bat["series"], 3.0, {"desc": "Three."}, "metron", "full", path=bat["db"])
    st = os.stat(cbr)
    db.upsert_book(cbr, st.st_size, st.st_mtime, [], tracked_series_id=bat["series"], number=3.0, path=bat["db"])
    shutil.copy2(cbr, _snap(bat, cbr))
    monkeypatch.setattr(downloader, "_read_archive_pages", lambda p, e=None: iter(_pages(30)))
    os.utime(bat["files"][1], (1, 1))                                                  # one falls back to the NAS
    with db._connect(bat["db"]) as conn:
        conn.execute("UPDATE books SET mtime = 1 WHERE path = ?", (bat["files"][1],))
    before = _tree(bat["root"])
    _queue(bat)
    out = _build(bat)
    assert out["from_nas"] == 1 and out["from_snapshot"] == 2
    assert _tree(bat["root"]) == before                                                # byte- and mtime-identical
    assert retag.summary(bat["db"]) == {"verified": 3}


# --- swap ----------------------------------------------------------------------------
def test_swap_writes_verifies_keeps_the_books_and_drops_the_build(bat):
    bid, f = bat["ids"][0], bat["files"][0]
    db.set_progress("me", bid, 2, False, "2026-10-12T00:00:00Z", bat["db"])
    old_mtime = os.stat(f).st_mtime
    _queue(bat)
    _build(bat)
    assert _swap(bat) == {"stopped": None}
    with zipfile.ZipFile(f) as zf:
        names = zf.namelist()
        assert names.count("ComicInfo.xml") == 1 and b"Issue 1 happens." in zf.read("ComicInfo.xml")
        assert [(n, zf.read(n)) for n in names if n != "ComicInfo.xml"] == _pages(10)
    st = os.stat(f)
    assert st.st_mtime == old_mtime
    b = db.get_book(bid, bat["db"])
    assert (b["size"], b["mtime"]) == (st.st_size, st.st_mtime)
    assert db.get_progress("me", bid, bat["db"])["page"] == 2
    j = retag.journal(bid, bat["db"])
    assert j["state"] == "swapped" and j["new_size"] == st.st_size
    assert not os.listdir(bat["build"])                                                # staged builds gone
    assert not [n for n in os.listdir(bat["folder"]) if n.endswith(retag.TMP_SUFFIX)]


def test_swap_turns_a_cbr_into_a_cbz_and_bins_the_cbr(bat, monkeypatch):
    cbr = os.path.join(bat["folder"], "Saga #003.cbr")
    open(cbr, "wb").write(b"Rar!\x1a\x07\x01\x00 not really a rar")
    record.write_issue(bat["series"], 3.0, {"desc": "Three."}, "metron", "full", path=bat["db"])
    st = os.stat(cbr)
    bid = db.upsert_book(cbr, st.st_size, st.st_mtime, [], tracked_series_id=bat["series"], number=3.0, path=bat["db"])
    shutil.copy2(cbr, _snap(bat, cbr))
    monkeypatch.setattr(downloader, "_read_archive_pages", lambda p, e=None: iter(_pages(30)))
    _queue(bat)
    _build(bat)
    rep = rb.batch_report(path=bat["db"])
    assert rep["cbr_to_cbz"]["total"] == 1 and rep["cbr_to_cbz"]["rows"][0]["book_id"] == bid
    _swap(bat)
    assert not os.path.exists(cbr) and os.path.exists(cbr[:-4] + ".cbz")
    binned = os.path.join(bat["root"], "_trash", "Image", f"retag-cbr-{time.strftime('%Y-%m-%d')}", "Saga (2012)", "Saga #003.cbr")
    assert os.path.exists(binned) and db.get_book(bid, bat["db"])["path"] == cbr[:-4] + ".cbz"


def test_swap_marks_a_changed_original_stale_and_leaves_it(bat):
    _queue(bat)
    _build(bat)
    f = bat["files"][0]
    with open(f, "ab") as fh:
        fh.write(b"someone else's write")
    sha = hashlib.sha256(open(f, "rb").read()).hexdigest()
    _swap(bat)
    j = retag.journal(bat["ids"][0], bat["db"])
    assert j["state"] == "stale" and "changed" in j["error"]
    assert hashlib.sha256(open(f, "rb").read()).hexdigest() == sha                      # never overwritten
    assert retag.journal(bat["ids"][1], bat["db"])["state"] == "swapped"
    assert not os.listdir(bat["build"])
    # a stale row is a re-queue away from another go
    assert _queue(bat)["queued"] == 1 and retag.journal(bat["ids"][0], bat["db"])["state"] == "pending"


def test_swap_refuses_without_the_build_dir(bat):
    _queue(bat)
    with pytest.raises(rb.BatchRefused):
        _swap(bat)


# --- resumability ----------------------------------------------------------------------
def test_build_resumes_and_rebuilds_only_what_is_gone(bat, monkeypatch):
    _queue(bat)
    _build(bat)
    read = _spy_builds(monkeypatch)
    assert _build(bat)["stopped"] is None and read == []                                # all verified, builds on disk
    os.remove(os.path.join(bat["build"], f"{bat['ids'][1]}.cbz"))
    _build(bat)
    assert read == [_snap(bat, bat["files"][1])]
    assert retag.summary(bat["db"]) == {"verified": 2}


def test_stop_finishes_the_file_in_hand_and_the_next_run_carries_on(bat, monkeypatch):
    _queue(bat)
    real = retag.build_one

    def stop_after(*a, **k):
        out = real(*a, **k)
        rb._STOP.set()
        return out
    monkeypatch.setattr(retag, "build_one", stop_after)
    out = rb.batch_build(bat["db"], root=bat["root"], workers=1)
    assert out["stopped"] == "stop requested" and retag.summary(bat["db"]) == {"verified": 1, "pending": 1}
    monkeypatch.setattr(retag, "build_one", real)
    rb._STOP.clear()
    _build(bat)
    assert retag.summary(bat["db"]) == {"verified": 2}


def test_swap_resumes_after_a_crash_past_the_rename(bat, monkeypatch):
    _queue(bat)
    _build(bat)
    real = retag._finish
    monkeypatch.setattr(retag, "_finish", lambda *a, **k: (_ for _ in ()).throw(retag.PostSwapError("db gone")))
    out = _swap(bat)
    assert out["stopped"] and "db gone" in out["stopped"]
    assert retag.journal(bat["ids"][0], bat["db"])["state"] == "verified"
    monkeypatch.setattr(retag, "_finish", real)
    assert _swap(bat)["stopped"] is None
    assert retag.summary(bat["db"]) == {"swapped": 2} and not os.listdir(bat["build"])
    with zipfile.ZipFile(bat["files"][0]) as zf:
        assert zf.namelist().count("ComicInfo.xml") == 1


# --- scope ----------------------------------------------------------------------------
def test_queue_requeues_a_swapped_row_only_when_its_xml_changed(bat):
    _queue(bat)
    _build(bat)
    _swap(bat)
    dc = rb.dry_count(bat["db"])
    assert dc["tracked_books"] == 2 and dc["would_queue"] == 0 and dc["unchanged_done"] == 2
    assert _queue(bat)["queued"] == 0 and retag.summary(bat["db"]) == {"swapped": 2}
    record.write_issue(bat["series"], 2.0, {"desc": "A new description."}, "metron", "full", path=bat["db"])
    assert rb.dry_count(bat["db"])["would_requeue_changed"] == 1
    assert _queue(bat)["queued"] == 1
    assert retag.journal(bat["ids"][1], bat["db"])["state"] == "pending"
    assert retag.journal(bat["ids"][0], bat["db"])["state"] == "swapped"


def test_dry_count_splits_catalogue_from_stand_in(bat):
    record.write_issue(bat["series"], 2.0, {"desc": "from the file"}, "comicinfo", "partial", path=bat["db"])
    dc = rb.dry_count(bat["db"])
    assert dc["would_queue"] == 2 and dc["queued_by_data"] == {"catalogue": 1, "stopgap": 1}
    assert retag.summary(bat["db"]) == {}                                               # read-only


# --- the check --------------------------------------------------------------------------
def test_report_and_staged_comicinfo(bat):
    _queue(bat)
    _build(bat)
    rep = rb.batch_report(sample=5, path=bat["db"])
    assert rep["states"] == {"verified": 2} and rep["build_source"] == {"snapshot": 2}
    assert rep["staged"]["files"] == 2 and rep["staged"]["bytes"] > 0 and rep["disk"]["free"] > 0
    assert rep["source"]["ok"] and len(rep["sample"]) == 2 and rep["problems"]["total"] == 0
    bid = rep["sample"][0]["book_id"]
    r = rb.route_staged_comicinfo(bid)
    assert r.body == comicinfo.for_book(db.get_book(bid, bat["db"]), bat["db"], 3)
    assert r.headers["x-comicinfo-digest"] == r.headers["x-journal-digest"]
    with pytest.raises(HTTPException):
        rb.route_staged_comicinfo(99999)


# --- controls ---------------------------------------------------------------------------
def _wait_idle():
    for _ in range(500):
        if not rb.running():
            return
        time.sleep(0.01)
    raise AssertionError("the batch phase never finished")


def test_every_route_refuses_unless_armed(bat):
    before = _tree(bat["root"])
    for route in (rb.route_queue, rb.route_build, rb.route_swap):
        with pytest.raises(HTTPException) as e:
            route()
        assert e.value.status_code == 403
    assert retag.summary(bat["db"]) == {} and _tree(bat["root"]) == before
    db.set_config({"retag_batch_armed": "0"}, bat["db"])
    with pytest.raises(HTTPException):
        rb.route_queue()
    rb.route_stop()                                                                     # stop always answers


def test_armed_routes_run_one_phase_at_a_time(bat):
    db.set_config({"retag_batch_armed": "1"}, bat["db"])
    rb.route_queue()
    _wait_idle()
    assert retag.summary(bat["db"]) == {"pending": 2}
    rb.route_build()
    _wait_idle()
    assert retag.summary(bat["db"]) == {"verified": 2}
    assert rb.route_report()["progress"]["phase"] == "build"
    assert retag.JOB_LOCK.acquire(blocking=False)                                         # a running tick
    try:
        with pytest.raises(HTTPException) as e:
            rb.route_swap()
        assert e.value.status_code == 409
    finally:
        retag.JOB_LOCK.release()
    rb.route_swap()
    _wait_idle()
    assert retag.summary(bat["db"]) == {"swapped": 2}


def test_build_route_refuses_without_a_source(bat):
    db.set_config({"retag_batch_armed": "1"}, bat["db"])
    shutil.rmtree(bat["source"])
    with pytest.raises(HTTPException) as e:
        rb.route_build()
    assert e.value.status_code == 409 and "doesn't exist" in e.value.detail


def test_the_tick_stands_down_while_armed_or_running(bat):
    db.set_config({"retag_enabled": "1", "retag_batch_armed": "1"}, bat["db"])
    _queue(bat)
    def boom(ids):
        raise AssertionError("the tick ran during the batch")
    assert retag.retag_tick(path=bat["db"], runner=boom)["skipped"] == "the retag batch is armed"
    db.set_config({"retag_batch_armed": "0"}, bat["db"])
    assert retag.JOB_LOCK.acquire(blocking=False)                                         # a phase in flight
    try:
        assert retag.retag_tick(path=bat["db"], runner=boom)["skipped"] == "a retag batch phase is running"
    finally:
        retag.JOB_LOCK.release()
    seen = []
    retag.retag_tick(path=bat["db"], runner=lambda ids: seen.append(ids) or {"counts": {}, "stopped": None})
    assert sorted(seen[0]) == sorted(bat["ids"])                                         # free again: the tick runs
