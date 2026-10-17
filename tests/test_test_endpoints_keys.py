"""Connection tests send the saved key to the saved address only (main._for_address).
Security review 2026-10-11 item 1: a test naming another server, key left blank, got
the saved SAB / Prowlarr / qBit key sent there."""
import pytest

import kometa.db as db
import kometa.main as main
from test_auth import TestClient


@pytest.fixture
def c(db_path, monkeypatch):
    import kometa.auth as auth
    for mod in (main, auth, db):
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    auth.forget_enabled()
    db.set_config({"sab_url": "http://sab:8080", "sab_apikey": "SAB-K1",
                   "prowlarr_url": "http://prowlarr:9696", "prowlarr_apikey": "PRW-K1",
                   "qbit_url": "http://qbit:8090", "qbit_user": "me", "qbit_pass": "QB-P1"}, path=db_path)
    return TestClient(main.app)


@pytest.fixture
def sent(monkeypatch):
    out = []
    import kometa.sabnzbd_client as sab
    import kometa.prowlarr_client as pr
    import kometa.qbittorrent_client as qb
    monkeypatch.setattr(sab.SABnzbdClient, "_api", lambda self, **kw: out.append(("sab", self.url, self.apikey)) or {"status": True})
    monkeypatch.setattr(pr.ProwlarrClient, "test", lambda self: out.append(("prowlarr", self.base_url, self.apikey)) or (True, "ok"))
    monkeypatch.setattr(qb.QBittorrentClient, "test", lambda self: out.append(("qbit", self.url, self.password)) or (True, "ok"))
    return out


def test_the_saved_key_goes_to_the_saved_address(c, sent):
    for name in ("sab", "prowlarr", "qbit"):
        assert c.post(f"/api/test/{name}", json={}).json()["ok"]
    assert [s[0] for s in sent] == ["sab", "prowlarr", "qbit"]
    assert ("sab", "http://sab:8080", "SAB-K1") in sent
    # the same address typed again (trailing slash, case) is still the saved one
    assert c.post("/api/test/sab", json={"url": "HTTP://sab:8080/"}).json()["ok"]


@pytest.mark.parametrize("name", ["sab", "prowlarr", "qbit"])
def test_another_address_never_gets_the_saved_key(c, sent, name):
    r = c.post(f"/api/test/{name}", json={"url": "http://attacker.example:8000"}).json()
    assert not r["ok"] and "key for this address" in r["error"]
    assert sent == []


def test_another_address_with_its_own_key_is_tested(c, sent):
    assert c.post("/api/test/sab", json={"url": "http://newsab:8080", "apikey": "NEWKEY"}).json()["ok"]
    assert sent == [("sab", "http://newsab:8080", "NEWKEY")]
