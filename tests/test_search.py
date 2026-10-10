"""Universal search (kometa/search.py): one box, sections, an issue jump, a top hit."""
import json

import pytest

import kometa.db as db
import kometa.people as people
import kometa.search as search
import kometa.readlists as rl


@pytest.fixture
def lib(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(search, "DB_PATH", db_path)
    search._cache["series"] = None
    people._cache["value"] = None
    root = tmp_path / "comics" / "DC Comics"
    def run(title, year, nums, owned_files=()):
        sid = db.add_series(title=title, publisher="DC Comics", year_began=year, folder_path=str(root / title),
                            on_pull_list=False, path=db_path)
        for n in nums:
            db.upsert_issue_status(sid, float(n), f"{year}-01-01", 1 if n in owned_files else 0, path=db_path)
        if owned_files:
            folder = root / title
            folder.mkdir(parents=True, exist_ok=True)
            shelf = db.upsert_shelf_series(str(folder), title, "DC Comics", sid, len(owned_files), "2026-10-10T00:00:00Z", db_path)
            files = []
            for n in owned_files:
                f = folder / f"{title} #{n:03d}.cbz"; f.write_bytes(b"PK")
                files.append((str(f), 2, 1.0, float(n), shelf, sid))
            db.index_books(files, db_path)
        return sid
    ids = {
        "old": run("Batman (1940)", 1940, range(1, 20)),                 # has #13, not owned
        "new": run("Batman (2016)", 2016, range(1, 30), owned_files=(13, 14)),
        "y100": run("Batman - Year 100", 2006, range(1, 5), owned_files=(1, 2, 3, 4)),
        "hl": run("Heavy Liquid", 1999, range(1, 6), owned_files=(1,)),
        "spawn": run("Spawn", 1992, range(1, 3)),
    }
    with db._connect(db_path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS series_signals (tracked_series_id INTEGER PRIMARY KEY, creators_json TEXT, arcs_json TEXT, fetched_at TEXT)")
        for k in ("y100", "hl", "spawn"):
            c.execute("INSERT INTO series_signals VALUES (?, ?, '[]', '2026-10-10')",
                      (ids[k], json.dumps([{"role": "writer" if k != "spawn" else "cover", "name": "Paul Pope", "id": 1116}])))
    rl.save_list("Doom: before Avengers Doomsday", [{"series": "Books of Doom", "number": "1", "volume": "2006"}], "cbl", None, db_path)
    return {"db": db_path, **ids}


def test_batman_13_jumps_to_the_run_you_own_it_in(lib):
    out = search.search("batman 13", path=lib["db"])
    assert out["issues"][0]["series_id"] == lib["new"] and out["issues"][0]["book_id"]
    assert out["issues"][0]["label"] == "#13"
    assert out["top"]["kind"] == "issue" and out["top"]["series_id"] == lib["new"]
    assert {s["id"] for s in out["series"]} >= {lib["old"], lib["new"]}       # the runs the words name, too


def test_pope_tops_with_the_person_and_lists_their_series_with_why(lib):
    out = search.search("pope", path=lib["db"])
    assert out["people"][0]["name"] == "Paul Pope" and out["people"][0]["count"] == 3
    assert out["top"]["kind"] == "person"
    whys = {s["title"]: s["why"] for s in out["series"]}
    assert whys["Heavy Liquid"] == "Writer: Paul Pope" and whys["Spawn"] == "Cover: Paul Pope"


def test_an_exact_title_beats_everything_and_titles_lead_the_series(lib):
    out = search.search("heavy liquid", path=lib["db"])
    assert out["top"]["kind"] == "series" and out["top"]["id"] == lib["hl"]
    assert out["series"][0]["why"] is None


def test_a_list_name_finds_the_list(lib):
    out = search.search("doom", path=lib["db"])
    assert [l["name"] for l in out["lists"]] == ["Doom: before Avengers Doomsday"]
    assert out["lists"][0]["total"] == 1


def test_too_short_is_nothing(lib):
    assert search.search("b", path=lib["db"])["top"] is None
