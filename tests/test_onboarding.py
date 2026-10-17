"""Onboarding with no Komga (retired 2026-10-12): adding a series must
land a correct folder_path, derived from publisher+title. This is what lets the
first sync reconcile owned-vs-missing against disk instead of falling into the
'own nothing, download everything' trap.
"""
import kometa.db as db
import kometa.main as main
import kometa.arcs as arcs
from kometa.main import AddSeriesRequest
from tests.conftest import make_cbz


def _wire(monkeypatch, tmp_path, comics_root):
    dbp = str(tmp_path / "k.db")
    db.init_db(dbp)
    monkeypatch.setattr(main, "DB_PATH", dbp)
    monkeypatch.setattr(main, "_comics_root", lambda: str(comics_root))
    monkeypatch.setattr(main, "_sync_one", lambda s: None)      # neutralize bg thread
    monkeypatch.setattr(main, "_process_queue", lambda: None)
    return dbp


def test_existing_series_resolves_to_its_on_disk_folder(tmp_path, monkeypatch):
    root = tmp_path / "comics"
    existing = root / "Image Comics" / "Saga"
    existing.mkdir(parents=True)
    for n in (1, 2, 3):
        make_cbz(existing / f"Saga #{n:03d}.cbz")
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

    # a folder of your choosing, inside the library (owner OK 2026-10-17: one outside it is
    # refused — tests/test_path_fence.py)
    custom = str(root / "Image" / "Somewhere Else")
    added = main.add_series(AddSeriesRequest(title="Whatever", publisher_name="Image",
                                             folder_path=custom, on_pull_list=False))

    assert added["folder_path"] == custom


def test_locg_add_persists_series_id(tmp_path, monkeypatch):
    """A keyless LOCG add stores its locg_series_id and title verbatim."""
    root = tmp_path / "comics"
    root.mkdir()
    _wire(monkeypatch, tmp_path, root)  # fresh DB, no external sources

    added = main.add_series(AddSeriesRequest(locg_id=100002, title="Saga",
                                             publisher_name="Image", on_pull_list=False))
    assert added["title"] == "Saga"
    assert added["locg_series_id"] == 100002


class TestProwlarrMasterGate:
    """Prowlarr is the master search switch for BOTH usenet and torrent — off
    means neither protocol searches, whatever the child toggles say. (The old
    per-newznab-feed indexer list is retired; Prowlarr aggregates them.)"""

    def test_config_exposes_master_flag(self, tmp_path, monkeypatch):
        dbp = str(tmp_path / "k.db")
        db.init_db(dbp)
        monkeypatch.setattr(main, "DB_PATH", dbp)
        monkeypatch.setattr(main, "_comics_root", lambda: "/comics")
        cfg = main.get_config()
        assert cfg["prowlarr_enabled"] is True          # absent = enabled
        assert "newznab_indexers" not in cfg            # retired

    def test_master_off_gates_both_protocols(self, tmp_path, monkeypatch):
        from kometa import acquisition as acq
        dbp = str(tmp_path / "k.db")
        db.init_db(dbp)
        monkeypatch.setattr(acq, "DB_PATH", dbp)
        db.set_config({"prowlarr_enabled": "0"}, dbp)
        assert acq._prowlarr_on() is False
        # torrent rung refuses to even reach for a client when the master is off
        assert acq._try_torrent({"title": "X", "issue_number": 1.0}, qid=1) is False


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


def test_duplicate_locg_add_returns_existing_row(tmp_path, monkeypatch):
    """Adding the same LOCG series twice must return the FIRST row, not mint a
    twin. Live repro: The Foundry added twice 41 min apart → two tracked_series
    rows syncing off locg 210251, two identical calendar cards."""
    root = tmp_path / "comics"
    root.mkdir()
    _wire(monkeypatch, tmp_path, root)

    first = main.add_series(AddSeriesRequest(locg_id=210251, title="The Foundry",
                                             publisher_name="Dark Horse Comics",
                                             on_pull_list=False))
    second = main.add_series(AddSeriesRequest(locg_id=210251, title="The Foundry",
                                              publisher_name="Dark Horse Comics",
                                              on_pull_list=False))

    assert second["id"] == first["id"]
    all_series = db.get_all_series(main.DB_PATH)
    assert len([s for s in all_series if str(s.get("locg_series_id")) == "210251"]) == 1


TRUNC = "Teenage Mutant Ninja Turtles: The Last Ronin – Training Day..."
FULL = "Teenage Mutant Ninja Turtles: The Last Ronin – Training Day"


def test_one_shot_add_gets_full_title_not_locg_clip(tmp_path, monkeypatch):
    """The live Training Day add: an issue-level LOCG hit, clipped before its '#1'.
    The series row knows the real name — the clip must never reach title or folder."""
    import kometa.locg_client as locg
    root = tmp_path / "comics"
    (root / "IDW Publishing").mkdir(parents=True)
    _wire(monkeypatch, tmp_path, root)
    monkeypatch.setattr(main, "_locg_resolve_comic_anon", lambda cid, slug: 206809)
    monkeypatch.setattr(locg, "search_series_anon", lambda q: [
        {"id": 2774701, "title": TRUNC + " #1", "comic": True, "slug": "x"},
        {"id": 206809, "title": FULL},
    ])

    added = main.add_series(AddSeriesRequest(
        title=TRUNC + " #1", publisher_name="IDW Publishing", on_pull_list=False,
        locg_comic_id=2774701, locg_comic_slug="tmnt-the-last-ronin-training-day-1"))

    assert added["title"] == FULL
    assert added["folder_path"] == str(root / "IDW Publishing" / "Teenage Mutant Ninja Turtles- The Last Ronin – Training Day")


def test_clipped_title_loses_dots_when_locg_has_no_series_row(tmp_path, monkeypatch):
    import kometa.locg_client as locg
    root = tmp_path / "comics"
    (root / "IDW Publishing").mkdir(parents=True)
    _wire(monkeypatch, tmp_path, root)
    monkeypatch.setattr(locg, "search_series_anon", lambda q: [])

    added = main.add_series(AddSeriesRequest(locg_id=206809, title=TRUNC,
                                             publisher_name="IDW Publishing", on_pull_list=False))
    assert added["title"] == FULL
    assert not added["folder_path"].endswith(".")


# --- arcs without Komga (retired 2026-10-12): ownership off the runs' folders -----
def _arc_wire(monkeypatch, tmp_path):
    dbp = str(tmp_path / "k.db")
    db.init_db(dbp)
    root = tmp_path / "comics"
    monkeypatch.setattr(arcs, "DB_PATH", dbp)
    monkeypatch.setattr(arcs, "_comics_root", lambda: str(root))
    monkeypatch.setattr(arcs, "_sync_one", lambda s: None)
    monkeypatch.setattr(arcs, "sync_one_guarded", lambda s, fn=None: True)
    return dbp, root


def test_arc_ownership_resolves_from_the_runs_folders(tmp_path, monkeypatch):
    dbp, root = _arc_wire(monkeypatch, tmp_path)
    batman = root / "DC Comics" / "Batman (1940)"
    batman.mkdir(parents=True)
    make_cbz(batman / "Batman (1940) #492.cbz")
    tec = root / "DC Comics" / "Detective Comics"
    tec.mkdir(parents=True)
    make_cbz(tec / "Detective Comics #659.cbz")
    coll = root / "DC Comics" / "Knightfall"
    coll.mkdir(parents=True)
    make_cbz(coll / "Knightfall v01.cbz")
    db.add_series(title="Batman (1940)", publisher="DC Comics", folder_path=str(batman),
                  cv_volume_id="796", path=dbp)
    db.add_series(title="Detective Comics", publisher="DC Comics", folder_path=str(tec), path=dbp)
    db.add_series(title="Knightfall", publisher="DC Comics", folder_path=str(coll), path=dbp)
    arc = db.add_series(title="Knightfall", publisher="DC Comics", kind="arc", path=dbp)
    db.replace_arc_reading_order(arc, [
        {"reading_order": 1, "source_title": "Batman", "number": "492", "story_title": "",
         "cv_issue_id": "1", "cv_volume_id": "796"},                         # by CV volume
        {"reading_order": 2, "source_title": "Detective Comics", "number": "659", "story_title": "",
         "cv_issue_id": "2", "cv_volume_id": None},                          # by title
        {"reading_order": 3, "source_title": "Batman", "number": "493", "story_title": "",
         "cv_issue_id": "3", "cv_volume_id": "796"},                         # not on disk
    ], dbp)

    r = arcs._resolve_arc_ownership(arc)

    assert (r["owned"], r["total"]) == (2, 3)
    assert [x["owned"] for x in db.get_arc_reading_order(arc, dbp)] == [1, 1, 0]
    assert r["collection"]["name"] == "Knightfall"                          # the trade on disk


def test_arc_participant_reuses_the_run_already_in_its_folder(tmp_path, monkeypatch):
    # Komga's title match used to stop a twin of a run tracked before it had a CV
    # id; the folder does that now — and the run gets anchored to the volume.
    dbp, root = _arc_wire(monkeypatch, tmp_path)
    folder = root / "DC Comics" / "Batman (1940)"
    folder.mkdir(parents=True)
    make_cbz(folder / "Batman (1940) #492.cbz")
    run = db.add_series(title="Batman (1940)", publisher="DC Comics", folder_path=str(folder), path=dbp)
    arc = db.add_series(title="Knightfall", publisher="DC Comics", kind="arc", path=dbp)
    db.replace_arc_reading_order(arc, [
        {"reading_order": 1, "source_title": "Batman", "number": "492", "story_title": "",
         "cv_issue_id": "1", "cv_volume_id": "796"}], dbp)

    class _CV:
        def get_volume_info(self, vid):
            return {"year": 1940, "publisher": "DC Comics"}
    monkeypatch.setattr(arcs, "_comicvine", lambda: _CV())

    assert arcs._track_participating(arc) == {"796": run}
    assert len([s for s in db.get_all_series(dbp) if s.get("kind") != "arc"]) == 1
    assert db.get_series_by_id(run, dbp)["cv_volume_id"] == "796"
