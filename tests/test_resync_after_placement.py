"""After a file lands: the cover from the file and the shelf row for the book,
now, locally."""
import kometa.db as db
import kometa.acquisition as acq
import kometa.shelf as shelf
from tests.conftest import make_cbz


def _series(db_path, folder):
    return db.add_series(title="Saga", publisher="Image", folder_path=str(folder), path=db_path)


def test_placement_makes_the_cover_and_indexes_the_folder(tmp_path, monkeypatch):
    db_path = str(tmp_path / "k.db")
    db.init_db(db_path)
    monkeypatch.setattr(acq, "DB_PATH", db_path)
    folder = tmp_path / "Image" / "Saga"
    folder.mkdir(parents=True)
    f = folder / "Saga #001.cbz"
    make_cbz(f)
    sid = _series(db_path, folder)
    covers = []
    from kometa import covers as cv
    monkeypatch.setattr(cv, "generate_for_path", lambda path, series_id, dbp=None: covers.append((path, series_id)))

    acq._resync_after_placement(sid, str(f))

    assert covers == [(str(f), sid)]
    with db._connect(db_path) as conn:
        rows = conn.execute("SELECT path, number, tracked_series_id FROM books").fetchall()
    assert [(r["path"], r["number"], r["tracked_series_id"]) for r in rows] == [(str(f), 1.0, sid)]
    assert db.shelf_id_for_series(sid, db_path) is not None


def test_index_series_folder_without_a_folder_is_a_no_op(tmp_path):
    db_path = str(tmp_path / "k.db")
    db.init_db(db_path)
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, path=db_path)
    assert shelf.index_series_folder(sid, db_path) == 0


def test_a_trade_from_getcomics_lands_on_the_shelf_at_once(tmp_path, monkeypatch):
    """Bedford Street, 2026-11-06: the trade was placed at 06:38 and Recently added
    never saw it — neither trade path ran the arrival hook, so no `books` row
    until the next full walk of the shelf."""
    db_path = str(tmp_path / "k.db")
    db.init_db(db_path)
    folder = tmp_path / "Fantagraphics" / "Bedford Street"
    folder.mkdir(parents=True)
    sid = db.add_series(title="Bedford Street", publisher="Fantagraphics Books", year_began=2026,
                        folder_path=str(folder), path=db_path)
    db.queue_trade(sid, "9106125", "Bedford Street", edition_title="Bedford Street TP", path=db_path)

    class _GC:
        def search_trade(self, *a, **k):
            return "https://gc/bedford", None

    def _download(url, dest_dir, **k):
        f = folder / "Bedford Street (2026) (Graphic Novel).cbz"
        make_cbz(f)
        return [str(f)]

    from kometa import covers as cv
    monkeypatch.setattr(cv, "generate_for_path", lambda *a, **k: None)
    monkeypatch.setattr(acq, "DB_PATH", db_path)
    monkeypatch.setattr(acq, "GetComicsClient", _GC)
    monkeypatch.setattr(acq.downloader, "download_trade", _download)
    monkeypatch.setattr(acq, "_fallback_usenet_torrent", lambda *a, **k: False)
    import kometa.sync as sync
    monkeypatch.setattr(sync, "refresh_trades_owned", lambda *a, **k: None)
    acq._process_queue()

    with db._connect(db_path) as conn:
        state = conn.execute("SELECT state FROM download_queue").fetchone()[0]
        books = [r["path"] for r in conn.execute("SELECT path FROM books WHERE tracked_series_id = ?", (sid,))]
    assert state == "done"
    assert books == [str(folder / "Bedford Street (2026) (Graphic Novel).cbz")]
