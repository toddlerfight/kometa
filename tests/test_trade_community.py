"""Community ratings on the book/trade sheet: a collected edition's Hardcover and
Open Library ratings, read by its ISBN. Offline — every row is hand-made."""
import kometa.db as db
import kometa.record as rec
from kometa import hardcover, main, openlibrary, trade_community as tc

ISBN = "9781607066019"


def _rows(db_path, hc=(4.23, 516), ol=(4.18, 45)):
    rec.ensure_tables(db_path)
    hardcover.ensure_tables(db_path)
    openlibrary.ensure_tables(db_path)
    sid = db.add_series(title="Saga", publisher="Image", folder_path="/comics/Image/Saga", path=db_path)
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO trade_record (tracked_series_id, key, title, isbn) VALUES (?, 'm900', 'Saga Vol. 1', ?)",
                     (sid, "978-1-60706-601-9"))
        if hc:
            conn.execute("""INSERT INTO hardcover_record (isbn, state, slug, rating, rating_count, at)
                VALUES (?, 'found', 'saga-volume-one', ?, ?, '2026-10-12 10:00:00')""", (ISBN, *hc))
        if ol:
            conn.execute("""INSERT INTO openlibrary_record (isbn, state, rating, rating_count, at)
                VALUES (?, 'found', ?, ?, '2026-10-12 10:00:00')""", (ISBN, *ol))
    return sid


def test_a_trade_gets_both_book_sites_by_its_isbn(db_path):
    _rows(db_path)
    c = tc.for_trade("m900", db_path)
    assert c["hardcover"]["rating"] == 4.23 and c["hardcover"]["count"] == 516
    assert c["hardcover"]["url"] == "https://hardcover.app/books/saga-volume-one"
    assert c["openlibrary"]["rating"] == 4.18 and c["openlibrary"]["count"] == 45


def test_unrated_or_unknown_shows_nothing(db_path):
    _rows(db_path, hc=(None, 0), ol=None)
    assert tc.for_trade("m900", db_path) is None
    assert tc.for_trade("m404", db_path) is None
    assert tc.for_isbn(None, db_path) is None


def test_only_the_site_that_rated_it(db_path):
    _rows(db_path, ol=(None, 0))
    assert set(tc.for_trade("m900", db_path)) == {"hardcover"}


def test_a_shelf_book_finds_its_trade_through_the_runs_list(db_path):
    sid = _rows(db_path)
    db.set_trades(sid, [{"locg_id": "m900", "file": "Saga Vol. 1 (2012).cbz", "title": "Saga Vol. 1"}], db_path)
    book = db.upsert_book("/comics/Image/Saga/Saga Vol. 1 (2012).cbz", 1, 1.0, [{}], tracked_series_id=sid, path=db_path)
    issue = db.upsert_book("/comics/Image/Saga/Saga #001.cbz", 1, 1.0, [{}], tracked_series_id=sid, number=1, path=db_path)
    assert tc.for_book(book, db_path)["hardcover"]["count"] == 516
    assert tc.for_book(issue, db_path) is None


def test_routes_carry_it(db_path, monkeypatch):
    sid = _rows(db_path)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(tc, "DB_PATH", db_path)
    monkeypatch.setattr(rec, "DB_PATH", db_path)
    d = main.get_trade_details("m900")
    assert d["source"] == "metron" and d["community"]["hardcover"]["rating"] == 4.23
    db.set_trades(sid, [{"locg_id": "m900", "file": "Saga Vol. 1 (2012).cbz"}], db_path)
    book = db.upsert_book("/comics/Image/Saga/Saga Vol. 1 (2012).cbz", 1, 1.0, [{}], tracked_series_id=sid, path=db_path)
    assert main.get_book_community(book)["community"]["openlibrary"]["count"] == 45
