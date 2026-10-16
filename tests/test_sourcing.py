"""Tests for LOCG-Only Series Credit Sourcing (kometa/sourcing.py, Task B).

Covers:
- Scorer across all 4 dimensions (title, imprint/publisher, issue count total, year)
- Imprint table concordance
- Confidence tiers and threshold cutoffs
- Background sourcing_tick persistence and rate limit handling
- API endpoints: preview, confirm (writes metron_series_id only), decline (metron_link = 'none')
"""
import json
import pytest
import kometa.db as db
import kometa.sourcing as src
import kometa.main as main
from test_auth import TestClient


# --- 1. Title Scoring ---

def test_score_title_dimensions():
    # Exact normalized match: 40 pts
    assert src.score_title("East of West", "East of West") == 40.0
    assert src.score_title("Hellblazer - Bad Blood", "Hellblazer: Bad Blood") == 40.0

    # Variant match (leading the / year): 35 pts
    assert src.score_title("The Department of Truth", "Department of Truth") == 35.0
    assert src.score_title("Batman (2016)", "Batman") == 35.0

    # Stripped edition / subtitle: 25 pts
    assert src.score_title("Mister Miracle: The Deluxe Edition", "Mister Miracle") == 25.0
    assert src.score_title("Saga Vol. 1", "Saga") == 25.0
    assert src.score_title("Five Years - Omnibus", "Five Years") == 25.0

    # Non-match: 0 pts
    assert src.score_title("Batman", "Superman") == 0.0
    assert src.score_title("", "East of West") == 0.0


# --- 2. Publisher & Imprint Concordance ---

def test_score_publisher_and_imprint_table():
    # Exact publisher match: 30 pts
    assert src.score_publisher("Image Comics", "Image Comics") == 30.0
    assert src.score_publisher("Marvel", "Marvel Comics") == 30.0

    # Imprint family concordance: 25 pts
    assert src.is_imprint_concordant("Skybound", "Image") is True
    assert src.score_publisher("Skybound", "Image") == 25.0

    assert src.is_imprint_concordant("Vertigo", "DC Comics") is True
    assert src.score_publisher("Vertigo", "DC") == 25.0

    assert src.is_imprint_concordant("Wildstorm", "DC Comics") is True
    assert src.score_publisher("Wildstorm", "DC") == 25.0

    assert src.is_imprint_concordant("MAX", "Marvel") is True
    assert src.score_publisher("MAX", "Marvel") == 25.0

    assert src.is_imprint_concordant("Top Shelf", "IDW") is True
    assert src.score_publisher("Top Shelf", "IDW") == 25.0

    assert src.is_imprint_concordant("Kaboom", "Boom! Studios") is True
    assert src.score_publisher("Kaboom", "Boom! Studios") == 25.0

    # Unrelated publisher: 0 pts
    assert src.is_imprint_concordant("DC Comics", "Marvel Comics") is False
    assert src.score_publisher("DC Comics", "Marvel Comics") == 0.0
    assert src.score_publisher("Image Comics", "Dark Horse") == 0.0
    assert src.score_publisher(None, "Image") == 0.0


# --- 3. Issue Count (Shelf Total Owned + Missing) ---

def test_score_issue_count_compares_against_total():
    # Exact count match: 20 pts
    assert src.score_issue_count(45, 45) == 20.0
    assert src.score_issue_count(1, 1) == 20.0

    # Within +/- 1 issue: 15 pts
    assert src.score_issue_count(45, 44) == 15.0
    assert src.score_issue_count(45, 46) == 15.0

    # Within +/- 2 issues: 5 pts
    assert src.score_issue_count(45, 43) == 5.0
    assert src.score_issue_count(45, 47) == 5.0

    # Diff >= 3 issues: 0 pts
    assert src.score_issue_count(45, 42) == 0.0
    assert src.score_issue_count(45, 10) == 0.0

    # Unknown on either side: 10 pts (neutral)
    assert src.score_issue_count(None, 45) == 10.0
    assert src.score_issue_count(45, None) == 10.0
    assert src.score_issue_count(0, 10) == 10.0


# --- 4. Year Concordance ---

def test_score_year_dimensions():
    # Exact year: 10 pts
    assert src.score_year(2013, 2013) == 10.0

    # Within +/- 1 year: 7 pts
    assert src.score_year(2013, 2012) == 7.0
    assert src.score_year(2013, 2014) == 7.0

    # Diff >= 2 years: 0 pts
    assert src.score_year(2013, 2010) == 0.0
    assert src.score_year(2013, 2016) == 0.0

    # Unknown on either side: 5 pts (neutral)
    assert src.score_year(None, 2013) == 5.0
    assert src.score_year(2013, None) == 5.0


# --- 5. Candidate Total Score & Confidence Thresholds ---

def test_score_candidate_and_thresholds():
    shelf = {"title": "East of West", "publisher": "Image Comics", "year_began": 2013}
    cand_high = {"title": "East of West", "publisher": "Image", "year": 2013, "issue_count": 45}

    # 40 (title) + 30 (pub) + 20 (issues) + 10 (year) = 100.0 -> High
    sc_high = src.score_candidate(shelf, cand_high, shelf_total=45)
    assert sc_high == 100.0
    assert src.confidence_tier(sc_high) == "high"

    # Half-collected run: owned 12, missing 33 -> shelf total is 45!
    # Cand issue_count 45 must match total, giving 20 pts
    sc_half = src.score_candidate(shelf, cand_high, shelf_total=45)
    assert sc_half == 100.0

    # Imprint concordance (Skybound vs Image = 25), year +/- 1 (7 pts)
    # 40 + 25 + 20 + 7 = 92.0 -> High
    cand_skybound = {"title": "East of West", "publisher": "Skybound", "year": 2014, "issue_count": 45}
    sc_sky = src.score_candidate(shelf, cand_skybound, shelf_total=45)
    assert sc_sky == 92.0
    assert src.confidence_tier(sc_sky) == "high"

    # Moderate tier: Title exact (40) + Pub exact (30) + count mismatch (0) + year mismatch (0) = 70.0
    cand_mod = {"title": "East of West", "publisher": "Image", "year": 2005, "issue_count": 4}
    sc_mod = src.score_candidate(shelf, cand_mod, shelf_total=45)
    assert sc_mod == 70.0
    assert src.confidence_tier(sc_mod) == "moderate"

    # Sub-50 discard: Title stripped (25) + count mismatch (0) + pub exact (30) + year mismatch (0) = 55 -> moderate
    # But stripped title (25) + unrelated pub (0) -> rejected by guard -> 0.0
    cand_unrelated = {"title": "East of West Vol. 1", "publisher": "Dark Horse", "year": 2013, "issue_count": 45}
    assert src.score_candidate(shelf, cand_unrelated, shelf_total=45) == 0.0
    assert src.confidence_tier(0.0) is None


# --- 6. Background sourcing_tick & Persistence ---

def test_sourcing_tick_persists_once_and_respects_limits(db_path, monkeypatch):
    monkeypatch.setattr(src, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DB_PATH", db_path)

    # Insert LOCG-only series (no Metron ID, no metron_candidates_json)
    sid = db.add_series(title="The Department of Truth", publisher="Image Comics", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET locg_series_id = 99999, metron_series_id = NULL, year_began = 2020 WHERE id = ?", (sid,))
        # 10 owned, 22 missing = total 32
        for num in range(1, 11):
            c.execute("INSERT INTO issue_status (tracked_series_id, number, owned) VALUES (?, ?, 1)", (sid, num))
        for num in range(11, 33):
            c.execute("INSERT INTO issue_status (tracked_series_id, number, owned) VALUES (?, ?, 0)", (sid, num))

    class FakeClient:
        searches = []
        def configured(self):
            return True
        def search_series(self, q):
            self.searches.append(q)
            return [
                {"id": 7777, "name": "The Department of Truth", "publisher": "Image", "year": 2020, "issue_count": 32},
                {"id": 8888, "name": "Department of Truth Specials", "publisher": "Image", "year": 2021, "issue_count": 2},
            ]

    fake = FakeClient()
    n = src.sourcing_tick(limit=5, path=db_path, client=fake)
    assert n == 1
    assert len(fake.searches) >= 1

    # Verify persisted in DB
    with db._connect(db_path) as c:
        row = c.execute("SELECT metron_candidates_json FROM tracked_series WHERE id = ?", (sid,)).fetchone()
        cands = json.loads(row[0])
        assert len(cands) == 1  # Only 7777 meets threshold, 8888 scored < 50
        assert cands[0]["id"] == 7777
        assert cands[0]["confidence"] == "high"
        assert cands[0]["score"] == 100.0

    # Next tick: series already has metron_candidates_json, so it is NOT searched again
    fake.searches.clear()
    n2 = src.sourcing_tick(limit=5, path=db_path, client=fake)
    assert n2 == 0
    assert len(fake.searches) == 0


# --- 7. API Endpoints ---

def test_sourcing_endpoints_outcomes(db_path, monkeypatch):
    import kometa.auth as auth
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(src, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    auth.forget_enabled()

    sid = db.add_series(title="East of West", publisher="Image Comics", folder_path=None, on_pull_list=False, path=db_path)
    cands_data = [{
        "id": 1234,
        "title": "East of West",
        "publisher": "Image",
        "year": 2013,
        "issue_count": 45,
        "score": 100.0,
        "confidence": "high",
    }]
    with db._connect(db_path) as c:
        c.execute("""UPDATE tracked_series
                     SET locg_series_id = 888, metron_series_id = NULL, metron_candidates_json = ?
                     WHERE id = ?""", (json.dumps(cands_data), sid))

    client = TestClient(main.app)

    # 1. GET /api/sourcing/candidates
    res = client.get("/api/sourcing/candidates")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == sid
    assert data["items"][0]["candidates"][0]["id"] == 1234

    # 2. GET /api/sourcing/preview/{metron_id}
    import kometa.metron_client as mc
    def fake_detail(mid):
        if mid == 1234:
            return {"id": 1234, "name": "East of West", "publisher": {"name": "Image Comics"}, "year": 2013, "issue_count": 45}
        return None
    monkeypatch.setattr(mc, "series_detail", fake_detail)

    prev = client.get("/api/sourcing/preview/1234")
    assert prev.status_code == 200
    pdata = prev.json()
    assert pdata["title"] == "East of West"
    assert pdata["publisher"] == "Image Comics"
    assert pdata["year"] == 2013
    assert pdata["issue_count"] == 45

    # 3. POST /api/sourcing/confirm: writes metron_series_id and nothing else
    conf = client.post("/api/sourcing/confirm", json={"series_id": sid, "metron_series_id": 1234})
    assert conf.status_code == 200
    s_row = db.get_series_by_id(sid, db_path)
    assert s_row["metron_series_id"] == 1234
    assert s_row["locg_series_id"] == 888  # Unchanged
    assert s_row["title"] == "East of West"  # Unchanged

    # Series no longer shows in /api/sourcing/candidates because metron_series_id IS NOT NULL
    res2 = client.get("/api/sourcing/candidates")
    assert len(res2.json()["items"]) == 0

    # 4. POST /api/sourcing/decline: sets metron_link = 'none'
    sid2 = db.add_series(title="Another Run", publisher="Marvel", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as c:
        c.execute("""UPDATE tracked_series
                     SET locg_series_id = 999, metron_series_id = NULL, metron_candidates_json = ?
                     WHERE id = ?""", (json.dumps(cands_data), sid2))

    dec = client.post("/api/sourcing/decline", json={"series_id": sid2})
    assert dec.status_code == 200
    s_row2 = db.get_series_by_id(sid2, db_path)
    assert s_row2["metron_link"] == "none"
    assert s_row2["metron_series_id"] is None

    # Series no longer in candidates because metron_link == 'none'
    res3 = client.get("/api/sourcing/candidates")
    assert len(res3.json()["items"]) == 0


def test_the_owners_no_run_answers_are_never_sourced(db_path, monkeypatch):
    """d78300f dropped the metron_link filter so the 113 historically-stamped
    LOCG-only series get searched — right, that stamp was a machine's. But an
    owner's own 'no run' answer (Needs matching → none) writes locg_link='none',
    and since 2026-10-11 the NEVER stamp on metron_checked_at. Those two marks
    are a person's decision and the sourcing deck never re-opens them."""
    monkeypatch.setattr(src, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    def locg_only(title, **cols):
        sid = db.add_series(title=title, publisher="Image Comics", folder_path=None, on_pull_list=False, path=db_path)
        sets = ", ".join(f"{k} = ?" for k in cols)
        with db._connect(db_path) as c:
            c.execute(f"UPDATE tracked_series SET locg_series_id = 4242, metron_series_id = NULL, metron_link = 'none', match_status = 'manual'{', ' + sets if sets else ''} WHERE id = ?",
                      (*cols.values(), sid))
        return sid
    machine = locg_only("Aliens - Book One")                                   # bulk pass stamped it: fair game
    declined = locg_only("Tintin VS Batman", locg_link="none")                # the owner said no run
    never = locg_only("Five Years- Omnibus", metron_checked_at="9999-12-31 00:00:00")
    picked = {r["id"] for r in src.sourcing_candidates_query(path=db_path, limit=10)}
    assert machine in picked and declined not in picked and never not in picked
