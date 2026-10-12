"""Read local first (kometa/localfirst.py, enrichment step 5): record → our own
ComicInfo → online, behind config read_local_first (absent = off). No network:
every resolver is stubbed, and the ones that must not run raise."""
import os
import zipfile
from datetime import datetime, timezone

import pytest

import kometa.db as db
from kometa import activity, comicinfo, harvest, localfirst, record, retag

FOREIGN_CI = b"<?xml version='1.0'?><ComicInfo><Series>Saga Vol. 1</Series><Summary>From the scan.</Summary><Notes>scanned by ZZZ</Notes></ComicInfo>"
METRON = {"desc": "A space opera.", "credits": [{"role": "Writer", "name": "Brian K. Vaughan", "metron_creator_id": 7}],
          "arcs": ["Chapter One"], "store_date": "2012-03-14", "cover_date": "2012-03-01", "page_count": 44, "extra": {}}


def _boom(*a, **k):
    raise AssertionError("network asked")


def _cbz(p, ci: bytes | None):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with zipfile.ZipFile(p, "w") as zf:
        if ci is not None:
            zf.writestr("ComicInfo.xml", ci)
        zf.writestr("p01.jpg", b"\xff\xd8not-really")
    return p


@pytest.fixture
def lib(tmp_path, db_path, monkeypatch):
    sid = db.add_series(title="Saga", publisher="Image", year_began=2012, folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 1.0, "2012-03-14", 1, metron_issue_id=101, path=db_path)
    monkeypatch.setattr(retag, "DB_PATH", db_path)
    monkeypatch.setattr(localfirst, "_spawn", lambda fn: fn())          # the background refresh, inline
    monkeypatch.setattr(activity, "_book_pages", {})
    return {"db": db_path, "sid": sid, "folder": str(tmp_path / "comics" / "Image" / "Saga (2012)")}


def _issue(lib, n=1.0):
    return next(i for i in db.get_issues_for_series(lib["sid"], lib["db"]) if i["number"] == n)


def _book(lib, ci, name="Saga #001.cbz", n=1.0):
    f = _cbz(os.path.join(lib["folder"], name), ci)
    st = os.stat(f)
    return db.upsert_book(f, st.st_size, st.st_mtime, [], tracked_series_id=lib["sid"], number=n, path=lib["db"])


def _ours(lib, **row) -> bytes:
    """What retag would write into the file: comicinfo.build from a Metron row."""
    base = {"desc": "Hazel is born.", "number": 1.0, "source": "metron", "metron_issue_id": 101, "cv_issue_id": "300001",
            "locg_issue_id": "55", "cover_date": "2012-03-01", "page_count": 44, "isbn": None, "arcs": ["Chapter One"],
            "credits": [{"role": "Writer", "name": "Brian K. Vaughan"}, {"role": "Artist", "name": "Fiona Staples"},
                        {"role": "Cover", "name": "Fiona Staples"}, {"role": "Letterer", "name": "Fonografiks"}],
            "extra": {"gcd_id": 777, "upc": "70985301827400111", "characters": ["Alana", "Marko"], "teams": ["Landfall"],
                      "publisher": "Image", "series_type": "Single Issue", "age_rating": "Mature", "title": "Chapter One"}}
    base.update(row)
    return comicinfo.build(db.get_series_by_id(lib["sid"], lib["db"]), base)


# --- off: byte-for-byte today -----------------------------------------------------------
def test_off_by_default_is_todays_path(lib, monkeypatch):
    assert localfirst.enabled(lib["db"]) is False
    bid = _book(lib, _ours(lib))                             # our tags on the shelf — ignored while off
    monkeypatch.setattr(localfirst, "details", _boom)
    asked = []
    monkeypatch.setattr("kometa.issue_meta.details_for", lambda i, p, want_covers=False: asked.append(1) or {**METRON, "source": "metron"})
    d = record.details(_issue(lib), lib["db"])
    assert (d["source"], d["record"], asked) == ("metron", False, [1])
    assert record.get_issue(lib["sid"], 1.0, lib["db"])["source"] == "metron"
    assert localfirst.miss_count(lib["db"]) == 0 and localfirst.deferred(lib["db"]) == []
    assert retag.journal(bid, lib["db"]) is None               # nothing queued for a retag
    db.set_config({"read_local_first": "0"}, lib["db"])
    assert record.details(_issue(lib), lib["db"])["record"] is True and asked == [1]


def test_off_a_stopgap_row_still_refreshes_behind(lib, monkeypatch):
    record.write_issue(lib["sid"], 1.0, {"desc": "From the scan."}, "comicinfo", "partial", _issue(lib), lib["db"])
    started = []
    monkeypatch.setattr(record.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda s: started.append(1)})())
    d = record.details(_issue(lib), lib["db"])
    assert (d["source"], d["record"], started) == ("comicinfo", True, [1])
    assert localfirst.miss_count(lib["db"]) == 0


# --- on --------------------------------------------------------------------------------
@pytest.fixture
def on(lib):
    db.set_config({"read_local_first": "1"}, lib["db"])
    return lib


@pytest.mark.parametrize("source", ["metron", "locg", "gcd", "comicinfo-kometa"])
def test_on_a_catalogue_row_answers_with_no_network(on, monkeypatch, source):
    record.write_issue(on["sid"], 1.0, METRON, source, "full", _issue(on), on["db"])
    with db._connect(on["db"]) as c:          # old enough that today's path would refresh behind
        c.execute("UPDATE issue_record SET fetched_at = '2020-01-01 00:00:00'")
    for target in ("kometa.issue_meta.details_for", "kometa.issue_meta._metron", "kometa.issue_meta._locg",
                   "kometa.record.fill_issue", "kometa.covers.read_comicinfo"):
        monkeypatch.setattr(target, _boom)
    behind = []                                # old: answered from the record now, refreshed behind (2026-10-13)
    monkeypatch.setattr(localfirst, "_spawn", behind.append)
    monkeypatch.setattr(record.threading, "Thread", _boom)
    d = record.details(_issue(on), on["db"])
    assert (d["desc"], d["source"], d["record"]) == ("A space opera.", source, True)
    assert len(behind) == 1
    assert localfirst.miss_count(on["db"]) == 0


def test_on_our_comicinfo_is_read_back_into_the_record(on, monkeypatch):
    _book(on, _ours(on))
    monkeypatch.setattr("kometa.issue_meta.details_for", _boom)
    d = record.details(_issue(on), on["db"])
    assert (d["source"], d["record"], d["desc"]) == ("comicinfo-kometa", True, "Hazel is born.")
    assert {(c["role"], c["name"]) for c in d["credits"]} >= {("Writer", "Brian K. Vaughan"), ("Penciller", "Fiona Staples"),
                                                              ("Inker", "Fiona Staples"), ("Cover", "Fiona Staples")}
    row = record.get_issue(on["sid"], 1.0, on["db"])
    assert (row["fill_state"], row["metron_issue_id"], row["locg_issue_id"], row["cv_issue_id"], row["cover_date"]) == \
        ("full", 101, "55", "300001", "2012-03-01")
    assert row["extra"]["gcd_id"] == 777 and row["extra"]["kometa_from"] == "metron"
    assert row["extra"]["characters"] == ["Alana", "Marko"] and row["extra"]["upc"] == "70985301827400111"
    assert record._fresh(row)                                # catalogue-grade: no stopgap re-ask
    assert localfirst.miss_count(on["db"]) == 0
    # and next time it's step 1: the file isn't even opened
    monkeypatch.setattr("kometa.covers.read_comicinfo", _boom)
    assert record.details(_issue(on), on["db"])["source"] == "comicinfo-kometa"


def test_parse_ours_is_the_inverse_of_fields(lib):
    series = db.get_series_by_id(lib["sid"], lib["db"])
    xml = _ours(lib)
    back = comicinfo.parse_ours(xml)
    row = dict(back, number=1.0, source=comicinfo.OURS_SOURCE)
    assert comicinfo.build(series, row) == xml                # a retag of a read-back row writes the same file
    assert comicinfo.parse_ours(FOREIGN_CI) is None and not comicinfo.is_ours(FOREIGN_CI)
    assert comicinfo.parse_ours(b"<ComicInfo><Notes>Tagged by Kometa (collected edition). </Notes></ComicInfo>") is None
    assert comicinfo.parse_ours(b"not xml") is None


def test_on_a_foreign_comicinfo_is_not_ours_and_goes_online(on, monkeypatch):
    bid = _book(on, FOREIGN_CI)
    monkeypatch.setattr("kometa.issue_meta.details_for", lambda i, p, want_covers=False: {**METRON, "source": "metron"})
    d = record.details(_issue(on), on["db"])
    assert (d["source"], d["record"]) == ("metron", False)
    assert record.get_issue(on["sid"], 1.0, on["db"])["source"] == "metron"
    m = localfirst.misses(on["db"])
    assert m["total"] == 1 and m["by_reason"] == {"no record / foreign ComicInfo": 1}
    assert (m["misses"][0]["book_id"], m["misses"][0]["outcome"], m["misses"][0]["series_title"]) == (bid, "metron", "Saga")
    assert retag.journal(bid, on["db"])["queued_for"] == "changed"     # closed book: queued at once


def test_on_online_fallback_defers_the_retag_while_the_book_is_open(on, monkeypatch):
    bid = _book(on, None)
    monkeypatch.setattr("kometa.issue_meta.details_for", lambda i, p, want_covers=False: {**METRON, "source": "metron"})
    activity.note_reading(bid)                               # a page of it just served
    d = record.details(_issue(on), on["db"])
    assert (d["desc"], d["record"]) == ("A space opera.", False)
    assert localfirst.misses(on["db"])["by_reason"] == {"no record / no ComicInfo": 1}
    assert retag.journal(bid, on["db"]) is None and localfirst.deferred(on["db"]) == [bid]
    assert localfirst.flush_deferred(on["db"]) == 0           # still open
    activity._book_pages.clear()                              # closed
    assert localfirst.flush_deferred(on["db"]) == 1
    assert retag.journal(bid, on["db"])["state"] == "pending" and localfirst.deferred(on["db"]) == []


def test_progress_written_lately_counts_as_open(on):
    bid = _book(on, None)
    assert not localfirst.is_open(bid, on["db"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.set_progress("me", bid, 1, False, now, on["db"])
    assert localfirst.is_open(bid, on["db"])
    with db._connect(on["db"]) as c:
        c.execute("UPDATE read_progress SET updated_at = '2026-01-01T00:00:00Z'")
    assert not localfirst.is_open(bid, on["db"])


def test_on_a_stopgap_row_answers_now_and_logs_the_refresh(on, monkeypatch):
    bid = _book(on, FOREIGN_CI)
    record.write_issue(on["sid"], 1.0, {"desc": "From the scan."}, "comicinfo", "partial", _issue(on), on["db"])
    monkeypatch.setattr("kometa.issue_meta._metron", lambda issue, path: METRON)
    d = record.details(_issue(on), on["db"])
    assert (d["desc"], d["source"], d["record"]) == ("From the scan.", "comicinfo", True)
    assert record.get_issue(on["sid"], 1.0, on["db"])["source"] == "metron"     # filled behind
    m = localfirst.misses(on["db"])
    assert m["by_reason"] == {"stopgap (comicinfo) / foreign ComicInfo": 1} and m["misses"][0]["outcome"] == "metron"
    assert retag.journal(bid, on["db"])["queued_for"] == "changed"


def test_on_nothing_anywhere_is_still_a_logged_miss(on, monkeypatch):
    monkeypatch.setattr("kometa.issue_meta.details_for", lambda i, p, want_covers=False: {"desc": "", "credits": [], "source": None})
    d = record.details(_issue(on), on["db"])
    assert d["source"] is None
    m = localfirst.misses(on["db"])
    assert m["by_reason"] == {"no record / no book on the shelf": 1} and m["misses"][0]["outcome"] == "nothing"


def test_on_a_failed_fetch_is_logged_and_raised(on, monkeypatch):
    def down(*a, **k):
        raise RuntimeError("LOCG shut")
    monkeypatch.setattr("kometa.issue_meta.details_for", down)
    with pytest.raises(RuntimeError):
        record.details(_issue(on), on["db"])
    assert localfirst.misses(on["db"])["misses"][0]["outcome"] == "error"


def test_misses_route_and_progress_count(on, monkeypatch):
    localfirst.log_miss(on["sid"], 1.0, "no record", "no ComicInfo", "metron", path=on["db"])
    localfirst.log_miss(on["sid"], 2.0, "record miss", "no book on the shelf", "nothing", path=on["db"])
    monkeypatch.setattr(harvest, "DB_PATH", on["db"])
    monkeypatch.setattr(harvest, "_images_pending", lambda path: 0)
    record.ensure_tables(on["db"])
    out = harvest.get_misses()
    assert out["total"] == 2 and out["enabled"] is True and [m["number"] for m in out["misses"]] == [2.0, 1.0]
    assert harvest.progress(on["db"])["misses"] == 2


def test_config_flag_round_trips(tmp_path, monkeypatch):
    from kometa import main
    dbp = str(tmp_path / "k.db")
    db.init_db(dbp)
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(main, "DB_PATH", dbp)
    monkeypatch.setattr(main, "_locg_status", lambda: {})
    monkeypatch.setattr(main, "_comics_root", lambda: str(tmp_path))
    assert main.get_config()["read_local_first"] is False
    assert main.update_config(main.ConfigRequest(read_local_first="1"))["read_local_first"] is True
    assert localfirst.enabled(dbp)
    assert main.update_config(main.ConfigRequest(read_local_first="0"))["read_local_first"] is False


# --- 2026-10-13 follow-ups: stale rows refresh behind; id-less issues ask nothing online ---
def test_followup_stale_catalogue_row_answers_now_and_refreshes_behind(db_path, monkeypatch):
    import kometa.record as rec
    from kometa import localfirst
    db.set_config({"read_local_first": "1"}, db_path)
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, path=db_path)
    db.upsert_issue_status(sid, 1.0, None, 1, metron_issue_id=101, path=db_path)
    rec.write_issue(sid, 1.0, {"desc": "old"}, "metron", "full", {"metron_issue_id": 101}, db_path)
    monkeypatch.setattr(rec, "_fresh", lambda row: False)
    ran = []
    monkeypatch.setattr(localfirst, "_spawn", lambda fn: ran.append(fn))
    issue = {"tracked_series_id": sid, "number": 1.0, "metron_issue_id": 101}
    assert localfirst.details(issue, db_path)["desc"] == "old"
    assert len(ran) == 1                       # a refresh queued behind the answer


def test_followup_idless_issue_asks_nothing_online(db_path, monkeypatch):
    from kometa import issue_meta, localfirst
    db.set_config({"read_local_first": "1"}, db_path)
    sid = db.add_series(title="Tintin", publisher="Casterman", folder_path=None, path=db_path)
    db.upsert_issue_status(sid, 16.0, None, 1, path=db_path)
    monkeypatch.setattr(issue_meta, "details_for", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    d = localfirst.details({"tracked_series_id": sid, "number": 16.0}, db_path)
    assert d["source"] is None and d["desc"] == ""
