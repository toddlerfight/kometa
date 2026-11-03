"""LOCG's series variants list as a top-up while its issue pages are walled (2026-11-04: Absolute
Batman #25 had Metron's 10 covers; the list held 148). The list is partial, so it only ever ADDS."""
from datetime import date, timedelta

import kometa.db as db
import kometa.locg_client as lc
import kometa.locg_sweep as sweep
import kometa.record as rec


def _row(parent, vid, title):
    return (f'<li class="issue" data-comic="{vid}" data-parent="{parent}"><div class="cover">'
            f'<a href="/comic/{parent}/x?variant={vid}"><img data-src="https://s3/x-{vid}.jpg"></a></div>'
            f'<div class="title color-primary" data-sorting="{title}"><a>{title}</a></div></li>')


LIST_HTML = "<ul>" + "".join([
    _row("26", "2601", "Absolute Batman #26 Cover B Cliff Chiang Variant"),
    _row("25", "2501", "Absolute Batman #25 616 Comics Carla Cohen Variant"),
    _row("25", "2502", "Absolute Batman #25 Cover B J. Scott Campbell Variant"),   # Metron has this one by name
    _row("25", "2501", "Absolute Batman #25 616 Comics Carla Cohen Variant"),       # LOCG repeats rows
]) + "</ul>"


class _Resp:
    def __init__(self, html):
        self._html = html

    def json(self):
        return {"list": self._html}


def test_the_list_parses_by_issue_with_the_issue_page_names(monkeypatch):
    lc._series_variant_cache.clear()
    calls = []
    def get(url, params=None, **kw):
        calls.append(params)
        return _Resp(LIST_HTML)
    got = lc._series_variants_with_get(178012, get)
    assert sorted(got) == ["25", "26"]
    assert [(c["id"], c["name"]) for c in got["25"]] == [("2501", "616 Comics Carla Cohen Variant"),
                                                          ("2502", "Cover B J. Scott Campbell Variant")]
    assert got["25"][0]["large"] == lc.S3_LARGE.format("2501") and got["25"][0]["thumb"] == lc.S3_THUMB.format("2501")
    assert calls[0]["format[]"] == 2
    lc._series_variants_with_get(178012, get)                       # one fetch serves the run
    assert len(calls) == 1
    lc._series_variant_cache.clear()


METRON = {"covers": [{"id": "m1", "name": "Cover A (Main)", "thumb": "https://m/a.jpg", "large": "https://m/a.jpg"},
                     {"id": "m2", "name": "Cover B J. Scott Campbell Variant", "thumb": "https://m/b.jpg", "large": "https://m/b.jpg"}]}
LISTED = [{"id": "2501", "name": "616 Comics Carla Cohen Variant", "thumb": "t1", "large": "l1"},
          {"id": "2502", "name": "Cover B J. Scott Campbell Variant", "thumb": "t2", "large": "l2"}]


def _wall(lid):
    raise RuntimeError("HTTP 500 for https://leagueofcomicgeeks.com/comic/25/comic")


def _issue(db_path, metron=True, store_date="2026-10-28"):
    sid = db.add_series(title="Absolute Batman", publisher="DC", folder_path=None, on_pull_list=True,
                        locg_series_id=178012, path=db_path)
    db.upsert_issue_status(sid, 25.0, store_date, 1, metron_issue_id=(182969 if metron else None),
                           locg_issue_id="25", path=db_path)
    return sid


def test_a_walled_page_falls_back_to_the_list_and_adds(db_path):
    sid = _issue(db_path)
    asked = []
    covers = rec.fill_variants(sid, 25.0, db_path, force=True, metron=lambda i, p: METRON, locg=_wall,
                               locg_open=lambda: True, locg_list=lambda lid: asked.append(lid) or LISTED)
    assert asked == ["25"]
    assert [(c["name"], c["source"]) for c in covers] == [("Cover A (Main)", "metron"),
                                                          ("Cover B J. Scott Campbell Variant", "metron"),
                                                          ("616 Comics Carla Cohen Variant", "locg")]
    assert len(rec.get_issue(sid, 25.0, db_path)["covers"]) == 3


def test_the_list_is_partial_so_stored_locg_covers_stay(db_path):
    sid = _issue(db_path)
    page = [{"id": "25", "name": "Cover A (Main)", "thumb": "s", "large": "s"},
            {"id": "2599", "name": "Big Time Collectibles Variant", "thumb": "t9", "large": "l9"}]
    rec.fill_variants(sid, 25.0, db_path, force=True, metron=lambda i, p: METRON, locg=lambda lid: page, locg_open=lambda: True)
    covers = rec.fill_variants(sid, 25.0, db_path, force=True, metron=lambda i, p: METRON, locg=_wall,
                               locg_open=lambda: True, locg_list=lambda lid: LISTED)
    assert {c["name"] for c in covers if c["source"] == "locg"} == {"Big Time Collectibles Variant", "616 Comics Carla Cohen Variant"}


def test_a_page_that_answers_is_the_list_and_the_series_list_isnt_asked(db_path):
    sid = _issue(db_path)
    def nope(lid):
        raise AssertionError("series list asked")
    covers = rec.fill_variants(sid, 25.0, db_path, force=True, metron=lambda i, p: METRON,
                               locg=lambda lid: LISTED[:1], locg_open=lambda: True, locg_list=nope)
    assert [c["name"] for c in covers][-1] == "616 Comics Carla Cohen Variant"


def test_a_locg_only_issue_gets_its_main_from_s3_ahead_of_the_list(db_path):
    sid = _issue(db_path, metron=False)
    covers = rec.fill_variants(sid, 25.0, db_path, force=True, locg=_wall, locg_open=lambda: True, locg_list=lambda lid: LISTED)
    assert [c["id"] for c in covers] == ["25", "2501", "2502"]
    assert covers[0]["large"] == lc.S3_LARGE.format("25")


def test_an_empty_or_failing_list_changes_nothing(db_path):
    sid = _issue(db_path)
    first = rec.fill_variants(sid, 25.0, db_path, force=True, metron=lambda i, p: METRON, locg=_wall,
                              locg_open=lambda: True, locg_list=lambda lid: None)
    assert [c["source"] for c in first] == ["metron", "metron"]
    def boom(lid):
        raise RuntimeError("FlareSolverr timeout")
    again = rec.fill_variants(sid, 25.0, db_path, force=True, metron=lambda i, p: METRON, locg=_wall,
                              locg_open=lambda: True, locg_list=boom)
    assert again == first


def test_add_list_variants_tops_up_made_lists_only_and_keeps_their_stamp(db_path):
    sid = _issue(db_path)
    db.upsert_issue_status(sid, 26.0, "2027-01-13", 0, locg_issue_id="26", path=db_path)       # no list made yet
    rec.fill_variants(sid, 25.0, db_path, force=True, metron=lambda i, p: METRON, locg=_wall, locg_open=lambda: True,
                      locg_list=lambda lid: None)
    stamp = rec.get_issue(sid, 25.0, db_path)["variants_at"]
    by_issue = {"25": LISTED + [LISTED[0]], "26": [{"id": "2601", "name": "Cover B Cliff Chiang Variant", "thumb": "a", "large": "b"}]}
    assert rec.add_list_variants(sid, db_path, fetch=lambda ls: by_issue) == 1
    row = rec.get_issue(sid, 25.0, db_path)
    assert [c["name"] for c in row["covers"]][-1] == "616 Comics Carla Cohen Variant" and row["variants_at"] == stamp
    assert rec.get_issue(sid, 26.0, db_path) is None                 # a made-up row would keep Metron's fill away
    assert rec.add_list_variants(sid, db_path, fetch=lambda ls: by_issue) == 0   # nothing twice


def test_the_tick_asks_runs_short_of_locg_covers_once_a_day(db_path):
    sweep._list_checked.clear()
    recent = (date.today() - timedelta(days=7)).isoformat()
    sid = _issue(db_path, store_date=recent)
    old = db.add_series(title="Old Run", publisher="DC", folder_path=None, on_pull_list=True, locg_series_id=5, path=db_path)
    db.upsert_issue_status(old, 1.0, "2019-01-02", 1, metron_issue_id=7, locg_issue_id="77", path=db_path)
    for s, n in ((sid, 25.0), (old, 1.0)):
        rec.fill_variants(s, n, db_path, force=True, metron=lambda i, p: METRON, locg=_wall, locg_open=lambda: True,
                          locg_list=lambda lid: None)
    assert sweep.list_candidates(db_path) == [sid]                  # an old run isn't this job's
    asked = []
    out = sweep.list_tick(path=db_path, is_open=lambda: True, syncing=lambda: False, sleep=lambda s: None,
                          add=lambda s: asked.append(s) or 1)
    assert asked == [sid] and out == {"series": 1, "added": 1, "stopped": None}
    assert sweep.list_candidates(db_path) == []                     # asked today
    assert sweep.list_tick(path=db_path, is_open=lambda: False)["stopped"] == "paused"
    sweep._list_checked.clear()
