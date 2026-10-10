"""On Deck rows (kometa/ondeck.py): continue, next, coming soon — from the
reading tables, in Kometa's issue order, with the labels Kometa can stand behind."""
import os

import pytest

import kometa.db as db
import kometa.ondeck as od


def _book(path_, number, sid, shelf, dbp, pages=24):
    os.makedirs(os.path.dirname(path_), exist_ok=True)
    open(path_, "wb").write(b"PK")
    db.index_books([(path_, 2, 1.0, number, shelf, sid)], dbp)
    b = db.get_book_by_path(path_, dbp)
    with db._connect(dbp) as c:
        c.execute("UPDATE books SET page_count = ? WHERE id = ?", (pages, b["id"]))
    return b["id"]


@pytest.fixture
def lib(tmp_path, db_path, monkeypatch):
    monkeypatch.setattr(od, "DB_PATH", db_path)
    root = tmp_path / "comics" / "Image"
    saga = db.add_series(title="Saga", publisher="Image", folder_path=str(root / "Saga"), on_pull_list=True, path=db_path)
    low = db.add_series(title="Low", publisher="Image", folder_path=str(root / "Low"), on_pull_list=False, path=db_path)
    sh_saga = db.upsert_shelf_series(str(root / "Saga"), "Saga", "Image", saga, 4, "2026-10-08T00:00:00.000000Z", db_path)
    sh_low = db.upsert_shelf_series(str(root / "Low"), "Low", "Image", low, 2, "2026-10-08T00:00:00.000000Z", db_path)
    books = {}
    for n in (1, 2, 3):
        books[("saga", n)] = _book(str(root / "Saga" / f"Saga #00{n}.cbz"), float(n), saga, sh_saga, db_path)
    books[("saga", "tpb")] = _book(str(root / "Saga" / "Saga v01 TPB.cbz"), None, saga, sh_saga, db_path)
    for n in (1, 2):
        books[("low", n)] = _book(str(root / "Low" / f"Low #00{n}.cbz"), float(n), low, sh_low, db_path)
    db.upsert_issue_status_many([(saga, float(n), "2024-01-01", n <= 3, None, None, None) for n in (1, 2, 3)]
                                + [(saga, 4.0, "2099-01-01", False, None, None, None)], path=db_path)
    db.upsert_issue_status_many([(low, 1.0, "2024-01-01", True, None, None, None), (low, 2.0, "2024-02-01", True, None, None, None),
                                 (low, 3.0, "2024-03-01", False, None, None, None)], path=db_path)
    return {"db": db_path, "saga": saga, "low": low, "b": books}


def test_continue_is_recent_partial_progress_newest_first(lib):
    b = lib["b"]
    db.set_progress("me", b[("saga", 1)], 5, False, "2026-10-01T10:00:00Z", lib["db"])
    db.set_progress("me", b[("low", 1)], 9, False, "2026-10-07T10:00:00Z", lib["db"])
    db.set_progress("me", b[("saga", 2)], 3, False, "2026-01-01T10:00:00Z", lib["db"])        # untouched for months
    rows = od.continue_reading(path=lib["db"])
    assert [(r["series"], r["label"], r["progress"]["page"]) for r in rows] == [("Low", "#1", 9), ("Saga", "#1", 5)]


def test_next_is_the_first_unread_after_the_last_finished_and_skips_trades(lib):
    b = lib["b"]
    db.set_progress("me", b[("saga", 1)], 24, True, "2026-10-01T10:00:00Z", lib["db"])
    db.set_progress("me", b[("saga", 2)], 24, True, "2026-10-02T10:00:00Z", lib["db"])
    rows = od.up_next(path=lib["db"])
    assert [(r["series"], r["label"], r["finished"]) for r in rows] == [("Saga", "#3", 2)]
    # something in progress in that series → it belongs to Continue, not Next
    db.set_progress("me", b[("saga", 3)], 2, False, "2026-10-03T10:00:00Z", lib["db"])
    assert od.up_next(path=lib["db"]) == []


def test_coming_soon_only_when_caught_up_and_labels_from_real_data(lib):
    b = lib["b"]
    for n in (1, 2, 3):
        db.set_progress("me", b[("saga", n)], 24, True, f"2026-10-0{n}T10:00:00Z", lib["db"])
    for n in (1, 2):
        db.set_progress("me", b[("low", n)], 24, True, f"2026-09-0{n}T10:00:00Z", lib["db"])
    rows = {r["series"]: r for r in od.coming_soon(path=lib["db"])}
    assert rows["Saga"]["label"] == "#4" and rows["Saga"]["status"] == "upcoming" and rows["Saga"]["status_label"].startswith("Out 2099")
    assert rows["Low"]["status"] == "not_owned" and "pull list off" in rows["Low"]["status_label"]
    assert list(rows) == ["Saga", "Low"]                                  # most recently read first


def test_recently_added_is_by_file_time_one_card_per_series_unread_only(lib):
    b = lib["b"]
    with db._connect(lib["db"]) as c:
        c.execute("UPDATE books SET mtime = 1.0")
        c.execute("UPDATE books SET mtime = 300.0 WHERE id IN (?, ?)", (b[("saga", 2)], b[("saga", 3)]))
        c.execute("UPDATE books SET mtime = 200.0 WHERE id = ?", (b[("low", 2)],))
    db.set_progress("me", b[("saga", 3)], 24, True, "2026-10-08T11:00:00Z", lib["db"])   # read already → not 'new' to you
    rows = od.recent_added(path=lib["db"])
    assert [(r["series"], r["label"]) for r in rows[:2]] == [("Saga", "#2"), ("Low", "#2")]
    assert len([r for r in rows if r["series"] == "Saga"]) == 1


def test_recently_released_is_by_store_date_of_owned_issues(lib):
    b = lib["b"]
    with db._connect(lib["db"]) as c:
        c.execute("UPDATE issue_status SET store_date = '2026-09-30' WHERE tracked_series_id = ? AND number = 3", (lib["saga"],))
        c.execute("UPDATE issue_status SET store_date = '2026-10-07' WHERE tracked_series_id = ? AND number = 2", (lib["low"],))
    rows = od.recent_released(path=lib["db"])
    assert [(r["series"], r["label"], r["store_date"]) for r in rows[:2]] == [("Low", "#2", "2026-10-07"), ("Saga", "#3", "2026-09-30")]
    db.set_progress("me", b[("low", 2)], 24, True, "2026-10-08T11:00:00Z", lib["db"])
    assert [r["series"] for r in od.recent_released(path=lib["db"])][:2] == ["Saga", "Low"]   # Low #2 read → Low #1 is its newest unread


def test_not_now_hides_keeps_the_place_and_clears_on_the_next_read(lib):
    b = lib["b"]
    db.set_progress("me", b[("saga", 1)], 5, False, "2026-10-08T10:00:00Z", lib["db"])
    assert [r["label"] for r in od.continue_reading(path=lib["db"])] == ["#1"]
    assert db.set_dismissed("me", b[("saga", 1)], True, lib["db"])
    assert od.continue_reading(path=lib["db"]) == []
    assert db.get_progress("me", b[("saga", 1)], lib["db"])["page"] == 5              # place kept
    db.set_progress("me", b[("saga", 1)], 6, False, "2026-10-08T11:00:00Z", lib["db"])   # read on → back
    assert [r["progress"]["page"] for r in od.continue_reading(path=lib["db"])] == [6]
    assert db.set_dismissed("me", 99999, True, lib["db"]) is False


# --- discovery rows -------------------------------------------------------------
NOW = __import__("datetime").datetime(2026, 10, 10, tzinfo=__import__("datetime").timezone.utc)


def test_rediscover_is_series_left_for_half_a_year_with_unread_books(lib, monkeypatch):
    monkeypatch.setattr(od, "DISCOVERY_MIN", 1)
    b = lib["b"]
    db.set_progress("me", b[("saga", 1)], 24, True, "2026-01-02T10:00:00Z", lib["db"])   # finished #1, then nothing
    db.set_progress("me", b[("low", 1)], 7, False, "2026-09-30T10:00:00Z", lib["db"])    # Low is current
    rows = od.rediscover(now=NOW, path=lib["db"])
    assert [(r["series"], r["label"]) for r in rows] == [("Saga", "#2")]
    # stalled mid-book: the stalled book is the card, with its page
    db.set_progress("me", b[("saga", 2)], 11, False, "2026-01-03T10:00:00Z", lib["db"])
    rows = od.rediscover(now=NOW, path=lib["db"])
    assert [(r["label"], r["progress"]["page"]) for r in rows] == [("#2", 11)]
    # a series already claimed by an earlier row stays out
    assert od.rediscover(now=NOW, path=lib["db"], seen={("t", lib["saga"])}) == []


def test_rediscover_needs_four_cards_to_be_a_row(lib):
    db.set_progress("me", lib["b"][("saga", 1)], 24, True, "2026-01-02T10:00:00Z", lib["db"])
    assert od.rediscover(now=NOW, path=lib["db"]) == []


def test_quick_reads_are_complete_short_runs_never_opened(lib, monkeypatch, db_path, tmp_path):
    monkeypatch.setattr(od, "DISCOVERY_MIN", 1)
    root = tmp_path / "comics" / "Image"
    mini = db.add_series(title="Mini", publisher="Image", folder_path=str(root / "Mini"), on_pull_list=False, path=db_path)
    sh = db.upsert_shelf_series(str(root / "Mini"), "Mini", "Image", mini, 3, "2026-10-08T00:00:00.000000Z", db_path)
    ids = [_book(str(root / "Mini" / f"Mini #00{n}.cbz"), float(n), mini, sh, db_path) for n in (1, 2, 3)]
    db.upsert_issue_status_many([(mini, float(n), "2025-01-01", True, None, None, None) for n in (1, 2, 3)], path=db_path)
    rows = od.quick_reads(path=db_path)
    # Saga has #4 still to come and Low is missing #3: neither is complete
    assert [(r["series"], r["label"], r["issues"]) for r in rows] == [("Mini", "#1", 3)]
    db.set_progress("me", ids[0], 2, False, "2026-10-01T10:00:00Z", db_path)           # opened = not a quick read
    assert od.quick_reads(path=db_path) == []


def test_next_on_list_starts_at_the_continue_point_and_keeps_gaps(lib, monkeypatch):
    import kometa.related as related
    b = lib["b"]
    db.set_progress("me", b[("saga", 1)], 24, True, "2026-10-05T10:00:00Z", lib["db"])
    prog = lambda done: {"page": 24, "completed": done, "updated_at": "2026-10-05T10:00:00Z"}
    lst = {"id": 7, "name": "Image picks", "total": 4, "continue": b[("saga", 2)], "entries": [
        {"position": 1, "series": "Saga", "number": "1", "books": [{"id": b[("saga", 1)], "progress": prog(True)}]},
        {"position": 2, "series": "Saga", "number": "2", "books": [{"id": b[("saga", 2)], "progress": None}]},
        {"position": 3, "series": "Nowhere", "number": "1", "books": [], "cover": None},
        {"position": 4, "series": "Low", "number": "1", "books": [{"id": b[("low", 1)], "progress": None}]},
    ]}
    monkeypatch.setattr(related, "_resolved_lists", lambda path: [lst])
    monkeypatch.setattr(od, "DISCOVERY_MIN", 1)
    rows = od.list_rows(now=NOW, path=lib["db"])
    assert [r["name"] for r in rows] == ["Image picks"]
    assert [(c["kind"], c["label"], c["position"]) for c in rows[0]["cards"]] == [
        ("book", "Saga #2", 2), ("gap", "Nowhere #1", 3), ("book", "Low #1", 4)]
    # a list nobody has read from in two months isn't 'being read'
    assert od.list_rows(now=NOW.replace(month=12, day=30), path=lib["db"]) == []
