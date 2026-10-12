"""The library harvest (enrichment spec 1b): owned issues first, the harvest's
share of Metron's day, and the progress summary. No network."""
import io
import json
import time
import urllib.error

import pytest

import kometa.db as db
import kometa.record as rec
from kometa import harvest, metron_client as mc
from kometa.interactive import harvesting, is_harvest


def test_owned_stopgap_issues_come_before_unowned_recordless_ones(db_path):
    """2026-10-12 live: 2,528 owned issues with a Metron id waited behind 1,956
    unowned issues that simply had no record yet."""
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 1.0, None, 0, metron_issue_id=1, path=db_path)       # unowned, no record
    db.upsert_issue_status(sid, 2.0, None, 1, metron_issue_id=2, path=db_path)       # owned, stopgap record
    rec.write_issue(sid, 2.0, {"desc": "from the file", "credits": [], "covers": []}, "comicinfo", "partial", {}, db_path)
    order = [c["number"] for c in rec.pending_issues(db_path)]
    assert order == [2.0, 1.0]


def test_harvesting_marks_only_the_calls_inside():
    assert not is_harvest()
    with harvesting():
        assert is_harvest()
    assert not is_harvest()


@pytest.fixture
def metron(db_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.set_config({"metron_token": "good"}, db_path)
    monkeypatch.setattr(mc, "MIN_INTERVAL_S", 0)
    monkeypatch.setattr(mc, "_state", {"last": 0.0, "paused_until": 0.0, "sustained_remaining": None, "sustained_reset": 0.0})
    calls = []
    left = {"n": 1001}

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        left["n"] -= 1

        class R(io.BytesIO):
            headers = {"X-Ratelimit-Sustained-Remaining": str(left["n"]),
                       "X-Ratelimit-Sustained-Reset": str(time.time() + 3600)}
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R(json.dumps({"results": []}).encode())

    monkeypatch.setattr(mc.urllib.request, "urlopen", urlopen)
    return calls


def test_harvest_stops_at_its_floor_but_the_app_keeps_going(metron):
    with harvesting():
        mc._get("publisher/")                         # remaining 1000 after this one
        with pytest.raises(mc.MetronUnavailable, match="share"):
            mc._get("publisher/")                     # the harvest's share is spent
    assert len(metron) == 1
    mc._get("publisher/")                             # a sync / a page: still served
    assert len(metron) == 2
    assert mc.budget()["harvest_paused"] is True


def test_the_floor_lifts_after_the_reset(metron, monkeypatch):
    with harvesting():
        mc._get("publisher/")
    assert mc.harvest_share_spent()
    real = time.time
    monkeypatch.setattr(mc.time, "time", lambda: real() + 7200)
    assert not mc.harvest_share_spent()


def test_reset_as_seconds_to_go_reads_right(metron, monkeypatch):
    def urlopen(req, timeout=None):
        class R(io.BytesIO):
            headers = {"X-Ratelimit-Sustained-Remaining": "900", "X-Ratelimit-Sustained-Reset": "3600"}
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R(b"{}")
    monkeypatch.setattr(mc.urllib.request, "urlopen", urlopen)
    mc._get("publisher/")
    assert mc._state["sustained_reset"] > time.time() + 3000
    assert mc.harvest_share_spent()


def test_progress_adds_up_owned_issues(db_path, monkeypatch):
    monkeypatch.setattr(harvest, "_images_pending", lambda path: 0)
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 1.0, None, 1, metron_issue_id=1, path=db_path)
    db.upsert_issue_status(sid, 2.0, None, 1, path=db_path)
    rec.write_issue(sid, 1.0, {"desc": "x", "credits": [], "covers": []}, "metron", "full", {}, db_path)
    rec.write_issue(sid, 2.0, {"desc": "y", "credits": [], "covers": []}, "comicinfo", "partial", {}, db_path)
    p = harvest.progress(db_path)["owned_issues"]
    assert (p["owned"], p["catalogue"], p["stopgap"], p["no_catalogue_id"]) == (2, 1, 1, 1)


# --- 1d: what Metron says beyond the modal's needs -------------------------------
METRON_ISSUE = {
    "id": 181715, "number": "1", "title": "", "name": ["Chapter One"], "desc": "Peter Parker…",
    "series": {"id": 16885, "name": "Midnight Spider-Man", "volume": 1, "series_type": {"id": 2, "name": "Limited Series"}},
    "publisher": {"id": 1, "name": "Marvel"}, "imprint": None, "rating": {"id": 3, "name": "Teen"},
    "upc": "75960621430800111", "sku": "AUG260001", "cv_id": 1196213, "gcd_id": 2950001,
    "characters": [{"id": 1, "name": "Spider-Man"}, {"id": 2, "name": "Norman Osborn"}],
    "teams": [{"id": 9, "name": "Oscorp"}], "universes": [], "reprints": [],
    "credits": [], "variants": [], "image": "https://static.metron.cloud/x.jpg",
    "store_date": "2026-10-07", "cover_date": "2026-12-01", "page": 32, "price": "4.99", "isbn": "",
}


def test_issue_detail_keeps_the_extra_fields(monkeypatch):
    monkeypatch.setattr(mc, "_get", lambda path, **k: METRON_ISSUE)
    ex = mc.issue_detail(181715)["extra"]
    assert (ex["cv_id"], ex["gcd_id"], ex["upc"], ex["age_rating"], ex["series_type"]) == \
           (1196213, 2950001, "75960621430800111", "Teen", "Limited Series")
    assert ex["characters"] == ["Spider-Man", "Norman Osborn"] and ex["teams"] == ["Oscorp"]
    assert ex["story_titles"] == ["Chapter One"]
    assert "imprint" not in ex and "universes" not in ex and "title" not in ex     # absent, not empty


def test_extra_lands_in_the_record_and_survives_a_locg_write(db_path):
    sid = db.add_series(title="Midnight Spider-Man", publisher="Marvel", folder_path=None, on_pull_list=False, path=db_path)
    row = rec.write_issue(sid, 1.0, {"desc": "m", "extra": {"cv_id": 1196213, "characters": ["Spider-Man"]}},
                          "metron", "full", {"metron_issue_id": 181715}, db_path)
    assert row["extra"]["characters"] == ["Spider-Man"] and row["cv_issue_id"] == "1196213"
    row = rec.write_issue(sid, 1.0, {"desc": "from LOCG"}, "locg", "partial", {}, db_path)
    assert row["extra"]["cv_id"] == 1196213                       # never wiped by a source that brings none


def test_owned_metron_rows_without_extra_are_asked_once_more(db_path):
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 1.0, None, 1, metron_issue_id=1, path=db_path)    # owned
    db.upsert_issue_status(sid, 2.0, None, 0, metron_issue_id=2, path=db_path)    # not owned
    for n in (1.0, 2.0):
        rec.write_issue(sid, n, {"desc": "old fill"}, "metron", "full", {"metron_issue_id": int(n)}, db_path)
    assert [c["number"] for c in rec.pending_issues(db_path)] == [1.0]
    assert rec._fresh(rec.get_issue(sid, 1.0, db_path)) is False
    rec.write_issue(sid, 1.0, {"desc": "new", "extra": {"cv_id": 5}}, "metron", "full", {"metron_issue_id": 1}, db_path)
    assert rec.pending_issues(db_path) == []


def test_metron_with_nothing_extra_is_not_asked_forever(db_path):
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 1.0, None, 1, metron_issue_id=1, path=db_path)
    rec.write_issue(sid, 1.0, {"desc": "x", "extra": {}}, "metron", "full", {"metron_issue_id": 1}, db_path)
    assert rec._fresh(rec.get_issue(sid, 1.0, db_path)) is True
    assert rec.pending_issues(db_path) == []


def test_runs_matched_after_their_last_sync_are_synced_most_owned_first(db_path, monkeypatch):
    monkeypatch.setattr(mc, "configured", lambda: True)
    monkeypatch.setattr(mc, "harvest_share_spent", lambda: False)
    a = db.add_series(title="Hellblazer", publisher="DC", folder_path=None, on_pull_list=False, path=db_path)
    b = db.add_series(title="The Woods", publisher="BOOM", folder_path=None, on_pull_list=False, path=db_path)
    c = db.add_series(title="Fetched", publisher="X", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as conn:
        conn.execute("UPDATE tracked_series SET metron_series_id = 1 WHERE id IN (?, ?, ?)", (a, b, c))
        conn.execute("UPDATE tracked_series SET metron_fetched_at = '2026-10-01 00:00:00' WHERE id = ?", (c,))
    for n in range(1, 4):
        db.upsert_issue_status(a, float(n), None, 1, path=db_path)
    db.upsert_issue_status(b, 1.0, None, 1, path=db_path)
    db.upsert_issue_status(c, 1.0, None, 1, path=db_path)
    synced = []
    assert harvest.issue_ids_tick(limit=5, path=db_path, sync=lambda s: synced.append(s["title"])) == 2
    assert synced == ["Hellblazer", "The Woods"]


def test_issue_ids_tick_rests_when_the_harvest_share_is_spent(db_path, monkeypatch):
    monkeypatch.setattr(mc, "configured", lambda: True)
    monkeypatch.setattr(mc, "harvest_share_spent", lambda: True)
    a = db.add_series(title="Hellblazer", publisher="DC", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as conn:
        conn.execute("UPDATE tracked_series SET metron_series_id = 1 WHERE id = ?", (a,))
    db.upsert_issue_status(a, 1.0, None, 1, path=db_path)
    assert harvest.issue_ids_tick(path=db_path, sync=lambda s: 1 / 0) == 0


def test_split_candidates_from_local_data(db_path):
    f = db.add_series(title="Fables", publisher="DC", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as conn:
        conn.execute("UPDATE tracked_series SET metron_series_id = 793 WHERE id = ?", (f,))
    for n in range(148, 154):
        db.upsert_issue_status(f, float(n), None, 1, metron_issue_id=n if n <= 149 else None, path=db_path)
    ff = db.add_series(title="Fantastic Four", publisher="Marvel", folder_path=None, on_pull_list=False, path=db_path)
    for n in range(500, 530):
        db.upsert_issue_status(ff, float(n), None, 1, path=db_path)
    small = db.add_series(title="One-shot", publisher="X", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(small, 1.0, None, 1, path=db_path)
    c = harvest.split_candidates(db_path)
    assert [r["title"] for r in c["outside"]] == ["Fables"]
    assert [r["title"] for r in c["unmatched"]] == ["Fantastic Four"]
