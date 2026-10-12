"""Grand Comics Database connection: the Settings card's plumbing. No network —
requests.get is stubbed; the one real knock happens from the Settings 'test'."""
import kometa.db as db
from kometa import gcd_client, main


class _Resp:
    def __init__(self, status):
        self.status_code = status


def _wire(tmp_path, monkeypatch):
    dbp = str(tmp_path / "k.db")
    db.init_db(dbp)
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(main, "DB_PATH", dbp)
    monkeypatch.setattr(main, "_comics_root", lambda: "/comics")
    return dbp


def test_not_configured_makes_no_request(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(gcd_client.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert gcd_client.test() == (False, "Not configured")


def test_stored_login_is_sent_as_basic_auth(tmp_path, monkeypatch):
    dbp = _wire(tmp_path, monkeypatch)
    db.set_config({"gcd_user": "me@example.com", "gcd_pass": "pw"}, dbp)
    seen = {}

    def fake_get(url, params=None, auth=None, headers=None, timeout=None):
        seen.update(url=url, auth=auth, ua=headers.get("User-Agent"))
        return _Resp(200)

    monkeypatch.setattr(gcd_client.requests, "get", fake_get)
    ok, _ = gcd_client.test()
    assert ok is True
    assert seen["auth"] == ("me@example.com", "pw")
    assert seen["url"].startswith("https://www.comics.org/api/")
    assert seen["ua"]                                   # never anonymous-agent


def test_refused_login_reads_as_refused(tmp_path, monkeypatch):
    dbp = _wire(tmp_path, monkeypatch)
    db.set_config({"gcd_user": "me", "gcd_pass": "wrong"}, dbp)
    for status in (401, 403):
        monkeypatch.setattr(gcd_client.requests, "get", lambda *a, _s=status, **k: _Resp(_s))
        ok, detail = gcd_client.test()
        assert ok is False and "refused" in detail


def test_config_never_echoes_the_password(tmp_path, monkeypatch):
    dbp = _wire(tmp_path, monkeypatch)
    db.set_config({"gcd_user": "me", "gcd_pass": "secret"}, dbp)
    cfg = main.get_config()
    assert cfg["gcd_user"] == "me"
    assert cfg["gcd_pass"] == ""
    assert cfg["gcd_configured"] is True
    assert cfg["gcd_enabled"] is True                  # absent = enabled


def test_disconnect_clears_the_login(tmp_path, monkeypatch):
    dbp = _wire(tmp_path, monkeypatch)
    db.set_config({"gcd_user": "me", "gcd_pass": "secret"}, dbp)
    cfg = main.disconnect_integration("gcd")
    assert cfg["gcd_configured"] is False
    assert db.get_config(dbp).get("gcd_pass") == ""
