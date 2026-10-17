"""Comic Vine by name (kometa/cvname.py): an issue no catalogue handed a Comic Vine id gets
its credits by its run's name — SQUEE!: not on Metron, GCD has only the covers, Comic Vine
has all four with Jhonen Vasquez on everything. No network: a fake Comic Vine."""
import pytest

import kometa.db as db
import kometa.record as record
from kometa import cvname, sources

SQUEE_VOLUMES = [   # what Comic Vine's search answered on 2026-10-17
    {"name": "Squee!", "year": 1997, "publisher": "SLG Publishing", "cv_volume_id": 18715, "issue_count": 4},
    {"name": "SQUEE!", "year": 1998, "publisher": "SLG Publishing", "cv_volume_id": 42168, "issue_count": 1},
]
JHONEN = [{"name": "Jhonen Vasquez", "role": "writer, penciler, letterer, inker, colorist, cover"}]


class FakeCV:
    def __init__(self, volumes=SQUEE_VOLUMES):
        self.volumes, self.calls = volumes, []

    def search_volumes(self, query, limit=12):
        self.calls.append(("search", query))
        return self.volumes

    def _get(self, path, **params):
        self.calls.append((path, params.get("filter")))
        if path == "issues/":
            rows = [{"id": 110628 + i, "issue_number": str(i + 1)} for i in range(4)]
            return {"results": rows, "number_of_total_results": 4}
        return {"status_code": 1, "results": {"person_credits": JHONEN}}


@pytest.fixture
def cv(monkeypatch):
    fake = FakeCV()
    monkeypatch.setattr(sources, "comicvine", lambda: fake)
    monkeypatch.setattr(cvname, "CV_GAP_S", 0)
    cvname._calls.clear()
    yield fake
    cvname._calls.clear()


def _squee(db_path, credits=None, source="gcd", n=1.0, extra=None):
    db.set_config({"cv_api_key": "k", "cv_by_name": "on"}, db_path)   # set up and switched on
    sid = next((s["id"] for s in db.get_all_series(db_path) if s["title"] == "SQUEE!"), None) if hasattr(db, "get_all_series") else None
    if not sid:
        sid = db.add_series(title="SQUEE!", publisher="SLG Publishing", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, n, "1997-04-01", 1, path=db_path)
    record.write_issue(sid, n, {"desc": "", "credits": credits or [], "extra": {"gcd_id": 374638 + int(n)} if extra is None else extra},
                       source, "partial", {}, db_path)
    return sid


def test_names_publishers_and_the_right_squee():
    assert cvname.norm_title("The Dark Knight Returns- the Golden Child") == cvname.norm_title("Dark Knight Returns: The Golden Child")
    assert cvname.same_publisher("Image Comics", "Image") and cvname.same_publisher("BOOM! Studios", "Boom! Studios")
    assert not cvname.same_publisher("SLG Publishing", "Marvel")
    # two SLG 'Squee!' volumes a year apart: the run's own year picks the 1997 one
    vid, why = cvname.pick_volume({"title": "SQUEE!", "publisher": "SLG Publishing"}, 1997, SQUEE_VOLUMES)
    assert vid == 18715 and "1997" in why
    # a name that isn't the same name never matches ('Deluxe Edition' is another book)
    assert cvname.pick_volume({"title": "The Dark Knight Returns- the Golden Child Deluxe Edition", "publisher": "DC Comics"},
                              2019, [{"name": "Dark Knight Returns: The Golden Child", "year": 2019,
                                      "publisher": "DC Comics", "cv_volume_id": 1}])[0] is None
    # another publisher's same-named run never matches
    assert cvname.pick_volume({"title": "SQUEE!", "publisher": "Marvel"}, 1997, SQUEE_VOLUMES)[0] is None


def test_squee_gets_its_credits_by_name_and_the_run_is_matched_once(db_path, cv):
    sid = _squee(db_path)
    _squee(db_path, n=2.0)
    assert (sid, 1.0) in {(r["tracked_series_id"], r["number"]) for r in record.credits_wanted(db_path)}
    out = record.fill_credits(sid, 1.0, db_path, rungs=(("comicvine", record._ask_comicvine),))
    assert out["source"] == "comicvine"
    roles = {c["role"] for c in record.get_issue(sid, 1.0, db_path)["credits"]}
    assert {"Writer", "Penciller", "Inker", "Colorist", "Letterer", "Cover"} <= roles
    record.fill_credits(sid, 2.0, db_path, rungs=(("comicvine", record._ask_comicvine),))
    assert [c for c in cv.calls if c[0] == "search"] == [("search", "SQUEE!")]          # one search for the run
    assert [c for c in cv.calls if c[0] == "issues/"] == [("issues/", "volume:18715")]  # one issue list
    assert cvname.summary(db_path) == {"runs_matched": 1, "runs_unmatched": 0, "issues_mapped": 4}


def test_a_run_with_no_clean_match_is_remembered_and_not_picked_again(db_path, monkeypatch, cv):
    cv.volumes = []
    sid = _squee(db_path, extra={})                       # nothing else to ask: only the name
    assert record.fill_credits(sid, 1.0, db_path, rungs=(("comicvine", record._ask_comicvine),))["source"] is None
    assert cvname.summary(db_path)["runs_unmatched"] == 1
    assert (sid, 1.0) not in {(r["tracked_series_id"], r["number"]) for r in record.credits_wanted(db_path)}


def test_an_issue_with_a_list_is_never_asked_by_name(db_path, cv):
    sid = _squee(db_path, credits=[{"role": "Writer", "name": "Jhonen Vasquez", "metron_creator_id": None}], source="locg")
    assert record._ask_comicvine(dict(record.get_issue(sid, 1.0, db_path), db_path=db_path)) is None
    assert cv.calls == []


def test_the_ladders_comic_vine_hour_has_a_ceiling(monkeypatch, cv):
    monkeypatch.setattr(cvname, "CV_PER_HOUR", 2)
    assert cvname.spend() and cvname.spend()
    assert not cvname.has_budget() and not cvname.spend()


def test_with_comic_vine_off_nothing_is_picked_by_name(db_path, monkeypatch):
    monkeypatch.setattr(sources, "comicvine", lambda: None)
    sid = db.add_series(title="Nobody's Run", publisher="SLG Publishing", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 1.0, "1997-04-01", 1, path=db_path)
    record.write_issue(sid, 1.0, {"desc": "", "credits": [], "extra": {}}, "comicinfo", "partial", {}, db_path)
    assert (sid, 1.0) not in {(r["tracked_series_id"], r["number"]) for r in record.credits_wanted(db_path)}


def test_by_name_is_off_until_switched_on_and_the_switch_is_a_setting(db_path, tmp_path, monkeypatch, cv):
    sid = _squee(db_path, extra={})
    db.set_config({"cv_by_name": "off"}, db_path)
    assert (sid, 1.0) not in {(r["tracked_series_id"], r["number"]) for r in record.credits_wanted(db_path)}
    assert record._ask_comicvine(dict(record.get_issue(sid, 1.0, db_path), db_path=db_path)) is None
    assert cv.calls == []
    from fastapi import HTTPException
    from kometa import main
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "_locg_status", lambda: {})
    monkeypatch.setattr(main, "_comics_root", lambda: str(tmp_path))
    assert main.update_config(main.ConfigRequest(cv_by_name="on"))["cv_by_name"] is True
    assert (sid, 1.0) in {(r["tracked_series_id"], r["number"]) for r in record.credits_wanted(db_path)}
    with pytest.raises(HTTPException):
        main.update_config(main.ConfigRequest(cv_by_name="yes"))
