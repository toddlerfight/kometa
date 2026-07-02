"""Komga-optional onboarding: adding a series with no Komga configured must still
land a correct folder_path, derived from publisher+title. This is what lets the
first sync reconcile owned-vs-missing against disk instead of falling into the
'own nothing, download everything' trap.
"""
import kometa.db as db
import kometa.main as main
import kometa.arcs as arcs
from kometa.main import AddSeriesRequest


def _wire(monkeypatch, tmp_path, comics_root):
    dbp = str(tmp_path / "k.db")
    db.init_db(dbp)
    monkeypatch.setattr(main, "DB_PATH", dbp)
    monkeypatch.setattr(main, "_comics_root", lambda: str(comics_root))
    monkeypatch.setattr(main, "_komga", lambda: None)          # no Komga
    monkeypatch.setattr(main, "_sync_one", lambda s: None)      # neutralize bg thread
    monkeypatch.setattr(main, "_process_queue", lambda: None)
    return dbp


def test_existing_series_resolves_to_its_on_disk_folder(tmp_path, monkeypatch):
    root = tmp_path / "comics"
    existing = root / "Image Comics" / "Saga"
    existing.mkdir(parents=True)
    for n in (1, 2, 3):
        (existing / f"Saga #{n:03d}.cbz").write_bytes(b"PK\x03\x04")
    _wire(monkeypatch, tmp_path, root)

    # short publisher form, no folder, no Komga
    added = main.add_series(AddSeriesRequest(title="Saga", publisher_name="Image",
                                             on_pull_list=False))

    assert added["folder_path"] == str(existing)


def test_new_series_gets_canonical_path_under_existing_publisher(tmp_path, monkeypatch):
    root = tmp_path / "comics"
    (root / "Image Comics").mkdir(parents=True)
    _wire(monkeypatch, tmp_path, root)

    added = main.add_series(AddSeriesRequest(title="Nimona", publisher_name="Image",
                                             on_pull_list=False))

    assert added["folder_path"] == str(root / "Image Comics" / "Nimona")


def test_explicit_folder_path_is_respected(tmp_path, monkeypatch):
    root = tmp_path / "comics"
    root.mkdir()
    _wire(monkeypatch, tmp_path, root)

    added = main.add_series(AddSeriesRequest(title="Whatever", publisher_name="Image",
                                             folder_path="/custom/path", on_pull_list=False))

    assert added["folder_path"] == "/custom/path"


def test_locg_add_persists_series_id(tmp_path, monkeypatch):
    """A keyless LOCG add stores its locg_series_id and title verbatim."""
    root = tmp_path / "comics"
    root.mkdir()
    _wire(monkeypatch, tmp_path, root)  # fresh DB, no external sources

    added = main.add_series(AddSeriesRequest(locg_id=100002, title="Saga",
                                             publisher_name="Image", on_pull_list=False))
    assert added["title"] == "Saga"
    assert added["locg_series_id"] == 100002


class TestIndexerManagement:
    """Add/remove individual newznab indexers without round-tripping apikeys."""

    def test_add_remove_preserves_apikeys(self, tmp_path, monkeypatch):
        import json
        from kometa.main import IndexerRequest
        dbp = str(tmp_path / "k.db")
        db.init_db(dbp)
        monkeypatch.setattr(main, "DB_PATH", dbp)
        monkeypatch.setattr(main, "_comics_root", lambda: "/comics")  # get_config resolves it

        main.add_indexer(IndexerRequest(name="Geek", host="api.geek.info", apikey="s3cret", ssl=True))
        main.add_indexer(IndexerRequest(name="Two", host="h2", apikey="k2", ssl=False))

        stored = json.loads(db.get_config(dbp)["newznab_indexers"])
        assert [i["name"] for i in stored] == ["Geek", "Two"]
        assert stored[0]["apikey"] == "s3cret"          # secret persisted server-side

        # config GET never exposes apikeys to the browser
        safe = main.get_config()["newznab_indexers"]
        assert all("apikey" not in i for i in safe)

        main.remove_indexer(0)
        after = json.loads(db.get_config(dbp)["newznab_indexers"])
        assert [i["name"] for i in after] == ["Two"]
        assert after[0]["apikey"] == "k2"               # survivor keeps its key

    def test_remove_out_of_range_404s(self, tmp_path, monkeypatch):
        import pytest
        from fastapi import HTTPException
        dbp = str(tmp_path / "k.db")
        db.init_db(dbp)
        monkeypatch.setattr(main, "DB_PATH", dbp)
        with pytest.raises(HTTPException):
            main.remove_indexer(5)


class TestBrowseScope:
    """fs scope browses outside the comics root (to pick the root itself);
    library scope stays sandboxed."""

    def test_fs_scope_reaches_outside_comics_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "_comics_root", lambda: "/nonexistent-root")
        (tmp_path / "sub").mkdir()
        res = main.browse_fs(path=str(tmp_path), scope="fs")
        assert "sub" in res["dirs"]

    def test_library_scope_blocks_outside_root(self, tmp_path, monkeypatch):
        import pytest
        from fastapi import HTTPException
        root = tmp_path / "root"
        root.mkdir()
        monkeypatch.setattr(main, "_comics_root", lambda: str(root))
        with pytest.raises(HTTPException):
            main.browse_fs(path="/etc", scope="library")

    def test_fs_scope_lands_at_home_not_bare_root(self, tmp_path, monkeypatch):
        import os
        monkeypatch.setattr(main, "_comics_root", lambda: "/nonexistent-root")
        res = main.browse_fs(scope="fs")  # empty path -> friendly default
        assert res["path"] == os.path.realpath(os.path.expanduser("~"))

    def test_fs_scope_lands_at_comics_root_when_it_exists(self, tmp_path, monkeypatch):
        import os
        root = tmp_path / "lib"
        root.mkdir()
        monkeypatch.setattr(main, "_comics_root", lambda: str(root))
        res = main.browse_fs(scope="fs")
        assert res["path"] == os.path.realpath(str(root))


class TestMkdir:
    """Create a folder while browsing (the 'New Folder' button)."""

    def test_creates_subfolder(self, tmp_path, monkeypatch):
        import os
        from kometa.main import MkdirRequest
        monkeypatch.setattr(main, "_comics_root", lambda: str(tmp_path))
        res = main.fs_mkdir(MkdirRequest(path=str(tmp_path), name="New Series", scope="library"))
        assert res["path"] == str(tmp_path / "New Series")
        assert os.path.isdir(res["path"])

    def test_rejects_separators_and_traversal(self, tmp_path, monkeypatch):
        import pytest
        from fastapi import HTTPException
        from kometa.main import MkdirRequest
        monkeypatch.setattr(main, "_comics_root", lambda: str(tmp_path))
        for bad in ("../evil", "a/b", ".."):
            with pytest.raises(HTTPException):
                main.fs_mkdir(MkdirRequest(path=str(tmp_path), name=bad, scope="library"))

    def test_blocks_outside_scope(self, tmp_path, monkeypatch):
        import pytest
        from fastapi import HTTPException
        from kometa.main import MkdirRequest
        root = tmp_path / "root"
        root.mkdir()
        monkeypatch.setattr(main, "_comics_root", lambda: str(root))
        with pytest.raises(HTTPException):
            main.fs_mkdir(MkdirRequest(path="/etc", name="x", scope="library"))


class TestComicsRootHealth:
    """config.comics_root_ok drives the just-in-time folder prompt."""

    def test_reports_ok_when_writable(self, tmp_path, monkeypatch):
        dbp = str(tmp_path / "k.db")
        db.init_db(dbp)
        monkeypatch.setattr(main, "DB_PATH", dbp)
        good = tmp_path / "lib"
        good.mkdir()
        monkeypatch.setattr(main, "_comics_root", lambda: str(good))
        assert main.get_config()["comics_root_ok"] is True

    def test_reports_not_ok_when_missing(self, tmp_path, monkeypatch):
        dbp = str(tmp_path / "k.db")
        db.init_db(dbp)
        monkeypatch.setattr(main, "DB_PATH", dbp)
        monkeypatch.setattr(main, "_comics_root", lambda: str(tmp_path / "nope"))
        assert main.get_config()["comics_root_ok"] is False


class TestResolveFolderPreview:
    """The wizard previews where a series will land — same logic add_series uses."""

    def test_existing_folder_reports_exists(self, tmp_path, monkeypatch):
        root = tmp_path / "comics"
        (root / "Image Comics" / "Saga").mkdir(parents=True)
        monkeypatch.setattr(main, "_comics_root", lambda: str(root))

        res = main.resolve_folder(publisher="Image", title="Saga")
        assert res["path"] == str(root / "Image Comics" / "Saga")
        assert res["exists"] is True

    def test_new_series_reports_not_exists(self, tmp_path, monkeypatch):
        root = tmp_path / "comics"
        root.mkdir()
        monkeypatch.setattr(main, "_comics_root", lambda: str(root))

        res = main.resolve_folder(publisher="Oni Press", title="Nimona")
        assert res["path"] == str(root / "Oni Press" / "Nimona")
        assert res["exists"] is False


class TestArcParticipantGate:
    """Publisher gate on _track_participating — CV arc issue lists include
    foreign reprints and magazine inserts; those must not mint tracked series."""

    def test_the_actual_offenders_are_gated(self):
        # The two real junk series this gate exists to prevent (2026-07-02).
        assert not arcs._arc_participant_allowed("Panini Verlag", "DC Comics")
        assert not arcs._arc_participant_allowed("Wizard Press", "DC Comics")

    def test_same_publisher_passes_including_punctuation_noise(self):
        assert arcs._arc_participant_allowed("DC Comics", "DC Comics")
        assert arcs._arc_participant_allowed("D.C. Comics", "DC Comics")
        assert arcs._arc_participant_allowed("Marvel", "marvel")

    def test_unknown_publisher_passes(self):
        # A CV hiccup (or no CV key) must not silently thin an arc.
        assert arcs._arc_participant_allowed(None, "DC Comics")
        assert arcs._arc_participant_allowed("", "DC Comics")
        assert arcs._arc_participant_allowed("DC Comics", None)
