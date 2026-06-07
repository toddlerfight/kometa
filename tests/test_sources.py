"""Path accessors — the comics root (the one required setting) and the derived
download staging dir. Config wins over env wins over default; staging hides
under the root unless explicitly overridden.
"""
import kometa.db as db
import kometa.sources as sources


def _fresh(tmp_path, monkeypatch):
    dbp = str(tmp_path / "k.db")
    monkeypatch.delenv("COMICS_ROOT", raising=False)
    monkeypatch.delenv("KOMETA_DOWNLOADS", raising=False)
    db.init_db(dbp)  # no env -> nothing seeded
    monkeypatch.setattr(sources, "DB_PATH", dbp)
    return dbp


class TestComicsRoot:
    def test_db_config_wins(self, tmp_path, monkeypatch):
        dbp = _fresh(tmp_path, monkeypatch)
        monkeypatch.setenv("COMICS_ROOT", "/from-env")
        db.set_config({"comics_root": "/library"}, dbp)
        assert sources.comics_root() == "/library"

    def test_env_when_no_config(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        monkeypatch.setenv("COMICS_ROOT", "/from-env")
        assert sources.comics_root() == "/from-env"

    def test_default_when_nothing_set(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        assert sources.comics_root() == "/comics"


class TestStagingDir:
    def test_hidden_child_of_comics_root(self, tmp_path, monkeypatch):
        dbp = _fresh(tmp_path, monkeypatch)
        db.set_config({"comics_root": "/library"}, dbp)
        assert sources.staging_dir() == "/library/.kometa-staging"

    def test_env_override(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        monkeypatch.setenv("KOMETA_DOWNLOADS", "/dls")
        assert sources.staging_dir() == "/dls"
