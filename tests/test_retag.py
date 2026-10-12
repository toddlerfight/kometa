"""The ComicInfo retag job (kometa/retag.py, enrichment 3b–3d). Everything here runs
on throwaway copies under tmp_path: tiny real CBZs, a stubbed RAR read path."""
import io
import os
import time
import zipfile
import hashlib

import pytest
from PIL import Image

import kometa.db as db
from kometa import comicinfo, downloader, reader, record, retag, trash

OLD_CI = b"<?xml version='1.0'?><ComicInfo><Series>Saga Vol. 1</Series><Notes>scanned by ZZZ</Notes></ComicInfo>"


def _img(fmt="JPEG", color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (8, 12), color).save(buf, fmt)
    return buf.getvalue()


PAGES = [("Saga 001/p01.jpg", _img()), ("Saga 001/p02.png", _img("PNG", (0, 90, 0))),
         ("Saga 001/p03.jpg", _img(color=(1, 2, 3)))]


def _messy_cbz(p, pages=PAGES, extra=True):
    """Deflated, an uploader's ComicInfo at the root AND a lower-case one in a subfolder, a stray txt."""
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ComicInfo.xml", OLD_CI)
        for name, data in pages:
            zf.writestr(name, data)
        if extra:
            zf.writestr("Saga 001/comicinfo.xml", OLD_CI)
            zf.writestr("Saga 001/zz-scanner.txt", b"ripped by ZZZ")
    return p


def _entries(p):
    with zipfile.ZipFile(p) as zf:
        return [(i.filename, zf.read(i), i.compress_type) for i in zf.infolist()]


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


@pytest.fixture
def lib(tmp_path, db_path, series, monkeypatch):
    """A comics root with one tracked CBZ, its record, its books row and a page cache."""
    root = tmp_path / "comics"
    folder = root / "Image" / "Saga (2012)"
    f = _messy_cbz(str(folder / "Saga #001.cbz"))
    monkeypatch.setattr(reader, "PAGE_CACHE_DIR", str(tmp_path / "page-cache"))
    monkeypatch.setattr(retag, "DB_PATH", db_path)
    monkeypatch.setattr(downloader, "_rar_entry_count", lambda p: None)     # no lsar on fixtures
    record.write_issue(series, 1.0, {"desc": "The war goes on.", "credits": [{"role": "Writer", "name": "Brian K. Vaughan"}],
                                     "cover_date": "2012-03-01"}, "metron", "full", path=db_path)
    st = os.stat(f)
    pages = [[n, 8, 12] for n, _d in PAGES]
    bid = db.upsert_book(f, st.st_size, st.st_mtime, pages, tracked_series_id=series, number=1.0, path=db_path)
    return {"root": str(root), "file": f, "folder": str(folder), "book_id": bid, "db": db_path,
            "staging": str(tmp_path / "staging"), "series": series, "tmp": tmp_path}


def _run(lib, ids=None, **kw):
    return retag.run_batch(ids or [lib["book_id"]], lib["staging"], root=lib["root"], path=lib["db"],
                           yield_to_reader=lambda sleep=None: False, **kw)


def _no_tmp_left(folder):
    return not [n for n in os.listdir(folder) if n.endswith(retag.TMP_SUFFIX)]


# --- the rewrite ------------------------------------------------------------------
def test_pages_are_byte_identical_and_only_the_new_comicinfo_remains(lib):
    before = _entries(lib["file"])
    old_mtime = os.stat(lib["file"]).st_mtime
    out = _run(lib)
    assert out["counts"] == {"swapped": 1} and out["stopped"] is None
    after = _entries(lib["file"])
    keep = [(n, d) for n, d, _c in before if not n.lower().endswith("comicinfo.xml")]
    assert [(n, d) for n, d, _c in after if n != "ComicInfo.xml"] == keep         # same names, same order, same bytes
    assert all(c == zipfile.ZIP_STORED for _n, _d, c in after)
    cis = [(n, d) for n, d, _c in after if n.lower().endswith("comicinfo.xml")]
    assert len(cis) == 1 and cis[0][0] == "ComicInfo.xml"                            # the subfolder one is gone too
    want = comicinfo.for_issue(lib["series"], 1.0, lib["db"], 3)
    assert cis[0][1] == want and b"Brian K. Vaughan" in want and b"scanned by ZZZ" not in cis[0][1]
    assert os.stat(lib["file"]).st_mtime == old_mtime                                # On Deck's order survives
    assert _no_tmp_left(lib["folder"]) and not os.listdir(lib["staging"])


def test_book_id_kept_row_updated_and_page_cache_carried(lib):
    bid, f = lib["book_id"], lib["file"]
    db.set_progress("me", bid, 2, False, "2026-10-12T00:00:00Z", lib["db"])
    old = db.get_book(bid, lib["db"])
    old_ver = reader._version(old["size"], old["mtime"])
    page_dir = os.path.join(reader.PAGE_CACHE_DIR, str(bid), old_ver)
    os.makedirs(page_dir)
    open(os.path.join(page_dir, "1_1440.jpg"), "wb").write(b"rendered")
    cover = reader._cover_cache_path(f, old_ver)
    os.makedirs(os.path.dirname(cover))
    open(cover, "wb").write(b"cover")
    _run(lib)
    new = db.get_book_by_path(f, lib["db"])
    st = os.stat(f)
    assert new["id"] == bid and (new["size"], new["mtime"]) == (st.st_size, st.st_mtime) and new["size"] != old["size"]
    assert new["pages"] == old["pages"]                                              # no rescan needed
    assert db.get_progress("me", bid, lib["db"])["page"] == 2
    new_ver = reader._version(st.st_size, st.st_mtime)
    assert open(os.path.join(reader.PAGE_CACHE_DIR, str(bid), new_ver, "1_1440.jpg"), "rb").read() == b"rendered"
    assert not os.path.exists(page_dir)
    assert open(reader._cover_cache_path(f, new_ver), "rb").read() == b"cover"
    j = retag.journal(bid, lib["db"])
    assert j["state"] == "swapped" and (j["new_size"], j["new_mtime"]) == (st.st_size, st.st_mtime)


# --- verify -------------------------------------------------------------------------
def _built(lib):
    row = retag._start(db.get_book(lib["book_id"], lib["db"]), lib["db"])
    b = retag.build_one(db.get_book(lib["book_id"], lib["db"]), lib["staging"], row, lib["db"])
    retag.verify(b["staged"], b)                                                     # the honest build passes
    return b


def _rewrite(src, dst, edit):
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_STORED) as zo:
        for name, data in edit([(i.filename, zi.read(i)) for i in zi.infolist()]):
            zo.writestr(name, data)


def test_verify_catches_a_flipped_byte(lib):
    b = _built(lib)
    raw = bytearray(open(b["staged"], "rb").read())
    i = raw.index(PAGES[0][1][20:40])                                                # inside page 1's data
    raw[i] ^= 0xFF
    open(b["staged"], "wb").write(bytes(raw))
    with pytest.raises(retag.VerifyFailed):
        retag.verify(b["staged"], b)


@pytest.mark.parametrize("edit", [
    lambda es: [e for e in es if not e[0].endswith("p02.png")],                    # a page missing
    lambda es: es[:1] + [("Saga 001/p01b.jpg", _img())] + es[1:],                  # a page extra
    lambda es: [es[1], es[0]] + es[2:],                                              # two pages swapped round
    lambda es: es + [("Saga 001/ComicInfo.xml", b"<ComicInfo/>")],                 # a second ComicInfo
])
def test_verify_catches_a_wrong_page_list(lib, edit):
    b = _built(lib)
    bad = b["staged"] + ".bad"
    _rewrite(b["staged"], bad, edit)
    with pytest.raises(retag.VerifyFailed):
        retag.verify(bad, b)


def test_a_corrupt_staged_build_is_never_swapped(lib, monkeypatch):
    orig = _sha(lib["file"])
    real_build = retag.build_one

    def build_then_corrupt(*a, **k):
        b = real_build(*a, **k)
        raw = bytearray(open(b["staged"], "rb").read())
        raw[raw.index(PAGES[2][1][20:40])] ^= 0x01
        open(b["staged"], "wb").write(bytes(raw))
        return b
    monkeypatch.setattr(retag, "build_one", build_then_corrupt)
    out = _run(lib)
    assert out["counts"] == {"failed": 1}
    assert _sha(lib["file"]) == orig and _no_tmp_left(lib["folder"])
    assert "CRC" in retag.journal(lib["book_id"], lib["db"])["error"]


# --- the swap -----------------------------------------------------------------------
def test_a_failed_rename_leaves_the_original_and_no_temp(lib, monkeypatch):
    orig, real = _sha(lib["file"]), os.replace

    def replace(src, dst):
        if str(src).endswith(retag.TMP_SUFFIX):
            assert os.path.exists(src)                                              # the tmp WAS written and read back
            raise OSError(5, "Input/output error")
        return real(src, dst)
    monkeypatch.setattr(retag.os, "replace", replace)
    out = _run(lib)
    assert out["counts"] == {"failed": 1}
    assert _sha(lib["file"]) == orig and _no_tmp_left(lib["folder"])
    j = retag.journal(lib["book_id"], lib["db"])
    assert j["state"] == "failed" and "swap failed" in j["error"]
    assert db.get_book(lib["book_id"], lib["db"])["size"] == os.path.getsize(lib["file"])


def test_a_bad_read_back_stops_before_the_swap(lib, monkeypatch):
    orig, real = _sha(lib["file"]), retag.verify

    def verify(p, want):
        if p.endswith(retag.TMP_SUFFIX):
            raise retag.VerifyFailed("read back differs")
        return real(p, want)
    monkeypatch.setattr(retag, "verify", verify)
    assert _run(lib)["counts"] == {"failed": 1}
    assert _sha(lib["file"]) == orig and _no_tmp_left(lib["folder"])


def test_a_source_changed_after_the_build_is_refused(lib, monkeypatch):
    real = retag.build_one

    def build_then_touch(*a, **k):
        b = real(*a, **k)
        with open(lib["file"], "ab") as fh:
            fh.write(b"\0")                                                          # someone re-downloaded it
        return b
    monkeypatch.setattr(retag, "build_one", build_then_touch)
    assert _run(lib)["counts"] == {"failed": 1}
    assert "changed" in retag.journal(lib["book_id"], lib["db"])["error"]


def test_zero_pages_is_refused(lib):
    with zipfile.ZipFile(lib["file"], "w") as zf:
        zf.writestr("ComicInfo.xml", OLD_CI)
    orig = _sha(lib["file"])
    assert _run(lib)["counts"] == {"failed": 1}
    assert "no image entries" in retag.journal(lib["book_id"], lib["db"])["error"] and _sha(lib["file"]) == orig


# --- resume ---------------------------------------------------------------------------
def test_a_second_run_does_nothing(lib):
    _run(lib)
    sha, st = _sha(lib["file"]), os.stat(lib["file"])
    out = _run(lib)
    assert out["counts"] == {"swapped": 1} and out["items"][0]["was"] == "swapped"
    assert _sha(lib["file"]) == sha and os.stat(lib["file"]).st_mtime_ns == st.st_mtime_ns


class Crash(Exception):
    """Something the batch has never heard of: it must stop, not plough on."""


def test_a_built_row_resumes_at_verify(lib, monkeypatch):
    real_verify = retag.verify
    monkeypatch.setattr(retag, "verify", lambda p, w: (_ for _ in ()).throw(Crash("power cut")))
    out = _run(lib)
    assert out["stopped"] and "power cut" in out["stopped"]
    assert retag.journal(lib["book_id"], lib["db"])["state"] == "built"
    builds = []
    real_build = retag.build_one
    monkeypatch.setattr(retag, "build_one", lambda *a, **k: builds.append(1) or real_build(*a, **k))
    monkeypatch.setattr(retag, "verify", real_verify)
    assert _run(lib)["counts"] == {"swapped": 1} and builds == []                    # verified the staged file, no rebuild


def test_a_crash_after_the_rename_is_finished_not_redone(lib, monkeypatch):
    real_finish = retag._finish
    monkeypatch.setattr(retag, "_finish", lambda *a, **k: (_ for _ in ()).throw(retag.PostSwapError("db gone")))
    out = _run(lib)
    assert out["stopped"] and retag.journal(lib["book_id"], lib["db"])["state"] == "verified"
    sha = _sha(lib["file"])                                                          # the new file is in place
    monkeypatch.setattr(retag, "_finish", real_finish)
    assert _run(lib)["counts"] == {"swapped": 1}
    assert _sha(lib["file"]) == sha
    b = db.get_book(lib["book_id"], lib["db"])
    assert (b["size"], b["mtime"]) == (os.path.getsize(lib["file"]), os.stat(lib["file"]).st_mtime)


def test_an_unknown_error_stops_the_batch(lib, monkeypatch):
    f2 = _messy_cbz(os.path.join(lib["folder"], "Saga #002.cbz"))
    st = os.stat(f2)
    b2 = db.upsert_book(f2, st.st_size, st.st_mtime, [], tracked_series_id=lib["series"], number=2.0, path=lib["db"])
    monkeypatch.setattr(retag, "build_one", lambda *a, **k: (_ for _ in ()).throw(Crash("disk on fire")))
    out = _run(lib, [lib["book_id"], b2])
    assert "disk on fire" in out["stopped"] and retag.journal(b2, lib["db"]) is None


def test_failures_in_a_row_stop_the_batch(lib, monkeypatch):
    ids = []
    for n in range(2, 7):
        f = _messy_cbz(os.path.join(lib["folder"], f"Saga #00{n}.cbz"))
        st = os.stat(f)
        ids.append(db.upsert_book(f, st.st_size, st.st_mtime, [], tracked_series_id=lib["series"], number=1.0, path=lib["db"]))
    monkeypatch.setattr(retag, "build_one", lambda *a, **k: (_ for _ in ()).throw(retag.RetagError("nope")))
    out = _run(lib, ids)
    assert out["counts"] == {"failed": 3} and "in a row" in out["stopped"]


# --- skipped ---------------------------------------------------------------------------
def test_a_file_already_saying_it_is_skipped(lib):
    _run(lib)
    sha, mt = _sha(lib["file"]), os.stat(lib["file"]).st_mtime_ns
    with db._connect(lib["db"]) as conn:
        conn.execute("DELETE FROM retag_journal")                                   # a fresh pass over the library
    assert _run(lib)["counts"] == {"skipped": 1}
    assert _sha(lib["file"]) == sha and os.stat(lib["file"]).st_mtime_ns == mt
    assert "already carries" in retag.journal(lib["book_id"], lib["db"])["error"]


def test_no_record_is_skipped_not_failed(lib):
    f = _messy_cbz(os.path.join(lib["folder"], "Saga Extra.cbz"))
    st = os.stat(f)
    loose = db.upsert_book(f, st.st_size, st.st_mtime, [], path=lib["db"])           # no tracked issue
    unrecorded = db.upsert_book(_messy_cbz(os.path.join(lib["folder"], "Saga #009.cbz")), 1, 1, [],
                          tracked_series_id=lib["series"], number=9.0, path=lib["db"])
    sha = _sha(f)
    out = _run(lib, [loose, unrecorded])
    assert out["counts"] == {"skipped": 2} and _sha(f) == sha


# --- CBR → CBZ ---------------------------------------------------------------------------
def test_a_cbr_becomes_a_cbz_and_the_cbr_goes_to_the_trash(lib, monkeypatch):
    cbr = os.path.join(lib["folder"], "Saga #002.cbr")
    open(cbr, "wb").write(b"Rar!\x1a\x07\x01\x00 not really a rar")
    rar_pages = sorted([("p01.jpg", _img()), ("p02.jpg", _img(color=(9, 9, 9))), ("ComicInfo.xml", OLD_CI)])

    def read_pages(path, extracted_dir=None):
        assert path == cbr
        yield from rar_pages
    monkeypatch.setattr(downloader, "_read_archive_pages", read_pages)
    st = os.stat(cbr)
    bid = db.upsert_book(cbr, st.st_size, st.st_mtime, [["p01.jpg", 8, 12], ["p02.jpg", 8, 12]],
                         tracked_series_id=lib["series"], number=1.0, path=lib["db"])
    db.set_progress("me", bid, 1, False, "2026-10-12T00:00:00Z", lib["db"])
    cbr_bytes = open(cbr, "rb").read()
    out = _run(lib, [bid])
    assert out["counts"] == {"swapped": 1}
    cbz = cbr[:-4] + ".cbz"
    assert not os.path.exists(cbr) and os.path.exists(cbz)
    got = _entries(cbz)
    assert [(n, d) for n, d, _c in got if n != "ComicInfo.xml"] == [e for e in rar_pages if e[0] != "ComicInfo.xml"]
    assert [n for n, _d, _c in got].count("ComicInfo.xml") == 1
    binned = os.path.join(lib["root"], "_trash", "Image", f"retag-cbr-{time.strftime('%Y-%m-%d')}", "Saga (2012)", "Saga #002.cbr")
    assert open(binned, "rb").read() == cbr_bytes                                   # moved, never deleted
    b = db.get_book(bid, lib["db"])
    assert b["path"] == cbz and b["size"] == os.path.getsize(cbz)
    assert db.get_progress("me", bid, lib["db"])["page"] == 1
    assert retag.journal(bid, lib["db"])["trashed_to"] == binned
    # the trash's own purge sees it as one trashed folder, after its week
    assert trash.purge(lib["root"], now=time.time() + 8 * 86400) == 1 and not os.path.exists(binned)


def test_a_cbz_already_beside_the_cbr_is_never_clobbered(lib, monkeypatch):
    cbr = os.path.join(lib["folder"], "Saga #001.cbr")                              # Saga #001.cbz already exists
    open(cbr, "wb").write(b"Rar!\x1a\x07\x01\x00 stub")
    monkeypatch.setattr(downloader, "_read_archive_pages", lambda p, e=None: iter([("p01.jpg", _img())]))
    st = os.stat(cbr)
    bid = db.upsert_book(cbr, st.st_size, st.st_mtime, [], tracked_series_id=lib["series"], number=1.0, path=lib["db"])
    sha = _sha(lib["file"])
    assert _run(lib, [bid])["counts"] == {"failed": 1}
    assert _sha(lib["file"]) == sha and os.path.exists(cbr)


# --- dry run --------------------------------------------------------------------------
def _tree(root):
    out = {}
    for r, _ds, fs in os.walk(root):
        for f in fs:
            p = os.path.join(r, f)
            out[p] = (os.stat(p).st_mtime_ns, _sha(p))
    return out


def test_dry_run_writes_nothing_outside_staging(lib):
    lib_before, book_before = _tree(lib["root"]), db.get_book(lib["book_id"], lib["db"])
    cache_before = _tree(reader.PAGE_CACHE_DIR) if os.path.isdir(reader.PAGE_CACHE_DIR) else {}
    out = _run(lib, dry_run=True)
    item = out["items"][0]
    assert out["counts"] == {"rewrite": 1} and item["pages"] == 3 and item["new_path"] == lib["file"]
    assert _tree(lib["root"]) == lib_before and db.get_book(lib["book_id"], lib["db"]) == book_before
    assert (_tree(reader.PAGE_CACHE_DIR) if os.path.isdir(reader.PAGE_CACHE_DIR) else {}) == cache_before
    assert retag.journal(lib["book_id"], lib["db"]) is None
    assert not os.listdir(os.path.join(lib["staging"], "dry-run"))


# --- preview -------------------------------------------------------------------------
def test_preview_returns_the_xml_the_job_would_write(lib):
    from fastapi import HTTPException
    sha = _sha(lib["file"])
    r = retag.comicinfo_preview(lib["book_id"])
    assert r.media_type == "application/xml"
    assert r.body == comicinfo.for_issue(lib["series"], 1.0, lib["db"], 3)
    assert r.headers["x-comicinfo-digest"] == comicinfo.digest(r.body)
    with pytest.raises(HTTPException):
        retag.comicinfo_preview(99999)
    assert _sha(lib["file"]) == sha and retag.journal(lib["book_id"], lib["db"]) is None   # read-only


def test_a_running_shelf_scan_holds_the_swap_back(lib):
    from kometa import shelf
    orig = _sha(lib["file"])
    assert shelf._scan_lock.acquire(blocking=False)
    try:
        out = _run(lib, lock_timeout=0.01)
    finally:
        shelf._scan_lock.release()
    assert out["stopped"] and "shelf scan" in out["stopped"]
    assert retag.journal(lib["book_id"], lib["db"])["state"] == "verified"           # resumes, not failed
    assert _sha(lib["file"]) == orig and _no_tmp_left(lib["folder"])
    assert _run(lib)["counts"] == {"swapped": 1}
