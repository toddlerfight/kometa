"""The Trades tab never builds trades inside the request (main.get_series_trades)."""
import threading

import kometa.db as db
import kometa.main as main
import kometa.record as record


def test_missing_trades_answer_pending_and_build_in_the_background(db_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", db_path)
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    db.set_locg_series_id(sid, 123, db_path)
    built = threading.Event()
    def fill(s, force=False):
        db.set_trades(s["id"], [{"locg_id": "9", "title": "Saga Vol. 1", "format": "TPB"}], db_path)
        built.set()
    monkeypatch.setattr(record, "fill_trades", fill)
    monkeypatch.setattr(main, "_cached_trades", lambda s: (db.get_trades(s["id"], db_path) or {}).get("trades"))
    assert main.get_series_trades(sid) == {"trades": [], "pending": True}
    assert built.wait(5)
    assert main.get_series_trades(sid)["trades"][0]["title"] == "Saga Vol. 1"
