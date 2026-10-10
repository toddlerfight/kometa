"""After a file lands: the cover from the file and the shelf row for the book,
now, locally. Komga is never asked (read-only since 2026-10-10)."""
import kometa.db as db
import kometa.acquisition as acq
import kometa.shelf as shelf
from tests.conftest import make_cbz


def _series(db_path, folder):
    return db.add_series(komga_series_id="K1", title="Saga", publisher="Image", folder_path=str(folder), path=db_path)


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
    monkeypatch.setattr(acq, "_komga", lambda: (_ for _ in ()).throw(AssertionError("Komga was asked")))

    acq._resync_after_placement(sid, str(f))

    assert covers == [(str(f), sid)]
    with db._connect(db_path) as conn:
        rows = conn.execute("SELECT path, number, tracked_series_id FROM books").fetchall()
    assert [(r["path"], r["number"], r["tracked_series_id"]) for r in rows] == [(str(f), 1.0, sid)]
    assert db.shelf_id_for_series(sid, db_path) is not None


def test_index_series_folder_without_a_folder_is_a_no_op(tmp_path):
    db_path = str(tmp_path / "k.db")
    db.init_db(db_path)
    sid = db.add_series(komga_series_id=None, title="Saga", publisher="Image", folder_path=None, path=db_path)
    assert shelf.index_series_folder(sid, db_path) == 0


def test_scan_library_passes_deep_flag():
    from kometa.komga_client import KomgaClient
    sent = []

    class _S:
        def post(self, url, params=None, timeout=None):
            sent.append(params)

            class R:
                def raise_for_status(self):
                    pass
            return R()
    c = KomgaClient.__new__(KomgaClient)
    c.session, c.base_url, c.library_id = _S(), "http://k", "LIB"
    c.scan_library()
    c.scan_library(deep=True)
    assert sent == [None, {"deep": "true"}]
