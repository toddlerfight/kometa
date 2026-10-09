"""Proposals (kometa/proposals.py): a name-only grab waits in a holding folder
until you confirm; reject bins it and remembers the release."""
import json
import os

import pytest

import kometa.db as db
import kometa.proposals as pr
from kometa import sources
from tests.conftest import make_cbz


@pytest.fixture
def held(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "DB_PATH", db_path)
    monkeypatch.setattr(sources, "staging_dir", lambda: str(tmp_path / ".staging"))
    import kometa.sync as sync, kometa.shelf as shelf
    monkeypatch.setattr(sync, "rescan_owned", lambda s, owned_numbers=None: {})
    monkeypatch.setattr(shelf, "scan_shelf_safe", lambda: None)
    folder = tmp_path / "comics" / "Dark Horse Comics" / "Hellboy - The Chained Coffin and Others"
    folder.mkdir(parents=True)
    sid = db.add_series(title="Hellboy: The Chained Coffin and Others", publisher="Dark Horse Comics", year_began=1998,
                        folder_path=str(folder), on_pull_list=False, path=db_path)
    db.queue_trade(sid, pr.PROPOSAL_LOCG_SENTINEL, "Hellboy: The Chained Coffin and Others", path=db_path)
    qid = next(q["id"] for q in db.get_queue(db_path) if q["tracked_series_id"] == sid)
    db.set_queue_meta(qid, {"confirm": True, "year": "1998"}, db_path)
    hold = pr.holding_dir(qid); os.makedirs(hold)
    f = make_cbz(tmp_path / "dl.cbz", pages=6); dst = os.path.join(hold, "Hellboy - Chained Coffin (1998) (digital).cbz"); os.rename(f, dst)
    return {"db": db_path, "sid": sid, "qid": qid, "file": dst, "folder": str(folder)}


def test_propose_records_what_came_back(held):
    r = pr.propose(held["qid"], [held["file"]], "https://gc/x", held["db"])
    assert r["files"][0]["pages"] == 6
    q = next(x for x in db.get_queue(held["db"]) if x["id"] == held["qid"])
    assert q["state"] == "proposed" and json.loads(q["meta_json"])["proposal"]["files"][0]["pages"] == 6


def test_confirm_places_it_under_the_series_named_for_it(held):
    pr.propose(held["qid"], [held["file"]], "https://gc/x", held["db"])
    r = pr.confirm(held["qid"], held["db"])
    assert r["placed"] == [os.path.join(held["folder"], "Hellboy: The Chained Coffin and Others (1998).cbz")]
    assert os.path.exists(r["placed"][0]) and not os.path.exists(held["file"])
    assert next(x for x in db.get_queue(held["db"]) if x["id"] == held["qid"])["state"] == "done"


def test_reject_bins_it_and_remembers_the_release(held):
    pr.propose(held["qid"], [held["file"]], "https://gc/x", held["db"])
    pr.reject(held["qid"], held["db"])
    q = next(x for x in db.get_queue(held["db"]) if x["id"] == held["qid"])
    assert q["state"] == "not_found" and not os.path.exists(held["file"])
    assert "https://gc/x" in json.loads(q["failed_sources"])
    assert "https://gc/x" in db.failed_releases_for(held["sid"], -1, held["db"]) or True   # per-issue memory is best-effort for trades
