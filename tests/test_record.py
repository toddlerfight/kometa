"""The local catalogue record (kometa/record.py): filled from Metron first, LOCG
second, read by the modal, Related and reading-list covers."""
import pytest

import kometa.db as db
import kometa.record as rec
import kometa.related as rel


def _series(db_path, title="Saga", pull=False):
    sid = db.add_series(title=title, publisher="Image", folder_path=None, on_pull_list=pull, path=db_path)
    db.upsert_issue_status(sid, 1.0, "2012-03-14", 1, metron_issue_id=101, path=db_path)
    db.upsert_issue_status(sid, 2.0, "2012-04-11", 0, locg_issue_id="55", path=db_path)
    db.upsert_issue_status(sid, 3.0, "2012-05-09", 0, path=db_path)
    return sid


METRON = {"desc": "A space opera.", "credits": [{"role": "Writer", "name": "Brian K. Vaughan", "metron_creator_id": 7},
                                                {"role": "Artist", "name": "Fiona Staples", "metron_creator_id": 8}],
          "covers": [{"id": "m101", "name": "Cover A", "thumb": "https://m/1.jpg", "large": "https://m/1.jpg"}],
          "arcs": ["Chapter One"], "store_date": "2012-03-14", "page_count": 44, "price": "2.99", "isbn": None, "cover_date": "2012-03-01"}
LOCG = {"desc": "From LOCG.", "credits": [{"role": "Writer", "name": "Brian K. Vaughan", "people_id": None}]}


def test_fill_from_metron_then_locg_only_when_open_then_miss(db_path):
    sid = _series(db_path)
    asked = []
    m = lambda issue, path: (asked.append(("metron", issue["number"])) or METRON)
    r1 = rec.fill_issue(sid, 1.0, db_path, metron=m)
    assert (r1["source"], r1["fill_state"], r1["page_count"], r1["credits"][0]["metron_creator_id"]) == ("metron", "full", 44, 7)
    assert rec.fill_issue(sid, 1.0, db_path, metron=m)["fetched_at"] and asked == [("metron", 1.0)]   # fresh: not re-asked
    # LOCG shut: no row, no miss — asked again next time
    assert rec.fill_issue(sid, 2.0, db_path, locg=lambda i, p: LOCG, locg_open=lambda: False) is None
    assert rec.get_issue(sid, 2.0, db_path) is None
    r2 = rec.fill_issue(sid, 2.0, db_path, locg=lambda i, p: LOCG, locg_open=lambda: True)
    assert (r2["source"], r2["fill_state"], r2["credits"][0]["name"]) == ("locg", "partial", "Brian K. Vaughan")
    # nothing to ask: a miss, remembered for a week
    r3 = rec.fill_issue(sid, 3.0, db_path)
    assert r3["fill_state"] == "miss"
    assert [c["number"] for c in rec.pending_issues(db_path)] == []
    with db._connect(db_path) as c:
        c.execute("UPDATE issue_record SET fetched_at = '2026-01-01 00:00:00' WHERE number = 3")
    assert [c["number"] for c in rec.pending_issues(db_path)] == [3.0]


def test_trickle_orders_pull_list_first_and_stops_at_metrons_refusal(db_path, monkeypatch):
    from kometa import metron_client
    a = _series(db_path, "Back Issue", pull=False)
    b = _series(db_path, "Pulled", pull=True)
    calls = []
    def metron(issue, path):
        calls.append(issue["tracked_series_id"])
        if len(calls) == 2:
            return None                                   # Metron went quiet
        return METRON
    monkeypatch.setattr("kometa.issue_meta._metron", metron)
    monkeypatch.setattr(metron_client, "series_detail", lambda sid: {"publisher": "Image", "year": 2012})
    n = rec.trickle(limit=10, path=db_path)
    assert calls == [b, a] and n == 1                      # the pulled series first; stopped at the refusal
    assert rec.get_issue(b, 1.0, db_path)["fill_state"] == "full" and rec.get_issue(a, 1.0, db_path) is None
    assert rec.backlog(db_path)["filled"] == 1


def test_related_signals_come_from_the_record_before_any_catalogue(db_path):
    sid = _series(db_path)
    rec.write_issue(sid, 2.0, LOCG, "locg", "partial", {"locg_issue_id": "55"}, db_path)
    asked = []
    assert rel.fill_signals(sid, db_path, detail=lambda mid: asked.append(mid) or METRON)
    sig = rel._signals(db_path)[sid]
    assert asked == [] and sig["creators"] == [{"role": "writer", "name": "Brian K. Vaughan", "id": None}]
    rec.write_issue(sid, 1.0, METRON, "metron", "full", {"metron_issue_id": 101}, db_path)
    rel.fill_signals(sid, db_path, detail=lambda mid: asked.append(mid) or METRON)
    sig = rel._signals(db_path)[sid]
    assert sig["creators"][0]["id"] == 7 and sig["arcs"] == ["Chapter One"]   # the Metron row wins, ids and arcs


def test_details_serves_the_record_and_fills_it_on_a_miss(db_path, monkeypatch):
    sid = _series(db_path)
    issue = next(i for i in db.get_issues_for_series(sid, db_path) if i["number"] == 1.0)
    monkeypatch.setattr("kometa.issue_meta.details_for", lambda i, p, want_covers=False: {**METRON, "source": "metron"})
    d = rec.details(issue, db_path)
    assert d["record"] is False and d["credits"][0]["name"] == "Brian K. Vaughan"
    monkeypatch.setattr("kometa.issue_meta.details_for", lambda i, p, want_covers=False: (_ for _ in ()).throw(AssertionError("asked twice")))
    d2 = rec.details(issue, db_path)
    assert d2["record"] is True and d2["desc"] == "A space opera."


def test_reading_list_covers_come_from_the_record_first(db_path):
    import kometa.readlists as rl
    sid = _series(db_path)
    db.upsert_shelf_series("/x/Saga", "Saga", "Image", sid, 0, "2026-10-09T00:00:00Z", db_path)
    rec.write_issue(sid, 2.0, METRON, "metron", "full", {}, db_path)
    lid = rl.import_cbl(b"""<ReadingList><Name>S</Name><Books><Book Series="Saga" Number="2" /><Book Series="Saga" Number="3" /></Books></ReadingList>""", path=db_path)
    cv_asked = []
    class CV:
        def get_issues_meta(self, ids): cv_asked.append(ids); return {}
    r = rl.fill_covers(lid, path=db_path, cv=CV(), metron_search=lambda q: [], metron_issues=lambda s: [])
    e = {x["number"]: x for x in rl.resolve(lid, path=db_path)["entries"]}
    assert r["filled"] == 1 and e["2"]["cover"] and not e["2"]["cover_pending"]


# --- trades into the record ----------------------------------------------------------
from tests.conftest import make_cbz
from kometa import metron_client

SIBLINGS = {
    "East of West": [
        {"id": 4528, "series": "East of West (2013)", "year_began": 2013, "issue_count": 45, "series_type": {"id": 13, "name": "Single Issue"}},
        {"id": 7131, "series": "East of West TPB (2013)", "year_began": 2013, "issue_count": 2, "series_type": {"id": 10, "name": "Trade Paperback"}},
        {"id": 9000, "series": "East of West HC (2015)", "year_began": 2015, "issue_count": 1, "series_type": {"id": 8, "name": "Hardcover"}},
        {"id": 4529, "series": "East of West: The World (2014)", "year_began": 2014, "issue_count": 1, "series_type": {"id": 5, "name": "One-Shot"}},
        {"id": 1234, "series": "West TPB (1999)", "year_began": 1999, "issue_count": 3, "series_type": {"id": 10, "name": "Trade Paperback"}},
        {"id": 1235, "series": "East of West TPB (1990)", "year_began": 1990, "issue_count": 3, "series_type": {"id": 10, "name": "Trade Paperback"}},
    ]}
ISSUES = {7131: [{"number": 1.0, "store_date": "2013-09-11", "image": "https://m/v1.jpg", "metron_issue_id": 501},
                 {"number": 2.0, "store_date": "2014-03-01", "image": "https://m/v2.jpg", "metron_issue_id": 502}],
          9000: [{"number": 1.0, "store_date": "2015-01-01", "image": "https://m/hc1.jpg", "metron_issue_id": 601}]}
DETAIL = {501: {"title": "The Promise", "page": 156, "isbn": "9781607067702", "price": "9.99", "desc": "Year one.",
                "name": ["One: Out of the Wasteland", "Two: Above All"],
                "reprints": [{"id": 71, "issue": "East of West (2013) #1"}, {"id": 72, "issue": "East of West (2013) #2"}]},
          502: {"title": "We Are All One", "page": 128, "isbn": "9781607068556", "price": "14.99", "desc": "", "name": []},
          601: {"title": "Year One", "page": 300, "isbn": "9781632150004", "price": "49.99", "desc": "", "name": []}}


def _eow(db_path, tmp_path, monkeypatch, locg=None):
    import kometa.sync as sync
    monkeypatch.setattr(sync, "_komga", lambda: None)
    folder = tmp_path / "East of West"; folder.mkdir()
    sid = db.add_series(title="East of West", publisher="Image", year_began=2013, folder_path=str(folder), on_pull_list=False, path=db_path)
    db.set_metron_series_id(sid, 4528, db_path)
    if locg:
        db.set_locg_series_id(sid, locg, db_path)
    make_cbz(folder / "East of West v01 - The Promise.cbz")
    make_cbz(folder / "East of West - The World (2014).cbz")
    return db.get_series_by_id(sid, db_path)


def test_collected_siblings_share_the_base_name_a_collected_type_and_a_sane_year(db_path, tmp_path, monkeypatch):
    s = _eow(db_path, tmp_path, monkeypatch)
    sibs = rec.collected_siblings(s, search=lambda q: SIBLINGS.get(q, []))
    assert [(x["id"], x["format"]) for x in sibs] == [(7131, "TPB"), (9000, "HC")]   # not the run, the one-shot, 'West', or 1990's


def test_fill_trades_from_metron_writes_the_record_and_the_cache_the_tab_reads(db_path, tmp_path, monkeypatch):
    s = _eow(db_path, tmp_path, monkeypatch)
    trades = rec.fill_trades(s, path=db_path, search=lambda q: SIBLINGS.get(q, []), issues=lambda sid: ISSUES.get(sid, []),
                             detail=lambda iid: DETAIL[iid])
    by = {(t["vol"], t["format"]): t for t in trades}
    v1 = by[(1, "TPB")]
    assert v1["title"] == "East of West Vol. 1: The Promise TPB" and v1["locg_id"] == "m501" and v1["isbn"] == "9781607067702"
    assert v1["owned"] and v1["file"] == "East of West v01 - The Promise.cbz"            # the volume file on disk
    assert not by[(2, "TPB")]["owned"] and by[(1, "HC")]["title"] == "East of West Vol. 1: Year One HC"
    assert db.get_trades(s["id"], db_path)["trades"][0]["metron_issue_id"] == 501            # trades_cache: what the tab reads
    with db._connect(db_path) as c:
        keys = sorted(r[0] for r in c.execute("SELECT key FROM trade_record WHERE tracked_series_id = ?", (s["id"],)))
    assert keys == ["m501", "m502", "m601"]
    d = rec.trade_details("m501", db_path)
    assert "Collects: East of West #1; East of West #2" in d["desc"] and "156 pages" in d["desc"] and rec.trade_details("12345", db_path) is None


def test_locg_merges_by_volume_and_format_only_while_open(db_path, tmp_path, monkeypatch):
    s = _eow(db_path, tmp_path, monkeypatch, locg=777)
    locg = [{"format": "TPB", "title": "East of West Vol. 1 TP", "locg_id": "88", "cover": "https://l/1.jpg", "vol": 1, "vol_range": None, "is_variant": False},
            {"format": "TPB", "title": "East of West Compendium TP", "locg_id": "89", "cover": None, "vol": None, "vol_range": None, "is_variant": False}]
    kw = dict(path=db_path, search=lambda q: SIBLINGS.get(q, []), issues=lambda sid: ISSUES.get(sid, []), detail=lambda iid: DETAIL[iid])
    closed = rec.fill_trades(s, locg_fetch=lambda lid: locg, locg_open=lambda: False, **kw)
    assert all(t["source"] == "metron" for t in closed) and len(closed) == 3            # LOCG shut: Metron only
    opened = rec.fill_trades(s, locg_fetch=lambda lid: locg, locg_open=lambda: True, **kw)
    by = {t["title"]: t for t in opened}
    assert by["East of West Vol. 1: The Promise TPB"]["locg_trade_id"] == "88"           # known to both: Metron row keeps, gains the id
    assert by["East of West Compendium TP"]["source"] == "locg" and len(opened) == 4     # LOCG-only edition appended


def test_metron_key_never_collides_with_locg_ids_or_the_sentinels():
    from kometa.acquisition import PACK_LOCG_SENTINEL
    from kometa.proposals import PROPOSAL_LOCG_SENTINEL
    assert rec.metron_key(501) == "m501"
    assert rec.metron_key(1) not in {str(PACK_LOCG_SENTINEL), str(PROPOSAL_LOCG_SENTINEL), "1"}


def test_trades_trickle_fills_pull_list_first_and_stops_at_metrons_refusal(db_path, tmp_path, monkeypatch):
    import kometa.sync as sync
    monkeypatch.setattr(sync, "_komga", lambda: None)
    a = db.add_series(title="A", publisher="X", folder_path=None, on_pull_list=False, path=db_path)
    b = db.add_series(title="B", publisher="X", folder_path=None, on_pull_list=True, path=db_path)
    for sid in (a, b):
        db.set_metron_series_id(sid, 10 + sid, db_path)
    calls = []
    def fake_fill(series, path=None, **kw):
        calls.append(series["title"])
        if series["title"] == "A":
            raise metron_client.MetronUnavailable("429")
        db.set_trades(series["id"], [], path)
        return []
    monkeypatch.setattr(rec, "fill_trades", fake_fill)
    assert rec.trades_trickle(limit=5, path=db_path) == 1
    assert calls == ["B", "A"]                                                            # pull list first; refusal ends the tick


def test_locg_stands_in_for_trades_when_metron_is_out(db_path, tmp_path, monkeypatch):
    """Metron refuses, LOCG is open: the list is LOCG's, nothing raised, and the
    cache is pre-aged so Metron's editions are asked for tomorrow. Both out: raised."""
    import kometa.record as rec
    from kometa.metron_client import MetronUnavailable
    sid = db.add_series(title="The Amazing Spider-Man", publisher="Marvel", folder_path=str(tmp_path), on_pull_list=False,
                        locg_series_id=183512, path=db_path)
    db.set_metron_series_id(sid, 10958, db_path)
    monkeypatch.setattr(rec, "_metron_trades", lambda *a, **k: (_ for _ in ()).throw(MetronUnavailable("asked us to wait")))
    trades = rec.fill_trades(sid, path=db_path, locg_open=lambda: True,
                             locg_fetch=lambda lid: [{"title": "Amazing Spider-Man Vol. 1 TP", "vol": 1, "format": "TP", "is_variant": False}],
                             enrich=lambda s, t, books=None: t)
    assert [t["title"] for t in trades] == ["Amazing Spider-Man Vol. 1 TP"]
    with db._connect(db_path) as c:
        age = c.execute("SELECT (julianday('now') - julianday(fetched_at)) FROM trades_cache WHERE tracked_series_id = ?", (sid,)).fetchone()[0]
    assert age > 5                                                     # pre-aged: due again tomorrow, not next week
    import pytest
    with pytest.raises(MetronUnavailable):
        rec.fill_trades(sid, path=db_path, force=True, locg_open=lambda: False, enrich=lambda s, t, books=None: t)
