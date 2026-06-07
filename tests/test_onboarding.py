"""Komga-optional onboarding: adding a series with no Komga configured must still
land a correct folder_path, derived from publisher+title. This is what lets the
first sync reconcile owned-vs-missing against disk instead of falling into the
'own nothing, download everything' trap.
"""
import kometa.db as db
import kometa.main as main
from kometa.main import AddSeriesRequest


class _FakeMetron:
    """No metron_id given -> add_series only calls search_series, which we stub empty."""
    def search_series(self, q):
        return []


def _wire(monkeypatch, tmp_path, comics_root):
    dbp = str(tmp_path / "k.db")
    db.init_db(dbp)
    monkeypatch.setattr(main, "DB_PATH", dbp)
    monkeypatch.setattr(main, "_comics_root", lambda: str(comics_root))
    monkeypatch.setattr(main, "_komga", lambda: None)          # no Komga
    monkeypatch.setattr(main, "_metron", lambda: _FakeMetron())
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


def test_locg_add_skips_metron_autolink_when_unconfigured(tmp_path, monkeypatch):
    """A keyless LOCG add must not fire the Metron auto-link (a guaranteed 401)."""
    root = tmp_path / "comics"
    root.mkdir()
    _wire(monkeypatch, tmp_path, root)  # fresh DB -> no metron creds

    class ExplodingMetron:
        def search_series(self, q):
            raise AssertionError("Metron must not be queried when unconfigured")
    monkeypatch.setattr(main, "_metron", lambda: ExplodingMetron())

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


class TestMetronSearchDegradesGracefully:
    """A missing/failing Metron must return [] (not 500) so the wizard reaches LOCG."""

    def test_not_configured_returns_empty(self, tmp_path, monkeypatch):
        dbp = str(tmp_path / "k.db")
        db.init_db(dbp)  # fresh DB has no metron_user/metron_pass
        monkeypatch.setattr(main, "DB_PATH", dbp)
        # _metron must never even be called when creds are absent
        def _boom():
            raise AssertionError("_metron() should not be called when unconfigured")
        monkeypatch.setattr(main, "_metron", _boom)

        assert main.search_metron("saga") == []

    def test_configured_but_failing_returns_empty(self, tmp_path, monkeypatch):
        dbp = str(tmp_path / "k.db")
        db.init_db(dbp)
        db.set_config({"metron_user": "u", "metron_pass": "p"}, dbp)
        monkeypatch.setattr(main, "DB_PATH", dbp)

        class FailingMetron:
            def search_series(self, q):
                raise RuntimeError("401 Unauthorized")
        monkeypatch.setattr(main, "_metron", lambda: FailingMetron())

        assert main.search_metron("saga") == []
