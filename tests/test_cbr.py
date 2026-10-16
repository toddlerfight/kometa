"""Tests for CBR matching, slug normalisation, and year hints (kometa/cbr.py)."""
import kometa.cbr as cbr
import kometa.db as db


def test_series_slug_normalisation():
    # Basic slugging
    assert cbr.series_slug("The Amazing Spider-Man (2025)") == "amazing-spider-man"
    assert cbr.series_slug("Miles Morales: Spider-Man") == "miles-morales-spider-man"
    assert cbr.series_slug("Alien Vs. X-Men") == "alien-vs.-x-men"

    # Edition and subtitle stripping
    assert cbr.series_slug("Mister Miracle - The Deluxe Edition") == "mister-miracle"
    assert cbr.series_slug("Mister Miracle: Deluxe Edition") == "mister-miracle"
    assert cbr.series_slug("Five Years - Omnibus") == "five-years"
    assert cbr.series_slug("Batman - The Dark Knight Returns 10th Anniversary Edition") == "batman-the-dark-knight-returns"
    assert cbr.series_slug("Saga Vol. 1") == "saga"
    assert cbr.series_slug("Aliens - Tribes GN") == "aliens-tribes"

    # Leading hyphen and track number prefixes
    assert cbr.series_slug("- Army of Darkness") == "army-of-darkness"
    assert cbr.series_slug("- Batman - Lost") == "batman-lost"
    assert cbr.series_slug("- Hawkman - Found") == "hawkman-found"
    assert cbr.series_slug("1 - Silent Night") == "silent-night"
    assert cbr.series_slug("6 - Daddy's Little Girl") == "daddys-little-girl"
    assert cbr.series_slug("1602- Witch Hunter Angela") == "1602-witch-hunter-angela"



def test_year_hint_from_metron_series_fallback(db_path, monkeypatch):
    import kometa.metron_client as mc
    sid = db.add_series(title="Eight Billion Genies", publisher="Image Comics", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET metron_series_id = 3364, year_began = NULL WHERE id = ?", (sid,))
    # No store date on issue
    db.upsert_issue_status(sid, 1.0, None, 1, path=db_path)

    calls = []
    def fake_detail(mid):
        calls.append(mid)
        return {"year": 2022} if mid == 3364 else {}

    # When no store date exists, year_hint falls back to Metron series detail
    monkeypatch.setattr(mc, "series_detail", fake_detail)

    s = db.get_series_by_id(sid, db_path)
    assert cbr.year_hint(s, path=db_path) == 2022
    assert len(calls) == 1
    assert s.get("year_began") == 2022

    # Verify persisted to DB
    row = db.get_series_by_id(sid, db_path)
    assert row["year_began"] == 2022

    # Second call uses persisted year_began and costs 0 API calls
    assert cbr.year_hint(row, path=db_path) == 2022
    assert len(calls) == 1


def test_year_hint_negative_result_cached_in_metron_checked_at(db_path, monkeypatch):
    import kometa.metron_client as mc
    sid = db.add_series(title="Yearless Series", publisher="Unknown", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE tracked_series SET metron_series_id = 9999, year_began = NULL, metron_checked_at = NULL WHERE id = ?", (sid,))

    calls = []
    def fake_detail(mid):
        calls.append(mid)
        return {}   # Metron has no year for this run

    monkeypatch.setattr(mc, "series_detail", fake_detail)

    s = db.get_series_by_id(sid, db_path)
    assert cbr.year_hint(s, path=db_path) is None
    assert len(calls) == 1

    # Verify metron_checked_at was stamped and year_began was NOT set to 0
    row = db.get_series_by_id(sid, db_path)
    assert row["year_began"] is None
    assert row["metron_checked_at"] is not None

    # Next call within 30 days skips the Metron API call
    assert cbr.year_hint(row, path=db_path) is None
    assert len(calls) == 1


def test_matches_handles_subtitled_runs_and_verifies_year():
    # Matching clean title against page heading
    s = {"title": "Mister Miracle - The Deluxe Edition"}
    page = {"name": "Mister Miracle", "start_year": 2017}

    # Year agrees within 1 year
    assert cbr.matches(s, page, 2017) is True
    assert cbr.matches(s, page, 2018) is True

    # Year mismatch
    assert cbr.matches(s, page, 2015) is False

    # Missing year on page or input must remain False (unsure)
    assert cbr.matches(s, {"name": "Mister Miracle", "start_year": None}, 2017) is False
    assert cbr.matches(s, page, None) is False


def test_candidate_urls_keep_the_bare_slug_and_add_the_the_variant():
    """Half the live links (142 of 281, 2026-10-16) are bare-slug pages with no year:
    that candidate is never dropped. 'The' titles also get the 'the-' pair."""
    B = "https://comicbookroundup.com/comic-books/reviews/image-comics/"
    s_the = {"title": "The Department of Truth", "publisher": "Image Comics"}
    assert cbr.candidate_urls(s_the, 2020) == [
        B + "department-of-truth-(2020)", B + "the-department-of-truth-(2020)",
        B + "department-of-truth", B + "the-department-of-truth",
    ]
    assert cbr.candidate_urls({"title": "Saga", "publisher": "Image Comics"}, 2012) == [B + "saga-(2012)", B + "saga"]
    assert cbr.candidate_urls(s_the, None) == []
    assert cbr.candidate_urls({"title": "The Flash", "publisher": None}, 2023) == []


def test_ensure_tables_the_reprobe_resets_none_series_once(db_path):
    cbr.ensure_tables.reset()
    cbr.ensure_tables(db_path)
    with db._connect(db_path) as conn:
        conn.execute("DELETE FROM config WHERE key = 'cbr_the_reprobe'")
    db._config_cache.pop(db_path, None)

    sid1 = db.add_series(title="The Department of Truth", publisher="Image Comics", folder_path=None, on_pull_list=False, path=db_path)
    sid2 = db.add_series(title="Batman", publisher="DC Comics", folder_path=None, on_pull_list=False, path=db_path)
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO cbr_series (tracked_series_id, state, at) VALUES (?, 'none', '2026-10-15 12:00:00')", (sid1,))
        conn.execute("INSERT INTO cbr_series (tracked_series_id, state, at) VALUES (?, 'none', '2026-10-15 12:00:00')", (sid2,))

    cbr.ensure_tables.reset()
    cbr.ensure_tables(db_path)

    with db._connect(db_path) as conn:
        r1 = conn.execute("SELECT at FROM cbr_series WHERE tracked_series_id = ?", (sid1,)).fetchone()
        r2 = conn.execute("SELECT at FROM cbr_series WHERE tracked_series_id = ?", (sid2,)).fetchone()
        assert r1[0] == "1970-01-01 00:00:00"
        assert r2[0] == "2026-10-15 12:00:00"
        assert db.get_config(db_path).get("cbr_the_reprobe") == "1"

