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


def test_generic_names_are_never_twins_and_publisher_mismatch_is_flagged(lib):
    root = lib["root"]
    # two unrelated 'Volume 01 (2022)' folders under root-level series dirs
    _file(os.path.join(root, "Dogs of London", "Volume 01 (2022)", "Dogs of London #001.cbz"))
    _file(os.path.join(root, "Bylines in Blood", "Volume 01 (2022)", "Bylines #001.cbz"))
    v = db.add_series(title="Volume 01 (2022)", publisher="Bylines in Blood",
                      folder_path=os.path.join(root, "Bylines in Blood", "Volume 01 (2022)"), on_pull_list=False, path=lib["db"])
    db.upsert_shelf_series(os.path.join(root, "Dogs of London", "Volume 01 (2022)"), "Volume 01 (2022)", "Dogs of London", None, 1,
                           "2026-10-08T00:00:00.000000Z", lib["db"])
    rows = tw.find_twins(lib["db"])
    assert all(r["title"] != "Volume 01 (2022)" for r in rows)
    eh = next(r for r in rows if r["title"] == "Event Horizon- Dark Descent")
    assert eh["publisher_differs"] is True                       # Mirage vs IDW: flagged, still listed


def test_merge_takes_the_clean_name_when_the_kept_folder_has_the_arc_dash(tmp_path, db_path, monkeypatch):
    monkeypatch.setattr(tw, "DB_PATH", db_path)
    import kometa.sync as sync
    monkeypatch.setattr(sync, "rescan_owned", lambda s, owned_numbers=None: {})
    root = tmp_path / "comics"
    keep = root / "DC Comics" / "- Justice League"
    twin = root / "DC Comics" / "Justice League"
    _file(str(keep / "Justice League #001.cbz"))
    _file(str(twin / "Justice League #002.cbz"))
    sid = db.add_series(title="Justice League", publisher="DC Comics", folder_path=str(keep), on_pull_list=False, path=db_path)
    db.set_match_status(sid, "auto", db_path)
    ks = db.upsert_shelf_series(str(keep), "- Justice League", "DC Comics", sid, 1, "2026-10-08T00:00:00.000000Z", db_path)
    ts = db.upsert_shelf_series(str(twin), "Justice League", "DC Comics", None, 1, "2026-10-08T00:00:00.000000Z", db_path)
    db.index_books([(str(keep / "Justice League #001.cbz"), 4, 1.0, 1.0, ks, sid), (str(twin / "Justice League #002.cbz"), 4, 1.0, 2.0, ts, None)], db_path)
    r = tw.apply(ts, sid, db_path, root=str(root))
    assert r["moved"] == 1 and r.get("renamed_to") == str(twin)
    assert sorted(os.listdir(twin)) == ["Justice League #001.cbz", "Justice League #002.cbz"] and not os.path.exists(keep)
    assert db.get_series_by_id(sid, db_path)["folder_path"] == str(twin)
    with db._connect(db_path) as c:
        assert all(p[0].startswith(str(twin) + "/") for p in c.execute("SELECT path FROM books"))


def test_two_tracked_series_on_one_catalogue_id_are_twins(lib):
    """'I Feel Sick' and 'I Feel Sick - A Book about a Girl', both LOCG 111869:
    neither folder is untracked, so the title walk can't see them. The one with
    fewer files is the twin, and its series row goes with its folder."""
    root = lib["root"]
    a = os.path.join(root, "SLG Publishing", "I Feel Sick"); b = os.path.join(root, "SLG Publishing", "I Feel Sick - A Book about a Girl")
    for folder, files in ((a, ["I Feel Sick #001.cbz", "I Feel Sick #002.cbz"]), (b, ["I Feel Sick #001.cbz"])):
        for f in files:
            _file(os.path.join(folder, f), b"PK" + b"x" * (60 if folder == a else 10))
    sa = db.add_series(title="I Feel Sick", publisher="SLG Publishing", folder_path=a, on_pull_list=False, locg_series_id=111869, path=lib["db"])
    sb = db.add_series(title="I Feel Sick - A Book about a Girl", publisher="SLG Publishing", folder_path=b, on_pull_list=False, locg_series_id=111869, path=lib["db"])
    sha = db.upsert_shelf_series(a, "I Feel Sick", "SLG Publishing", sa, 2, "2026-10-09T00:00:00.000000Z", lib["db"])
    shb = db.upsert_shelf_series(b, "I Feel Sick - A Book about a Girl", "SLG Publishing", sb, 1, "2026-10-09T00:00:00.000000Z", lib["db"])
    db.index_books([(os.path.join(a, "I Feel Sick #001.cbz"), 64, 1.0, 1.0, sha, sa), (os.path.join(a, "I Feel Sick #002.cbz"), 64, 1.0, 2.0, sha, sa),
                    (os.path.join(b, "I Feel Sick #001.cbz"), 14, 1.0, 1.0, shb, sb)], lib["db"])
    rows = [r for r in tw.find_twins(lib["db"]) if r.get("same_run")]
    assert len(rows) == 1 and rows[0]["series_id"] == sa and rows[0]["twin_series_id"] == sb and rows[0]["shelf_id"] == shb
    r = tw.apply(shb, sa, lib["db"], root=root)
    assert r["errors"] == [] and r["binned"] == 1
    assert db.get_series_by_id(sb, lib["db"]) is None and not os.path.exists(b)
    assert sorted(os.listdir(a)) == ["I Feel Sick #001.cbz", "I Feel Sick #002.cbz"]
