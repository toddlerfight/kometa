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
