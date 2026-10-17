"""Gap fill (kometa/gapfill.py): on-sale dates only from a source that knows the day, cover
dates kept apart and shown as such, descriptions filled only where empty."""
import json

import kometa.db as db
import kometa.gapfill as gf
import kometa.record as record
from kometa import runstatus


def _issue(db_path, n=201.0, status_date=None, desc="", extra=None, source="metron", store_date=None,
           cover_date=None, title="Uncanny X-Men", owned=False):
    sid = db.add_series(title=title, publisher="Marvel", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, n, status_date, owned, path=db_path)
    record.write_issue(sid, n, {"desc": desc, "credits": [], "store_date": store_date, "cover_date": cover_date,
                                "extra": extra if extra is not None else {"cv_id": 5001, "gcd_id": 7001}},
                       source, "full", {"metron_issue_id": 9}, db_path)
    return sid


def _status(db_path, sid, n=201.0):
    with db._connect(db_path) as conn:
        return dict(conn.execute("SELECT * FROM issue_status WHERE tracked_series_id = ? AND number = ?", (sid, n)).fetchone())


class FakeCV:
    def __init__(self, results):
        self.results, self.asked = results, []

    def _get(self, path, **params):
        self.asked.append(params["filter"])
        return {"status_code": 1, "results": self.results}


def test_cover_dates_keep_the_precision_they_have():
    assert gf.cover_month("1994-08-01") == "1994-08"
    assert gf.cover_month("1994-08-00") == "1994-08"
    assert gf.cover_month("1990-00-00") == "1990"
    assert gf.cover_month("") is None and gf.cover_month("0000-00-00") is None
    assert gf.from_publication("September 1994") == "1994-09"
    assert gf.from_publication("Fall 1994") == "1994"
    assert gf.from_publication("Sept 1994") is None or gf.from_publication("Sept 1994") == "1994"
    assert gf.as_date("1994-08") == "1994-08-01" and gf.as_date("1994") == "1994-01-01"


def test_comic_vines_description_loses_its_cover_table():
    raw = ("<p><em>Mutant Massacre</em></p><p>The X-Men face the Marauders &amp; lose.</p>"
           "<h4>List of covers and their creators:</h4><table><tr><td>Reg</td><td>John Romita Jr.</td></tr></table>"
           "<p>Trailing note.</p>")
    assert gf.clean_description(raw) == "Mutant Massacre\n\nThe X-Men face the Marauders & lose."
    assert gf.clean_description("<table><tr><td>x</td></tr></table>", deck="One-line summary.") == "One-line summary."


def test_held_cover_dates_reach_the_issue_without_asking_anyone(db_path):
    a = _issue(db_path, cover_date="1985-08-01")
    b = _issue(db_path, source="comicinfo", store_date="2019-01-01", title="They Called Us Enemy", extra={})
    assert gf.local_covers(db_path, dry=True) == 2
    assert _status(db_path, a)["cover_date"] is None                     # dry: nothing written
    assert gf.local_covers(db_path) == 2
    assert _status(db_path, a)["cover_date"] == "1985-08"
    assert _status(db_path, b)["cover_date"] == "2019"                    # a bare year, not 1 January
    assert _status(db_path, b)["store_date"] is None                      # and never a release date


def test_comic_vine_fills_the_on_sale_date_both_places_and_only_empty_descriptions(db_path):
    a = _issue(db_path)
    b = _issue(db_path, n=202.0, desc="Metron's own words.", extra={"cv_id": 5002})
    cv = FakeCV([{"id": 5001, "store_date": "1985-05-14", "cover_date": "1985-08-01", "description": "<p>Rogue joins.</p>"},
                 {"id": 5002, "store_date": None, "cover_date": "1985-09-01", "description": "<p>Not this.</p>"}])
    out = gf.cv_fill(db_path, calls=1, client=cv)
    assert out == {"asked": 2, "store_date": 1, "desc": 1, "cover_date": 2}
    assert _status(db_path, a)["store_date"] == "1985-05-14"
    ra = record.get_issue(a, 201.0, db_path)
    assert ra["store_date"] == "1985-05-14" and ra["desc"] == "Rogue joins."
    assert ra["field_sources"]["store_date"]["source"] == "comicvine"
    assert record.get_issue(b, 202.0, db_path)["desc"] == "Metron's own words."   # a catalogue's text is never replaced
    assert _status(db_path, b, 202.0)["store_date"] is None and _status(db_path, b, 202.0)["cover_date"] == "1985-09"
    assert gf.cv_wanted(db_path) == []                                    # asked: not again for RETRY_DAYS


def test_a_dry_run_writes_nothing(db_path):
    a = _issue(db_path)
    cv = FakeCV([{"id": 5001, "store_date": "1985-05-14", "cover_date": "1985-08-01", "description": "<p>Rogue joins.</p>"}])
    assert gf.cv_fill(db_path, calls=1, client=cv, dry=True)["store_date"] == 1
    assert _status(db_path, a)["store_date"] is None and record.get_issue(a, 201.0, db_path)["desc"] == ""
    assert len(gf.cv_wanted(db_path)) == 1


def test_gcd_gives_its_on_sale_day_never_its_cover_key(db_path):
    a = _issue(db_path, extra={"gcd_id": 7001})                          # no Comic Vine id: GCD's turn
    raw = {"on_sale_date": "", "key_date": "1985-08-00", "publication_date": "August 1985",
           "story_set": [{"type": "cover", "synopsis": "cover"}, {"type": "comic story", "synopsis": "The Marauders strike."}]}
    out = gf.gcd_fill(db_path, fetch=lambda g: raw)
    assert out["store_date"] == 0 and out["desc"] == 1 and out["cover_date"] == 1
    st = _status(db_path, a)
    assert st["store_date"] is None and st["cover_date"] == "1985-08"
    assert record.get_issue(a, 201.0, db_path)["desc"] == "The Marauders strike."


def test_a_gcd_date_that_was_its_cover_key_moves_to_the_cover(db_path):
    # filled before 2026-10-18: GCD's key_date stood in as the on-sale date
    a = _issue(db_path, source="gcd", store_date="1990-01-01", status_date="1990-01-01", extra={"gcd_id": 7001},
               title="The Thanos Quest")
    raw = {"on_sale_date": "", "key_date": "1990-00-00", "publication_date": "1990", "story_set": []}
    assert gf.gcd_fill(db_path, fetch=lambda g: raw)["date_cleared"] == 1
    st = _status(db_path, a)
    assert st["store_date"] is None and st["cover_date"] == "1990"
    assert record.get_issue(a, 201.0, db_path)["store_date"] is None
    assert gf.gcd_wanted(db_path) == []                                   # settled


def test_a_catalogue_refresh_with_nothing_keeps_what_the_gap_fill_found(db_path):
    a = _issue(db_path)
    gf.cv_fill(db_path, calls=1, client=FakeCV([{"id": 5001, "store_date": "1985-05-14", "description": "<p>Rogue joins.</p>"}]))
    record.write_issue(a, 201.0, {"desc": "", "credits": [], "extra": {"cv_id": 5001}}, "metron", "full",
                       {"metron_issue_id": 9}, db_path)                   # Metron re-asked: still nothing
    r = record.get_issue(a, 201.0, db_path)
    assert r["desc"] == "Rogue joins." and r["store_date"] == "1985-05-14"


def test_an_old_run_with_only_cover_dates_reads_as_ended():
    issues = [{"store_date": None, "cover_date": "1986-03"}, {"store_date": None, "cover_date": "1986-04"}]
    assert runstatus.compute({}, issues, today="2026-10-18")["label"] == "Ended"
    assert runstatus.compute({}, [{"store_date": None, "cover_date": None}], today="2026-10-18") is None


def test_an_old_issue_with_only_a_cover_date_is_old_for_downloads(db_path, monkeypatch):
    from kometa import acquisition
    monkeypatch.setattr(acquisition, "DB_PATH", db_path)
    a = _issue(db_path, cover_date="1985-08-01")
    gf.local_covers(db_path)
    item = {"tracked_series_id": a, "issue_number": 201.0, "kind": "issue"}
    assert acquisition._issue_age_date(item, None) == "1985-08-01"
    assert acquisition._is_old_issue(acquisition._issue_age_date(item, None))
    assert acquisition._issue_age_date(item, "2026-10-14") == "2026-10-14"   # a real date always wins


def test_the_card_shows_the_issues_own_date_and_its_cover_date(db_path, monkeypatch):
    import kometa.main as main
    monkeypatch.setattr(main, "DB_PATH", db_path)
    a = _issue(db_path, source="comicinfo", store_date="2019-01-01", extra={}, title="They Called Us Enemy")
    gf.local_covers(db_path)
    d = main.get_issue_locg_details(a, 201.0)
    assert d["store_date"] is None                                        # not 'Released January 1, 2019'
    assert d["cover_date"] == "2019"


def test_the_switch_is_off_until_turned_on(db_path):
    _issue(db_path, cover_date="1985-08-01")
    assert gf.tick(db_path) == {}
    db.set_config({"gap_fill": "on"}, db_path)
    assert gf.tick(db_path)["cover_local"] == 1
