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
    # issue-based (2026-10-15): a trade reprint no longer hides the single from the
    # missing COUNT — all three unowned released issues are missing — but the reading
    # list still resolves the covered ones as owned-via-trade.
    assert db.get_all_series_summaries(db_path)[ff]["missing"] == 3
    lid = rl.save_list("FF", [{"series": "Fantastic Four", "number": "67", "volume": "1998"},
                              {"series": "Fantastic Four", "number": "503", "volume": "1998"}], "cbl", None, db_path)
    e = rl.resolve(lid, db_path)["entries"]
    assert e[0]["status"] == "owned" and e[0].get("via_trade") and e[1]["status"] == "missing"


def test_parse_collects_reads_locg_lines():
    rows = tf.parse_collects("Mark Waid's run continues! Collecting FANTASTIC FOUR #66-70 and #500-502.")
    assert [r["number"] for r in rows] == [66, 67, 68, 69, 70, 500, 501, 502]
    rows = tf.parse_collects("Collects Secret Avengers (2010) #12.1, 13-15 and Fear Itself: Black Widow #1.")
    assert (rows[0]["series"], rows[0]["year"], rows[0]["number"]) == ("Secret Avengers", 2010, 12.1)
    assert rows[-1]["series"] == "Fear Itself: Black Widow"
    assert tf.parse_collects("A great story.") == []


def test_when_metron_lacks_the_edition_the_runs_locg_trades_answer(db_path):
    nothing = dict(search=lambda q: [], issues=lambda sid: [], get=lambda iid: {})
    ff = db.add_series(title="Fantastic Four", publisher="Marvel", year_began=1997, folder_path=None, on_pull_list=False, path=db_path)
    _stuck(db_path, ff, [67, 68, 500], date="2003-03-26")
    db.set_trades(ff, [{"title": "Fantastic Four by Waid & Wieringo: Imaginauts TP", "locg_id": "111", "format": "TPB", "store_date": "2017-01-01"},
                       {"title": "Fantastic Four by Waid & Wieringo: Unthinkable TP", "locg_id": "4834365", "format": "TPB", "store_date": "2017-06-01"}],
                  db_path)
    desc = {"111": "Collecting FANTASTIC FOUR #60-65.", "4834365": "Collecting FANTASTIC FOUR #66-70 and #500-502."}
    # LOCG shut: the rows wait for the next pass, they don't give up
    import kometa.locg_client as lc
    real = lc.locg_paused
    lc.locg_paused = lambda: 1e12
    try:
        out = tf.run_pass(db_path, **nothing)
    finally:
        lc.locg_paused = real
    assert out["proposed"] == [] and {tf._state(r["id"], db_path) for r in tf.stuck_rows(db_path, 50)} <= {"looking", None}
    with db._connect(db_path) as c:
        c.execute("UPDATE trade_fallback SET checked_at = '2000-01-01'")
    out = tf.run_pass(db_path, locg_details=lambda lid: {"desc": desc[lid]}, **nothing)
    assert [(p["title"], p["fills"]) for p in out["proposed"]] == [("Fantastic Four by Waid & Wieringo: Unthinkable TP", "#67–68, #500")]
    with db._connect(db_path) as c:
        assert tuple(c.execute("SELECT locg_id, state FROM download_queue WHERE kind = 'trade'").fetchone()) == ("4834365", "suggested")


def test_a_point_one_finds_its_event_trade_on_locg_across_series(db_path):
    nothing = dict(search=lambda q: [], issues=lambda sid: [], get=lambda iid: {})
    sa = db.add_series(title="Secret Avengers", publisher="Marvel", year_began=2010, folder_path=None, on_pull_list=False, path=db_path)
    _stuck(db_path, sa, [12.1], date="2011-04-06")
    hits = [{"id": 55, "title": "Secret Avengers"}, {"id": 77, "title": "Fear Itself: Secret Avengers"},
            {"id": 88, "title": "Uncanny X-Men"}]
    lists = {77: [{"title": "Fear Itself: Secret Avengers TP", "locg_id": "9977", "format": "TPB", "store_date": "2012-01-11"}]}
    desc = {"9977": "Collecting Secret Avengers #12.1, 13-15 and Fear Itself: Black Widow #1."}
    asked = []
    out = tf.run_pass(db_path, locg_details=lambda lid: asked.append(lid) or {"desc": desc[lid]},
                      locg_search=lambda q: hits, locg_trades=lambda lid: lists.get(lid, []), **nothing)
    assert [(p["title"], p["fills"]) for p in out["proposed"]] == [("Fear Itself: Secret Avengers TP", "#12.1")]
    assert asked == ["9977"]                       # only the related series' trade was read


def test_an_interrupted_look_comes_back_in_an_hour_a_dead_end_in_a_week(db_path):
    ff = db.add_series(title="Fantastic Four", publisher="Marvel", year_began=1998, folder_path=None, on_pull_list=False, path=db_path)
    _stuck(db_path, ff, [67, 68])
    tf.ensure_tables(db_path)
    with db._connect(db_path) as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM download_queue WHERE tracked_series_id = ? ORDER BY issue_number", (ff,))]
    tf._mark(ids[0], "looking", db_path)
    tf._mark(ids[1], "none", db_path)
    assert tf.stuck_rows(db_path, 50) == []                     # both just checked
    two_hours_ago = (tf.datetime.now(tf.timezone.utc) - tf.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect(db_path) as c:
        c.execute("UPDATE trade_fallback SET checked_at = ?", (two_hours_ago,))
    assert [r["id"] for r in tf.stuck_rows(db_path, 50)] == [ids[0]]


def test_trade_searches_use_the_name_releases_carry():
    from kometa.acquisition import trade_search_title as t
    assert t("Fantastic Four by Waid & Wieringo: Unthinkable TP") == "Fantastic Four: Unthinkable"
    assert t("Avengers Academy: The Complete Collection Vol. 2 TP", 2) == "Avengers Academy: The Complete Collection"
    assert t("Batman: Hush") == "Batman: Hush"
    assert t("Batman by Brian Azzarello and Eduardo Risso - The Deluxe Edition") == "Batman by Brian Azzarello and Eduardo Risso - The Deluxe Edition"


# --- event-aware searches: a tie-in whose event edition the plain search never returns ---
def _fear_itself_metron():
    """Metron as Secret Avengers #12.1 sees it: 'secret avengers' only answers the
    run's own TPB; 'Fear Itself: Secret Avengers' has to be asked for by name."""
    search = {"Secret Avengers": [
                  {"id": 200, "series": "Secret Avengers TPB (2010)", "series_type": {"name": "Trade Paperback"}, "year_began": 2010}],
              "Fear Itself: Secret Avengers": [
                  {"id": 201, "series": "Fear Itself: Secret Avengers (2012)", "series_type": {"name": "Trade Paperback"}, "year_began": 2012}]}
    vols = {200: [{"number": 2.0, "store_date": "2011-08-01", "metron_issue_id": 9200}],
            201: [{"number": 1.0, "store_date": "2012-01-11", "metron_issue_id": 9201}]}
    details = {9200: {"number": "2", "title": "Eyes of the Dragon", "reprints": [{"issue": "Secret Avengers (2010) #6"}],
                      "series": {"id": 200, "name": "Secret Avengers TPB (2010)", "series_type": {"name": "Trade Paperback"}}},
               9201: {"number": "1", "title": None, "store_date": "2012-01-11",
                      "reprints": [{"issue": "Secret Avengers (2010) #12.1"}, {"issue": "Fear Itself: Black Widow (2011) #1"}],
                      "series": {"id": 201, "name": "Fear Itself: Secret Avengers (2012)", "series_type": {"name": "Trade Paperback"}}}}
    asked = []
    world = dict(search=lambda q: asked.append(q) or search.get(q, []), issues=lambda sid: vols.get(sid, []),
                 get=lambda iid: dict(details[iid], id=iid))
    return world, asked


def _fear_itself_list(path, series, number, volume):
    rl.save_list("Fear Itself (full event)", [{"series": "Fear Itself", "number": "1", "volume": "2011"},
                                              {"series": series, "number": number, "volume": volume}], "cbl", None, path)


def test_event_name_from_a_reading_list_name():
    assert tf.event_name("Fear Itself (full event)") == "Fear Itself"
    assert tf.event_name("Civil War - Reading Order") == "Civil War"
    assert tf.event_name("Secret Invasion") == "Secret Invasion"


def test_a_tie_in_in_an_event_list_asks_metron_for_the_event_edition(db_path):
    world, asked = _fear_itself_metron()
    sa = db.add_series(title="Secret Avengers", publisher="Marvel", year_began=2010, folder_path=None, on_pull_list=False, path=db_path)
    _stuck(db_path, sa, [12.1], date="2011-04-06")
    _fear_itself_list(db_path, "Secret Avengers", "12.1", "2010")
    out = tf.run_pass(db_path, **world)
    assert [(p["title"], p["fills"]) for p in out["proposed"]] == [("Fear Itself: Secret Avengers Vol. 1", "#12.1")]
    assert asked == ["Secret Avengers", "Fear Itself: Secret Avengers"]


def test_a_tie_in_asks_locg_for_the_event_edition_when_metron_has_nothing(db_path):
    nothing = dict(search=lambda q: [], issues=lambda sid: [], get=lambda iid: {})
    gr = db.add_series(title="Ghost Rider", publisher="Marvel", year_began=2011, folder_path=None, on_pull_list=False, path=db_path)
    _stuck(db_path, gr, [0.1], date="2011-07-13")
    _fear_itself_list(db_path, "Ghost Rider", "0.1", "2011")
    # LOCG's 'ghost rider' tops out before any Fear Itself edition; by name it's there
    hits = {"Ghost Rider": [{"id": 55, "title": "Ghost Rider"}, {"id": 56, "title": "Ghost Rider: Danny Ketch"}],
            "Fear Itself: Ghost Rider": [{"id": 77, "title": "Fear Itself: Ghost Rider"}]}
    lists = {56: [], 77: [{"title": "Fear Itself: Ghost Rider TP", "locg_id": "9977", "format": "TPB", "store_date": "2012-02-01"}]}
    desc = {"9977": "Collecting Ghost Rider (2011) #0.1, 1-5."}
    searched = []
    out = tf.run_pass(db_path, locg_details=lambda lid: {"desc": desc[lid]},
                      locg_search=lambda q: searched.append(q) or hits.get(q, []),
                      locg_trades=lambda lid: lists.get(lid, []), **nothing)
    assert [(p["title"], p["fills"]) for p in out["proposed"]] == [("Fear Itself: Ghost Rider TP", "#0.1")]
    assert searched == ["Ghost Rider", "Fear Itself: Ghost Rider"]


def test_no_event_search_when_the_plain_search_already_found_a_trade(db_path):
    world, asked = _fear_itself_metron()
    # the plain search turns the event edition up itself (Metron's contains-match)
    plain = {"Secret Avengers": [{"id": 201, "series": "Fear Itself: Secret Avengers (2012)",
                                  "series_type": {"name": "Trade Paperback"}, "year_began": 2012}]}
    world["search"] = lambda q: asked.append(q) or plain.get(q, [])
    sa = db.add_series(title="Secret Avengers", publisher="Marvel", year_began=2010, folder_path=None, on_pull_list=False, path=db_path)
    _stuck(db_path, sa, [12.1], date="2011-04-06")
    _fear_itself_list(db_path, "Secret Avengers", "12.1", "2010")
    locg_asked = []
    out = tf.run_pass(db_path, locg_search=lambda q: locg_asked.append(q) or [], **world)
    assert [p["title"] for p in out["proposed"]] == ["Fear Itself: Secret Avengers Vol. 1"]
    assert asked == ["Secret Avengers"] and locg_asked == []


def test_an_empty_look_from_the_old_strategy_is_retried_once(db_path):
    world, asked = _fear_itself_metron()
    sa = db.add_series(title="Secret Avengers", publisher="Marvel", year_began=2010, folder_path=None, on_pull_list=False, path=db_path)
    _stuck(db_path, sa, [12.1], date="2011-04-06")
    _fear_itself_list(db_path, "Secret Avengers", "12.1", "2010")
    tf.ensure_tables(db_path)
    now = tf._now()
    with db._connect(db_path) as c:
        qid = c.execute("SELECT id FROM download_queue WHERE tracked_series_id = ?", (sa,)).fetchone()[0]
        # what the live box holds: looked today by the old strategy, nothing found
        c.execute("INSERT INTO trade_lookup (issue_key, candidates_json, fetched_at) VALUES (?, '[]', ?)",
                  (tf.issue_key(sa, 12.1), now))
        c.execute("INSERT INTO trade_fallback (queue_id, state, rejected_json, checked_at) VALUES (?, 'none', '[]', ?)", (qid, now))
    assert [r["id"] for r in tf.stuck_rows(db_path, 50)] == [qid]
    out = tf.run_pass(db_path, locg_search=lambda q: [], **world)
    assert [p["title"] for p in out["proposed"]] == ["Fear Itself: Secret Avengers Vol. 1"]
    assert "Fear Itself: Secret Avengers" in asked


def test_a_miss_under_the_current_strategy_waits_the_week(db_path, monkeypatch):
    import kometa.locg_client as lc
    monkeypatch.setattr(lc, "locg_paused", lambda: None)       # LOCG open, whatever earlier tests left behind
    asked = []
    nothing = dict(search=lambda q: asked.append(q) or [], issues=lambda sid: [], get=lambda iid: {})
    sa = db.add_series(title="Secret Avengers", publisher="Marvel", year_began=2010, folder_path=None, on_pull_list=False, path=db_path)
    _stuck(db_path, sa, [12.1], date="2011-04-06")
    _fear_itself_list(db_path, "Secret Avengers", "12.1", "2010")
    locg = []
    out = tf.run_pass(db_path, locg_search=lambda q: locg.append(q) or [], **nothing)
    assert out["none"] == 1
    assert asked == ["Secret Avengers", "Fear Itself: Secret Avengers"]
    assert locg == ["Secret Avengers", "Fear Itself: Secret Avengers"]
    assert tf.stuck_rows(db_path, 50) == []                     # not back next pass
    tf.run_pass(db_path, locg_search=lambda q: locg.append(q) or [], **nothing)
    assert len(asked) == 2 and len(locg) == 2


# --- LOCG trades land and cover (2026-10-11: Avengers Academy #14.1 'Getting it in …' forever) ---
_AA_CC = "Avengers Academy: The Complete Collection Vol. 2 TP"
_AA_DESC = "The kids are back! Collecting Avengers Academy #13-20 and #14.1."


def _aa(db_path, folder):
    aa = db.add_series(title="Avengers Academy", publisher="Marvel", year_began=2010, folder_path=str(folder),
                       on_pull_list=False, path=db_path)
    _stuck(db_path, aa, [13, 14.1, 21], date="2011-06-01")
    db.set_issue_details_cache("5484155", {"desc": _AA_DESC}, db_path)
    return aa


def test_a_locg_trade_landing_finishes_the_issues_it_was_fetched_for(db_path, tmp_path):
    from tests.conftest import make_cbz
    folder = tmp_path / "Avengers Academy"; folder.mkdir()
    aa = _aa(db_path, folder)
    db.set_trades(aa, [{"title": _AA_CC, "locg_id": "5484155", "vol": 2, "format": "TPB", "store_date": "2018-03-01"}], db_path)
    nothing = dict(search=lambda q: [], issues=lambda sid: [], get=lambda iid: {})
    out = tf.run_pass(db_path, locg_details=lambda lid: {"desc": _AA_DESC}, **nothing)
    assert [p["fills"] for p in out["proposed"]] == ["#13, #14.1"]
    qid = out["proposed"][0]["qid"]
    tf.confirm(qid, db_path)
    with db._connect(db_path) as c:
        # confirming taught trade_record what the LOCG edition holds
        rec = c.execute("SELECT collects_json FROM trade_record WHERE tracked_series_id = ? AND key = 'l:5484155'", (aa,)).fetchone()
    assert 14.1 in [x["number"] for x in json.loads(rec[0])]
    book = make_cbz(folder / "Avengers Academy - The Complete Collection Vol. 2 TP Vol 02.cbz")
    db.complete_trade(qid, db_path, placed=[str(book)])
    with db._connect(db_path) as c:
        q = {r["issue_number"]: (r["state"], r["error"]) for r in c.execute(
            "SELECT issue_number, state, error FROM download_queue WHERE kind = 'issue'")}
        cov = {r[0]: r[1] for r in c.execute("SELECT number, covered_by FROM issue_status WHERE tracked_series_id = ?", (aa,))}
        assert c.execute("SELECT state FROM download_queue WHERE id = ?", (qid,)).fetchone()[0] == "done"
    assert q[14.1] == ("done", f"In {book.name}") and q[13.0][0] == "done"
    assert q[21.0][0] == "not_found"                                   # not in the trade
    assert cov[13.0] == str(book) and cov[14.1] == str(book) and cov[21.0] is None
    # the 10-minute coverage pass keeps them covered (it clears and re-stamps everything)
    assert tf.refresh_coverage(db_path) >= 2
    with db._connect(db_path) as c:
        cov = {r[0]: r[1] for r in c.execute("SELECT number, covered_by FROM issue_status WHERE tracked_series_id = ?", (aa,))}
    assert cov[13.0] == str(book) and cov[14.1] == str(book) and cov[21.0] is None


def test_refresh_coverage_reads_an_owned_locg_trades_collecting_line(db_path, tmp_path):
    folder = tmp_path / "Avengers Academy"; folder.mkdir()
    aa = _aa(db_path, folder)
    with db._connect(db_path) as c:
        c.execute("UPDATE download_queue SET state = 'via_trade', error = 'Getting it in …' WHERE issue_number = 14.1")
    f = "Avengers Academy - The Complete Collection Vol. 02 (2018).cbz"
    db.set_trades(aa, [{"title": _AA_CC, "locg_id": "5484155", "vol": 2, "owned": True, "file": f, "metron_issue_id": None}], db_path)
    assert tf.refresh_coverage(db_path) == 9                          # #13–20 + #14.1
    with db._connect(db_path) as c:
        cov = {r[0]: r[1] for r in c.execute("SELECT number, covered_by FROM issue_status WHERE tracked_series_id = ?", (aa,))}
        q = {r[0]: (r[1], r[2]) for r in c.execute("SELECT issue_number, state, error FROM download_queue WHERE kind = 'issue'")}
    assert cov[14.1] == os.path.join(str(folder), f) and cov[13.0] and cov[21.0] is None
    assert q[14.1] == ("done", f"In {f}") and q[21.0][0] == "not_found"


def test_a_reprint_goes_to_the_dated_run_not_a_yearless_namesake():
    yearless = {"id": 395, "year_began": None}
    run_1997 = {"id": 956, "year_began": 1997}
    run_2018 = {"id": 7, "year_began": 2018}
    assert tf._same_run([yearless, run_1997, run_2018], 1998) is run_1997      # Unthinkable's #501 (1998)
    assert tf._same_run([yearless, run_2018], 1998) is yearless                 # no dated run fits: the yearless one
    assert tf._same_run([run_2018], 1998) is None
    assert tf._same_run([yearless, run_1997], None) is yearless                 # no year on the reprint: as before


def test_dropping_a_trade_that_wasnt_found_sends_its_issues_back_to_the_single_search(db_path, monkeypatch):
    monkeypatch.setattr(tf, "DB_PATH", db_path)
    tf.ensure_tables(db_path)
    sid = db.add_series(title="Journey Into Mystery", publisher="Marvel", folder_path=None, on_pull_list=False, path=db_path)
    db.queue_issue(sid, 626.1, db_path)
    with db._connect(db_path) as c:
        issue = c.execute("SELECT id FROM download_queue").fetchone()[0]
        c.execute("UPDATE download_queue SET state = 'via_trade' WHERE id = ?", (issue,))
        trade = c.execute("""INSERT INTO download_queue (tracked_series_id, kind, state, locg_id, meta_json)
            VALUES (?, 'trade', 'not_found', '8015010', ?)""", (sid, json.dumps({"title": "Loki Epic Collection", "fills": [issue]}))).lastrowid
    tf.decline(trade, db_path)
    states = {q["id"]: q["state"] for q in db.get_queue(db_path)}
    assert trade not in states and states[issue] == "queued"
    with db._connect(db_path) as c:
        assert "l8015010" in c.execute("SELECT rejected_json FROM trade_fallback WHERE queue_id = ?", (issue,)).fetchone()[0]



def test_a_pulled_series_sweeps_singles_even_when_a_trade_covers_them(db_path):
    # The Seasons (2026-10-15): the user pulls ISSUES, not trades. Owning the Vol 3 TP
    # that reprints #12 must NOT suppress the single from the sweep — trades are only
    # acquired when wanted per-trade. #13 (unreleased) still waits for its date.
    sid = db.add_series(title="The Seasons", publisher="Image", folder_path="/x/The Seasons",
                        on_pull_list=True, path=db_path)
    db.upsert_issue_status_many([(sid, 12.0, "2026-10-14", False, None, None, None),
                                 (sid, 13.0, "2026-10-21", False, None, None, None)], path=db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET monitor_status='monitored' WHERE id=?", (sid,))
        c.execute("UPDATE issue_status SET covered_by='/x/The Seasons/Vol 3 TP.cbz' WHERE tracked_series_id=?", (sid,))
    import datetime
    today = datetime.date.today().isoformat()
    # move #12 to today so it reads as released regardless of when the test runs
    with db._connect(db_path) as c:
        c.execute("UPDATE issue_status SET store_date=? WHERE tracked_series_id=? AND number=12", (today, sid))
        c.execute("UPDATE issue_status SET store_date=date('now','+7 days') WHERE tracked_series_id=? AND number=13", (sid,))
    assert db.get_missing_counts_by_series(db_path).get(sid) == 1          # #12 swept despite being trade-covered
    nums = {r["number"] for r in db.get_missing_for_monitored(db_path)}
    assert 12.0 in nums and 13.0 not in nums                               # #13 not out yet
