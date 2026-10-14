"""A read book whose folder vanished is kept for its history but flagged missing:
On Deck never offers it and the reader never opens it (2026-10-15: a kept ghost on
On Deck, 'remove' opened the vanished file over NFS and hung ginbako)."""
import kometa.db as db


def _book(path, sid, db_path, n=1.0):
    db.index_books([(path, 10, 1.0, n, sid, None)], db_path)
    with db._connect(db_path) as c:
        return c.execute("SELECT id FROM books WHERE path = ?", (path,)).fetchone()[0]


def test_a_read_book_in_a_vanished_folder_is_kept_but_flagged_then_cleared_when_it_returns(db_path):
    db.init_db(db_path)
    read_id = _book("/comics/X/Gone/a.cbz", 7, db_path)
    unread_id = _book("/comics/X/Gone/b.cbz", 7, db_path, 2.0)
    with db._connect(db_path) as c:
        c.execute("INSERT INTO read_progress (reader_id, book_id, page, completed, updated_at) VALUES ('me', ?, 5, 1, '2026-10-14T00:00:00Z')", (read_id,))
    db.forget_missing_books(7, [], db_path, keep_read=True)
    with db._connect(db_path) as c:
        rows = {r[0]: r[1] for r in c.execute("SELECT id, missing_at FROM books")}
    assert unread_id not in rows                       # unread: forgotten
    assert rows[read_id] is not None                   # read: kept, flagged
    _book("/comics/X/Gone/a.cbz", 7, db_path)          # the folder came back
    with db._connect(db_path) as c:
        assert c.execute("SELECT missing_at FROM books WHERE id = ?", (read_id,)).fetchone()[0] is None


def test_the_reader_never_opens_a_flagged_book(db_path, monkeypatch):
    import pytest
    from fastapi import HTTPException
    from kometa import reader
    db.init_db(db_path)
    monkeypatch.setattr(reader, "DB_PATH", db_path)
    bid = _book("/comics/X/Y/c.cbz", 8, db_path)
    with db._connect(db_path) as c:
        c.execute("UPDATE books SET missing_at = datetime('now') WHERE id = ?", (bid,))
    with pytest.raises(HTTPException) as e:
        reader._book_or_404(bid)
    assert e.value.status_code == 404
