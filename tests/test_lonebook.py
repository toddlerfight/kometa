"""A run that is one book takes that book as its #1 (kometa/lonebook.py).

Bedford Street, 2026-11-06: LOCG's only entry for the run is a 'TP'; Kometa built
no issues, filed the book under Trades, and On Deck labelled it with the filename.
"""
import os

import pytest

import kometa.db as db
import kometa.lonebook as lb
from tests.conftest import make_cbz

TP = {"format": "TPB", "title": "Bedford Street TP", "locg_id": "9106125", "cover": "https://s3/medium-9106125.jpg",
      "vol": None, "vol_range": None, "is_variant": False, "source": "locg"}
FILE = "Bedford Street (2026) (Graphic Novel) (Fantagraphics) (Digital).cbz"


@pytest.fixture
def run(tmp_path):
    db_path = str(tmp_path / "k.db")
    db.init_db(db_path)
    folder = tmp_path / "Fantagraphics" / "Bedford Street"
    folder.mkdir(parents=True)
    sid = db.add_series(title="Bedford Street", publisher="Fantagraphics Books", year_began=2026,
                        folder_path=str(folder), path=db_path)
    db.set_locg_series_id(sid, 217784, db_path)
    return {"db": db_path, "sid": sid, "folder": folder}


def _trades(run, *trades):
    db.set_trades(run["sid"], [dict(t) for t in trades], run["db"])


def _series(run):
    return db.get_series_by_id(run["sid"], run["db"])


def test_one_unnumbered_edition_and_no_issues_is_one_book(run):
    _trades(run, TP)
    assert lb.edition(_series(run), run["db"])["locg_id"] == "9106125"


@pytest.mark.parametrize("trades", [
    [TP, dict(TP, format="HC", title="Bedford Street HC", locg_id="9106126")],   # Bad Seeds: TPB + HC
    [dict(TP, vol=1)],                                                             # a Vol. 1 is a run's first trade
    [],
])
def test_not_one_book(run, trades):
    _trades(run, *trades)
    assert lb.edition(_series(run), run["db"]) is None


def test_a_variant_printing_doesnt_make_it_two_books(run):
    _trades(run, TP, dict(TP, locg_id="9106127", is_variant=True))
    assert lb.edition(_series(run), run["db"]) is not None


def test_a_run_with_catalogue_issues_is_not_one_book(run):
    _trades(run, TP)
    db.upsert_issue_rows([(run["sid"], 1.0, "2026-01-07", False, None, "555")], path=run["db"])
    assert lb.edition(_series(run), run["db"]) is None


def test_adopt_renames_to_issue_one_and_keeps_the_book(run):
    _trades(run, TP)
    src = run["folder"] / FILE
    make_cbz(src)
    db.index_books([(str(src), 10, 1.0, None, None, run["sid"])], run["db"])
    book_id = db.get_book_by_path(str(src), run["db"])["id"]

    new = lb.adopt(run["sid"], [str(src)], run["db"], check=lambda s: True)

    assert os.path.basename(new) == "Bedford Street #001 (2026).cbz"
    assert not src.exists() and os.path.exists(new)
    b = db.get_book_by_path(new, run["db"])
    assert (b["id"], b["number"]) == (book_id, 1.0)          # same book: read progress rides along
    i = db.get_issues_for_series(run["sid"], run["db"])
    assert [(x["number"], bool(x["owned"]), x["locg_issue_id"]) for x in i] == [(1.0, True, "9106125")]
    assert lb.adopted(_series(run), run["db"])                # Trades hides it now
    assert lb.edition(_series(run), run["db"])                # and it's still one book after #1 exists


def test_no_rename_unless_the_catalogue_answers_none(run):
    _trades(run, TP)
    src = run["folder"] / FILE
    make_cbz(src)
    assert lb.adopt(run["sid"], [str(src)], run["db"], check=lambda s: False) is None
    assert src.exists()
    assert db.get_issues_for_series(run["sid"], run["db"]) == []


def test_two_files_are_not_one_book(run):
    _trades(run, TP)
    a, b = run["folder"] / "part 1.cbz", run["folder"] / "part 2.cbz"
    make_cbz(a)
    make_cbz(b)
    assert lb.adopt(run["sid"], [str(a), str(b)], run["db"], check=lambda s: True) is None


def test_catalogue_says_none_only_on_a_real_empty_answer(run, monkeypatch):
    import kometa.locg_client as locg
    s = _series(run)
    monkeypatch.setattr(locg, "get_issues_strict", lambda sid: [])
    assert lb.catalogue_says_none(s) is True
    monkeypatch.setattr(locg, "get_issues_strict", lambda sid: [{"number": 1.0}])
    assert lb.catalogue_says_none(s) is False

    def walled(sid):
        raise RuntimeError("HTTP 500 — FlareSolverr timed out")
    monkeypatch.setattr(locg, "get_issues_strict", walled)
    assert lb.catalogue_says_none(s) is False
    assert lb.catalogue_says_none(dict(s, locg_series_id=None)) is False      # nothing linked to ask


def test_the_strict_locg_fetch_raises_where_the_old_one_said_none():
    import kometa.locg_client as locg

    class _R:
        status_code = 500

        def raise_for_status(self):
            raise RuntimeError("HTTP 500")

        def json(self):
            raise ValueError("not json")
    assert locg._get_issues_with_get(217784, lambda *a, **k: _R()) == []
    with pytest.raises(RuntimeError):
        locg._get_issues_with_get(217784, lambda *a, **k: _R(), strict=True)


def test_a_lone_book_trade_lands_as_issue_one(run, monkeypatch):
    """The whole path: the trade lands, the run takes it as #1, the shelf has it numbered."""
    import kometa.acquisition as acq
    from kometa import covers as cv
    _trades(run, TP)
    src = run["folder"] / FILE
    make_cbz(src)
    monkeypatch.setattr(acq, "DB_PATH", run["db"])
    monkeypatch.setattr(lb, "DB_PATH", run["db"])
    monkeypatch.setattr(lb, "catalogue_says_none", lambda s: True)
    monkeypatch.setattr(cv, "generate_for_path", lambda *a, **k: None)

    acq._trade_landed(run["sid"], [str(src)])

    with db._connect(run["db"]) as conn:
        books = [(os.path.basename(r["path"]), r["number"]) for r in conn.execute("SELECT path, number FROM books")]
    assert books == [("Bedford Street #001 (2026).cbz", 1.0)]


def test_trades_tab_hides_the_adopted_edition(run, monkeypatch):
    import kometa.main as main
    _trades(run, dict(TP, owned=True, file="Bedford Street #001 (2026).cbz"))
    monkeypatch.setattr(main, "DB_PATH", run["db"])
    monkeypatch.setattr(lb, "DB_PATH", run["db"])
    assert main._cached_trades(_series(run))                 # before: listed
    db.upsert_issue_rows([(run["sid"], 1.0, None, True, None, "9106125")], path=run["db"])
    assert main._cached_trades(_series(run)) == []           # after: it's #1
