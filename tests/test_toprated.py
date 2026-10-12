"""Top rated (kometa/toprated.py): a run's issues by community score, and On
Deck's Community picks. Offline — every score is a hand-made community_json."""
import json

import pytest

import kometa.db as db
import kometa.record as rec
from kometa import toprated as tr


def _run(db_path, title, owned=(1, 2, 3), numbers=(1, 2, 3)):
    sid = db.add_series(title=title, publisher="Image", folder_path=f"/comics/Image/{title}", path=db_path)
    for n in numbers:
        db.upsert_issue_status(sid, n, None, n in owned, path=db_path)
    return sid


def _score(db_path, sid, number, cbr=None, locg=None):
    rec.ensure_tables(db_path)
    c = {}
    if locg:
        c.update(rating=locg[0], ratings=locg[1])
    if cbr:
        c["cbr"] = {"critic": cbr[0], "critic_reviews": cbr[1], "user": cbr[2], "user_reviews": cbr[3]}
    with db._connect(db_path) as conn:
        conn.execute("""INSERT OR REPLACE INTO issue_record (tracked_series_id, number, covers_json, fill_state, community_json)
            VALUES (?, ?, '[]', 'variants', ?)""", (sid, number, json.dumps(c)))


def _books(db_path, sid, numbers, read=()):
    for n in numbers:
        b = db.upsert_book(f"/comics/Image/{sid}/#{n:03d}.cbz", 1, 1.0, [{}], tracked_series_id=sid, number=n, path=db_path)
        if n in read:
            db.set_progress("me", b, 1, True, "2026-10-01T00:00:00Z", path=db_path)


# --- the arithmetic -------------------------------------------------------------------
def test_every_source_lands_on_ten():
    c = {"rating": 4.0, "ratings": 100, "cbr": {"critic": 9.0, "critic_reviews": 8, "user": 7.5, "user_reviews": 2},
         "hardcover": {"rating": 3.5, "count": 10}, "openlibrary": {"rating": None, "count": 0}}
    assert sorted(tr.sources(c)) == [(7.0, 10), (7.5, 2), (8.0, 100), (9.0, 8)]


def test_unrated_parts_are_skipped_not_zeroed():
    assert tr.sources({"rating": 0, "ratings": 0, "cbr": {"critic": None, "critic_reviews": 0, "user": 8.0, "user_reviews": 0}}) == []
    assert tr.sources(None) == []


def test_a_big_crowd_is_capped_so_critics_still_count():
    raw, w, votes = tr.pool([(8.0, 5000), (10.0, 25)])
    assert w == 2 * tr.CAP and votes == 5025
    assert raw == pytest.approx(9.0)


def test_one_perfect_review_barely_moves_off_the_prior():
    assert tr.shrink(10.0, 1, 7.0, tr.ISSUE_M) == pytest.approx(7.0 + 3.0 / 11)
    assert tr.shrink(None, 0, 7.0, tr.ISSUE_M) == 7.0


# --- a run's issues ------------------------------------------------------------------
def test_issues_rank_by_shrunk_score_not_raw(db_path, monkeypatch):
    monkeypatch.setattr(tr, "DB_PATH", db_path)
    sid = _run(db_path, "Saga", numbers=(1, 2, 3, 4))
    _score(db_path, sid, 1, cbr=(8.5, 20, 8.6, 30))            # solid and well-reviewed
    _score(db_path, sid, 2, cbr=(10.0, 3, None, 0))            # perfect, but three people
    _score(db_path, sid, 3, cbr=(6.0, 15, 6.5, 10))
    _score(db_path, sid, 4, cbr=(10.0, 1, None, 0))            # one review: not ranked at all
    d = tr.top_rated(sid)
    assert d["owned"] is True
    assert [i["number"] for i in d["issues"]] == [1, 2, 3]
    assert d["issues"][1]["raw"] == 10.0 and d["issues"][1]["score"] < d["issues"][0]["score"]
    assert d["issues"][0]["owned"] is True


def test_a_run_you_dont_own_gets_nothing(db_path, monkeypatch):
    monkeypatch.setattr(tr, "DB_PATH", db_path)
    sid = _run(db_path, "Monstress", owned=())
    _score(db_path, sid, 1, cbr=(9.0, 20, 9.0, 20))
    assert tr.top_rated(sid) == {"owned": False, "score": None, "issues": []}


def test_owned_but_unrated_is_empty_not_an_error(db_path, monkeypatch):
    monkeypatch.setattr(tr, "DB_PATH", db_path)
    sid = _run(db_path, "Paper Girls")
    assert tr.top_rated(sid) == {"owned": True, "score": None, "issues": []}


def test_locg_is_doubled_into_the_same_ranking(db_path):
    sid = _run(db_path, "East of West")
    _score(db_path, sid, 1, locg=(4.5, 200))                   # 9.0
    _score(db_path, sid, 2, cbr=(8.0, 25, 8.0, 25))
    d = tr.series(sid, db_path)
    assert [i["number"] for i in d["issues"]] == [1, 2]


# --- Community picks -----------------------------------------------------------------
def _library(db_path):
    """Six owned runs with scores; returns their ids, best first by design."""
    ids = []
    for k, s in enumerate((9.5, 9.0, 8.5, 8.0, 7.5, 7.0)):
        sid = _run(db_path, f"Run {k}", owned=(1, 2), numbers=(1, 2))
        for n in (1, 2):
            _score(db_path, sid, n, cbr=(s, 25, s, 25))
        _books(db_path, sid, (1, 2))
        ids.append(sid)
    return ids


def test_picks_are_best_first_and_finished_runs_drop(db_path, monkeypatch):
    monkeypatch.setattr(tr, "DB_PATH", db_path)
    ids = _library(db_path)
    with db._connect(db_path) as conn:                         # finish Run 0, start Run 2
        books = {r["number"]: r["id"] for r in conn.execute("SELECT id, number FROM books WHERE tracked_series_id = ?", (ids[0],))}
        half = conn.execute("SELECT id FROM books WHERE tracked_series_id = ? AND number = 1", (ids[2],)).fetchone()["id"]
    for b in books.values():
        db.set_progress("me", b, 1, True, "2026-10-01T00:00:00Z", path=db_path)
    db.set_progress("me", half, 1, True, "2026-10-01T00:00:00Z", path=db_path)
    p = tr.community_picks()["picks"]
    assert [x["series_id"] for x in p] == ids[1:]
    assert next(x for x in p if x["series_id"] == ids[2])["read"] == 1
    assert all(x["books"] == 2 for x in p)


def test_review_count_outweighs_a_thin_perfect_run(db_path):
    ids = _library(db_path)
    thin = _run(db_path, "Thin", owned=(1,), numbers=(1,))
    _score(db_path, thin, 1, cbr=(10.0, 4, None, 0))
    p = tr.picks(path=db_path)
    order = [x["series_id"] for x in p]
    assert order.index(thin) > order.index(ids[1])             # four 10s don't beat a hundred 9s
    assert next(x for x in p if x["series_id"] == thin)["score"] < 8.5


def test_unowned_runs_and_arcs_never_pick(db_path):
    ids = _library(db_path)
    ghost = _run(db_path, "Ghost", owned=(), numbers=(1,))
    _score(db_path, ghost, 1, cbr=(10.0, 25, 10.0, 25))
    with db._connect(db_path) as conn:
        conn.execute("UPDATE tracked_series SET kind = 'arc' WHERE id = ?", (ids[0],))
    got = {x["series_id"] for x in tr.picks(path=db_path)}
    assert ghost not in got and ids[0] not in got


def test_too_few_picks_is_no_row(db_path):
    sid = _run(db_path, "Lonely")
    _score(db_path, sid, 1, cbr=(9.0, 25, 9.0, 25))
    assert tr.picks(path=db_path) == []
