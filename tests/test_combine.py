"""Combine (kometa/combine.py): many one-file folders → one numbered series,
subtitles kept, duplicates binned, stubs gone, progress follows."""
import os

import pytest

import kometa.db as db
import kometa.combine as cb


def _cbz(path, data=b"PK\x03\x04"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(data)


@pytest.fixture
def lib(tmp_path, db_path, monkeypatch):
    monkeypatch.setattr(cb, "DB_PATH", db_path)
    import kometa.sync as sync
    monkeypatch.setattr(sync, "rescan_owned", lambda s, owned_numbers=None: {})
    root = tmp_path / "comics"
    albums = [("The Blue Lotus", 1936, ["The Adventures of Tintin - The Blue Lotus #001 (1936).cbz"]),
              ("In the Land of Soviets", 1930, ["The Adventures of Tintin - In the Land of Soviets #001 (1930).cbz"]),
              ("King Ottokar's Sceptre", 1937, ["The Adventures of Tintin - King Ottokar's Sceptre #001 (1937).cbz",
                                                "The Adventures of Tintin - King Ottokar's Sceptre #001.cbr"]),
              ("Flight", None, ["The Adventures of Tintin - Flight #714.cbz"])]
    ids = {}
    for sub, year, files in albums:
        folder = root / "Casterman" / f"The Adventures of Tintin - {sub}"
        for f in files:
            _cbz(str(folder / f), b"PK" + b"x" * (60 if f.endswith(".cbz") else 10))
        sid = db.add_series(title=f"The Adventures of Tintin - {sub}", publisher="Casterman", folder_path=str(folder),
                            on_pull_list=False, path=db_path)
        db.set_match_status(sid, "pending", db_path)
        sh = db.upsert_shelf_series(str(folder), f"The Adventures of Tintin - {sub}", "Casterman", sid, len(files), "2026-10-09T00:00:00.000000Z", db_path)
        db.index_books([(str(folder / f), 4, 1.0, 1.0, sh, sid) for f in files], db_path)
        ids[sub] = sid
    # noise: a real run with many issues, and a lone two-folder pair below the group minimum
    run = root / "Image Comics" / "Saga - Compendium"
    _cbz(str(run / "Saga #001.cbz")); _cbz(str(run / "Saga #002.cbz")); _cbz(str(run / "Saga #003.cbz")); _cbz(str(run / "Saga #004.cbz"))
    db.add_series(title="Saga - Compendium", publisher="Image Comics", folder_path=str(run), on_pull_list=False, path=db_path)
    b = db.get_book_by_path(str(root / "Casterman" / "The Adventures of Tintin - The Blue Lotus" / "The Adventures of Tintin - The Blue Lotus #001 (1936).cbz"), db_path)
    db.set_progress("me", b["id"], 12, False, "2026-10-01T00:00:00Z", db_path)
    return {"db": db_path, "root": str(root), "ids": ids, "lotus_book": b["id"]}


def test_groups_by_prefix_need_three_and_skip_real_runs(lib):
    g = cb.find_groups(lib["db"])
    assert [x["prefix"] for x in g] == ["The Adventures of Tintin"]
    assert g[0]["count"] == 4 and [m["subtitle"] for m in g[0]["members"]] == \
        ["In the Land of Soviets", "The Blue Lotus", "King Ottokar's Sceptre", "Flight"]      # by year, undated last


def test_plan_numbers_in_order_keeps_subtitles_and_picks_the_better_duplicate(lib):
    ids = [lib["ids"][k] for k in ("In the Land of Soviets", "The Blue Lotus", "King Ottokar's Sceptre", "Flight")]
    p = cb.plan(ids, None, "year", lib["db"])
    assert p["title"] == "The Adventures of Tintin"
    assert [i["to"] for i in p["items"]] == [
        "The Adventures of Tintin #01 - In the Land of Soviets (1930).cbz",
        "The Adventures of Tintin #02 - The Blue Lotus (1936).cbz",
        "The Adventures of Tintin #03 - King Ottokar's Sceptre (1937).cbz",
        "The Adventures of Tintin #04 - Flight.cbz"]
    assert [d["file"] for d in p["duplicates"]] == ["The Adventures of Tintin - King Ottokar's Sceptre #001.cbr"]
    p2 = cb.plan(ids, "Tintin", [ids[3], ids[0], ids[1], ids[2]], lib["db"])
    assert p2["items"][0]["to"].startswith("Tintin #01 - Flight")


def test_apply_moves_numbers_drops_stubs_and_keeps_progress(lib):
    ids = [lib["ids"][k] for k in ("In the Land of Soviets", "The Blue Lotus", "King Ottokar's Sceptre", "Flight")]
    r = cb.apply(ids, None, "year", lib["db"], root=lib["root"])
    assert r["moved"] == 4 and r["binned"] == 1 and r["errors"] == []
    target = os.path.join(lib["root"], "Casterman", "The Adventures of Tintin")
    assert sorted(os.listdir(target)) == [
        "The Adventures of Tintin #01 - In the Land of Soviets (1930).cbz", "The Adventures of Tintin #02 - The Blue Lotus (1936).cbz",
        "The Adventures of Tintin #03 - King Ottokar's Sceptre (1937).cbz", "The Adventures of Tintin #04 - Flight.cbz"]
    for sid in ids:
        assert db.get_series_by_id(sid, lib["db"]) is None
    s = db.get_series_by_id(r["series_id"], lib["db"])
    assert s["title"] == "The Adventures of Tintin" and s["match_status"] == "manual" and s["folder_path"] == target
    with db._connect(lib["db"]) as c:
        rows = {r_[0].split("/")[-1]: (r_[1], r_[2]) for r_ in c.execute("SELECT path, tracked_series_id, number FROM books")}
    assert rows["The Adventures of Tintin #02 - The Blue Lotus (1936).cbz"] == (r["series_id"], 2.0)
    assert db.get_progress("me", lib["lotus_book"], lib["db"])["page"] == 12
    assert not os.path.exists(os.path.join(lib["root"], "Casterman", "The Adventures of Tintin - The Blue Lotus"))
    assert cb.find_groups(lib["db"]) == []
