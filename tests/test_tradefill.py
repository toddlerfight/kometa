"""Trades fill what singles can't (kometa/tradefill.py): find the smallest edition
that reprints a stuck issue — across series — propose it, download nothing until
confirmed, and count an owned trade's issues as covered."""
import json
import os

import pytest

import kometa.db as db
import kometa.tradefill as tf
import kometa.readlists as rl
from tests.test_readlists import _shelf


def _ff_world():
    """Metron as the FF #67-70/#500 case sees it: a TPB series (vol 1, vol 2 =
    Unthinkable) and an omnibus that also reprints them."""
    search = {"Fantastic Four": [
        {"id": 100, "series": "Fantastic Four TPB (2003)", "series_type": {"name": "Trade Paperback"}, "year_began": 2003},
        {"id": 101, "series": "Fantastic Four by Waid Omnibus (2011)", "series_type": {"name": "Omnibus"}, "year_began": 2011},
        {"id": 102, "series": "Fantastic Four (1998)", "series_type": {"name": "Single Issue"}, "year_began": 1998}]}
    vols = {100: [{"number": 1.0, "store_date": "2003-05-01", "metron_issue_id": 9001},
                  {"number": 2.0, "store_date": "2003-12-01", "metron_issue_id": 9002}],
            101: [{"number": 1.0, "store_date": "2011-06-01", "metron_issue_id": 9101}]}
    rp = lambda nums: [{"id": None, "issue": f"Fantastic Four (1998) #{n}"} for n in nums]
    details = {9001: {"number": "1", "title": "Imaginauts", "store_date": "2003-05-01", "reprints": rp(range(60, 67)),
                      "series": {"id": 100, "name": "Fantastic Four TPB (2003)", "series_type": {"name": "Trade Paperback"}}},
               9002: {"number": "2", "title": "Unthinkable", "store_date": "2003-12-01", "reprints": rp([67, 68, 69, 70, 500, 501, 502]),
                      "series": {"id": 100, "name": "Fantastic Four TPB (2003)", "series_type": {"name": "Trade Paperback"}}},
               9101: {"number": "1", "title": None, "store_date": "2011-06-01", "reprints": rp(list(range(60, 71)) + list(range(500, 525))),
                      "series": {"id": 101, "name": "Fantastic Four by Waid Omnibus (2011)", "series_type": {"name": "Omnibus"}}}}
    calls = {"get": 0}
    def get(iid):
        calls["get"] += 1
        return dict(details[iid], id=iid)
    return dict(search=lambda q: search.get(q, []), issues=lambda sid: vols.get(sid, []), get=get), calls


def _stuck(path, sid, numbers, date="2003-01-01"):
    db.upsert_issue_status_many([(sid, float(n), date, False, None, None, None) for n in numbers], path=path)
    db.queue_issues_bulk([(sid, float(n)) for n in numbers], path)
    with db._connect(path) as c:
        c.execute("UPDATE download_queue SET state = 'not_found' WHERE tracked_series_id = ?", (sid,))


def test_parse_reprint():
    assert tf.parse_reprint("Fantastic Four (1998) #67") == {"series": "Fantastic Four", "year": 1998, "number": 67.0}
    assert tf.parse_reprint("Secret Avengers (2010) #12.1")["number"] == 12.1
    assert tf.parse_reprint("not an issue") is None


def test_one_trade_fills_five_stuck_issues_and_nothing_downloads_until_confirmed(db_path):
    world, calls = _ff_world()
    ff = db.add_series(title="Fantastic Four", publisher="Marvel", year_began=1998, folder_path=None, on_pull_list=False, path=db_path)
    _stuck(db_path, ff, [67, 68, 69, 70, 500])
    out = tf.run_pass(db_path, **world)
    assert [p["title"] for p in out["proposed"]] == ["Fantastic Four TPB Vol. 2: Unthinkable"]
    assert out["proposed"][0]["fills"] == "#67–70, #500"
    with db._connect(db_path) as c:
        rows = {r["state"]: r for r in c.execute("SELECT * FROM download_queue WHERE kind = 'trade'")}
        assert set(rows) == {"suggested"}                          # the worker never picks this state up
        assert c.execute("SELECT COUNT(*) FROM download_queue WHERE state = 'queued'").fetchone()[0] == 0
    assert db.get_queued_items(db_path) == []
    # a second pass proposes nothing new and reads nothing new (cached)
    before = calls["get"]
    assert tf.run_pass(db_path, **world)["proposed"] == []
    assert calls["get"] == before
    # confirm: an ordinary trade grab; the five leave not_found
    qid = rows["suggested"]["id"]
    tf.confirm(qid, db_path)
    with db._connect(db_path) as c:
        assert c.execute("SELECT state FROM download_queue WHERE id = ?", (qid,)).fetchone()[0] == "queued"
        assert {r[0] for r in c.execute("SELECT state FROM download_queue WHERE kind = 'issue'")} == {"via_trade"}
    assert db.get_trades(ff, db_path)["trades"][-1]["metron_issue_id"] == 9002    # the series knows the edition now


def test_declining_offers_the_next_best(db_path):
    world, _ = _ff_world()
    ff = db.add_series(title="Fantastic Four", publisher="Marvel", year_began=1998, folder_path=None, on_pull_list=False, path=db_path)
    _stuck(db_path, ff, [67, 500])
    tf.run_pass(db_path, **world)
    with db._connect(db_path) as c:
        qid = c.execute("SELECT id FROM download_queue WHERE state = 'suggested'").fetchone()[0]
    tf.decline(qid, db_path)
    out = tf.run_pass(db_path, **world)
    assert [p["title"] for p in out["proposed"]] == ["Fantastic Four by Waid Omnibus Vol. 1"]


def test_a_point_one_finds_its_event_trade_in_another_series(db_path):
    sa = db.add_series(title="Secret Avengers", publisher="Marvel", year_began=2010, folder_path=None, on_pull_list=False, path=db_path)
    _stuck(db_path, sa, [12.1], date="2011-04-06")
    search = {"Secret Avengers": [
        {"id": 200, "series": "Secret Avengers TPB (2010)", "series_type": {"name": "Trade Paperback"}, "year_began": 2010},
        {"id": 201, "series": "Fear Itself: Secret Avengers (2012)", "series_type": {"name": "Trade Paperback"}, "year_began": 2012}]}
    vols = {200: [{"number": 2.0, "store_date": "2011-08-01", "metron_issue_id": 9200}],
            201: [{"number": 1.0, "store_date": "2012-01-11", "metron_issue_id": 9201}]}
    details = {9200: {"number": "2", "title": "Eyes of the Dragon", "reprints": [{"issue": "Secret Avengers (2010) #6"}],
                      "series": {"id": 200, "name": "Secret Avengers TPB (2010)", "series_type": {"name": "Trade Paperback"}}},
               9201: {"number": "1", "title": None, "store_date": "2012-01-11",
                      "reprints": [{"issue": "Secret Avengers (2010) #12.1"}, {"issue": "Secret Avengers (2010) #13"},
                                   {"issue": "Fear Itself: Black Widow (2011) #1"}],
                      "series": {"id": 201, "name": "Fear Itself: Secret Avengers (2012)", "series_type": {"name": "Trade Paperback"}}}}
    out = tf.run_pass(db_path, search=lambda q: search.get(q, []), issues=lambda sid: vols.get(sid, []),
                      get=lambda iid: dict(details[iid], id=iid))
    assert [(p["title"], p["fills"]) for p in out["proposed"]] == [("Fear Itself: Secret Avengers Vol. 1", "#12.1")]


def test_an_owned_trade_covers_its_issues_for_counts_and_reading_lists(db_path, tmp_path):
    world, _ = _ff_world()
    root = tmp_path / "comics" / "Marvel Comics"
    ff = db.add_series(title="Fantastic Four", publisher="Marvel", year_began=1998, folder_path=str(root / "Fantastic Four"),
                       on_pull_list=False, path=db_path)
    _shelf(db_path, root, "Fantastic Four", ["Fantastic Four Vol. 02 - Unthinkable (2003).cbz"], tracked=ff)
    db.upsert_issue_status_many([(ff, float(n), "2003-01-01", False, None, None, None) for n in (67, 500, 503)], path=db_path)
    tf.ensure_tables(db_path)
    tf.edition(9002, db_path, world["get"])
    db.set_trades(ff, [{"title": "Fantastic Four Vol. 2: Unthinkable TPB", "vol": 2, "owned": True,
                        "file": "Fantastic Four Vol. 02 - Unthinkable (2003).cbz", "metron_issue_id": 9002}], db_path)
    assert tf.refresh_coverage(db_path) >= 2
    with db._connect(db_path) as c:
        cov = {r[0]: r[1] for r in c.execute("SELECT number, covered_by FROM issue_status WHERE tracked_series_id = ?", (ff,))}
    assert cov[67.0].endswith("Unthinkable (2003).cbz") and cov[500.0] and cov[503.0] is None
    assert db.get_all_series_summaries(db_path)[ff]["missing"] == 1          # only #503
    lid = rl.save_list("FF", [{"series": "Fantastic Four", "number": "67", "volume": "1998"},
                              {"series": "Fantastic Four", "number": "503", "volume": "1998"}], "cbl", None, db_path)
    e = rl.resolve(lid, db_path)["entries"]
    assert e[0]["status"] == "owned" and e[0].get("via_trade") and e[1]["status"] == "missing"
