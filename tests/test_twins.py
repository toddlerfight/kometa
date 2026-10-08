"""Twin folders (kometa/twins.py): found by normalised title, merged into the
series' folder, duplicates keep the better copy, progress follows, twin goes."""
import os

import pytest

import kometa.db as db
import kometa.twins as tw


def _file(path, data=b"PK\x03\x04"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(data)


@pytest.fixture
def lib(tmp_path, db_path, monkeypatch):
    monkeypatch.setattr(tw, "DB_PATH", db_path)
    import kometa.sync as sync
    monkeypatch.setattr(sync, "rescan_owned", lambda s, owned_numbers=None: {})
    root = tmp_path / "comics"
    keep = root / "IDW Publishing" / "Event Horizon - Dark Descent"
    twin = root / "Mirage" / "Event Horizon- Dark Descent"
    _file(str(keep / "Event Horizon - Dark Descent #001.cbz"))
    _file(str(keep / "Event Horizon - Dark Descent #002.cbr"), b"Rar!" + b"x" * 10)
    _file(str(twin / "Event Horizon - Dark Descent #002 (2024).cbz"), b"PK" + b"y" * 50)   # better copy of #2
    _file(str(twin / "Event Horizon - Dark Descent #003.cbz"))                             # new
    _file(str(twin / "Event Horizon - Dark Descent #001.cbr"), b"Rar!")                    # worse copy of #1
    _file(str(twin / "Event Horizon TPB.cbz"))                                             # no number
    sid = db.add_series(title="Event Horizon - Dark Descent", publisher="IDW Publishing", folder_path=str(keep),
                        on_pull_list=False, path=db_path)
    db.set_match_status(sid, "auto", db_path)
    ks = db.upsert_shelf_series(str(keep), "Event Horizon - Dark Descent", "IDW Publishing", sid, 2, "2026-10-08T00:00:00.000000Z", db_path)
    ts = db.upsert_shelf_series(str(twin), "Event Horizon- Dark Descent", "Mirage", None, 4, "2026-10-08T00:00:00.000000Z", db_path)
    db.index_books([(str(keep / "Event Horizon - Dark Descent #001.cbz"), 4, 1.0, 1.0, ks, sid),
                    (str(keep / "Event Horizon - Dark Descent #002.cbr"), 14, 1.0, 2.0, ks, sid),
                    (str(twin / "Event Horizon - Dark Descent #002 (2024).cbz"), 52, 1.0, 2.0, ts, None),
                    (str(twin / "Event Horizon - Dark Descent #003.cbz"), 4, 1.0, 3.0, ts, None),
                    (str(twin / "Event Horizon - Dark Descent #001.cbr"), 4, 1.0, 1.0, ts, None),
                    (str(twin / "Event Horizon TPB.cbz"), 4, 1.0, None, ts, None)], db_path)
    b3 = db.get_book_by_path(str(twin / "Event Horizon - Dark Descent #003.cbz"), db_path)["id"]
    db.set_progress("me", b3, 7, False, "2026-10-01T00:00:00Z", db_path)                  # halfway through #3 in the twin
    return {"db": db_path, "root": str(root), "sid": sid, "ks": ks, "ts": ts, "keep": str(keep), "twin": str(twin), "b3": b3}


def test_twins_are_found_by_normalised_title(lib):
    rows = tw.find_twins(lib["db"])
    assert [(r["title"], r["series_title"]) for r in rows] == [("Event Horizon- Dark Descent", "Event Horizon - Dark Descent")]


def test_plan_sorts_moves_duplicates_and_as_is(lib):
    p = tw.plan(lib["ts"], lib["sid"], lib["db"])
    assert [m["file"] for m in p["moves"]] == ["Event Horizon - Dark Descent #003.cbz"]
    assert [a["file"] for a in p["as_is"]] == ["Event Horizon TPB.cbz"]
    d = {x["number"]: x["keep"] for x in p["duplicates"]}
    assert d == {1.0: "series", 2.0: "twin"}                     # cbz beats cbr both ways
    assert p["counts"] == {"move": 2, "duplicate": 2, "twin_wins": 1}


def test_apply_moves_bins_and_the_database_follows(lib):
    r = tw.apply(lib["ts"], lib["sid"], lib["db"], root=lib["root"])
    assert r["moved"] == 3 and r["binned"] == 2 and r["errors"] == []
    keep = lib["keep"]
    assert sorted(os.listdir(keep)) == ["Event Horizon - Dark Descent #001.cbz", "Event Horizon - Dark Descent #002 (2024).cbz",
                                        "Event Horizon - Dark Descent #003.cbz", "Event Horizon TPB.cbz"]
    assert not os.path.exists(lib["twin"]) and not os.path.exists(os.path.dirname(lib["twin"]))   # Mirage/ emptied → gone
    binned = sorted(os.listdir(os.path.join(lib["root"], "_trash", "IDW Publishing", "Event Horizon - Dark Descent")) +
                    os.listdir(os.path.join(lib["root"], "_trash", "Mirage", "Event Horizon- Dark Descent")))
    assert "Event Horizon - Dark Descent #002.cbr" in binned and "Event Horizon - Dark Descent #001.cbr" in binned
    with db._connect(lib["db"]) as c:
        rows = {r[0]: (r[1], r[2], r[3]) for r in c.execute("SELECT path, tracked_series_id, shelf_series_id, number FROM books")}
    assert all(p.startswith(keep + "/") for p in rows) and len(rows) == 4
    assert rows[os.path.join(keep, "Event Horizon - Dark Descent #003.cbz")] == (lib["sid"], lib["ks"], 3.0)
    assert rows[os.path.join(keep, "Event Horizon TPB.cbz")][2] is None
    assert db.get_progress("me", lib["b3"], lib["db"])["page"] == 7                        # progress rode along
    assert db.get_shelf_series(lib["ts"], lib["db"]) is None
    assert tw.find_twins(lib["db"]) == []
