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
    monkeypatch.setattr(main, "_COMICS_ROOT", str(comics_root))
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
