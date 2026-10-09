"""Favourites + ratings (kometa/marks.py) and the end screen's next-in-series."""
import pytest

import kometa.db as db
import kometa.marks as mk
from tests.conftest import make_cbz


def _shelf(db_path, tmp_path, n=3, tracked=True):
    folder = tmp_path / "Saga"
    folder.mkdir(parents=True, exist_ok=True)
    tid = db.add_series(title="Saga", publisher="Image", folder_path=str(folder), on_pull_list=False, path=db_path) if tracked else None
    sid = db.upsert_shelf_series(str(folder), "Saga", "Image", tid, n, "2026-10-09T00:00:00Z", db_path)
    rows = []
    for i in range(1, n + 1):
        f = folder / f"Saga #{i:03d}.cbz"
        make_cbz(f)
        rows.append((str(f), 10, 1.0, float(i), sid, tid))
        if tid:
            db.upsert_issue_status(tid, float(i), "2012-01-01", 1, path=db_path)
    db.index_books(rows, db_path)
    return tid, sid, [db.get_book_by_path(r[0], db_path)["id"] for r in rows]


def test_marks_round_trip_and_series_rating_derives_from_issues(db_path, tmp_path):
    tid, sid, ids = _shelf(db_path, tmp_path)
    assert mk.get_mark("book", ids[0], db_path) == {"favourite": False, "rating": None}
    assert mk.set_mark("book", ids[0], favourite=True, path=db_path) == {"favourite": True, "rating": None}
    mk.set_mark("book", ids[0], rating=5, path=db_path)
    mk.set_mark("book", ids[1], rating=4, path=db_path)
    assert mk.get_mark("book", ids[0], db_path) == {"favourite": True, "rating": 5}      # the star survived the rating
    r = mk.series_rating(tid, db_path)
    assert (r["rating"], r["rating_derived"], r["rated_issues"]) == (None, 4.5, 2)
    mk.set_mark("series", tid, rating=3, path=db_path)
    assert mk.series_rating(tid, db_path)["rating"] == 3 and mk.rated_series(db_path)[tid] == 3.0
    with pytest.raises(ValueError):
        mk.set_mark("book", ids[0], rating=9, path=db_path)
    mk.set_mark("book", ids[0], favourite=False, clear_rating=True, path=db_path)
    assert mk.marks_for("book", db_path) == {ids[1]: {"favourite": False, "rating": 4}}  # an empty mark is dropped


def test_favourites_row_lists_series_then_books_newest_first(db_path, tmp_path):
    tid, sid, ids = _shelf(db_path, tmp_path)
    mk.set_mark("book", ids[2], favourite=True, path=db_path)
    mk.set_mark("series", tid, favourite=True, path=db_path)
    f = mk.favourites(db_path)
    assert [(x["kind"], x["label"]) for x in f] == [("series", "Saga"), ("book", "#3")]


def test_next_in_series_prefers_the_file_then_explains_the_gap(db_path, tmp_path):
    tid, sid, ids = _shelf(db_path, tmp_path)
    assert mk.next_in_series(ids[0], db_path)["book_id"] == ids[1]
    db.upsert_issue_status(tid, 4.0, "2099-01-01", 0, path=db_path)
    n = mk.next_in_series(ids[2], db_path)
    assert n["status"] == "upcoming" and n["label"].startswith("#4 out")
    db.upsert_issue_status(tid, 4.0, "2012-05-01", 0, path=db_path)
    assert mk.next_in_series(ids[2], db_path)["status"] == "not_owned"
    db.set_issue_ignored(tid, 4.0, True, db_path)
    assert mk.next_in_series(ids[2], db_path)["status"] == "end"
