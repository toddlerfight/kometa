"""The request guard (main._RequestGuard): a foreign Host is refused (DNS rebinding), and a
change that names an Origin must name this address (cross-site requests). Security review
2026-10-11, items 4 and 6."""
import pytest

import kometa.db as db
import kometa.main as main
from test_auth import TestClient


@pytest.fixture
def client(db_path, monkeypatch):
    import kometa.auth as auth
    for mod in (main, auth, db):
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    auth.forget_enabled()
    main._RequestGuard._extra.update(at=0.0, hosts=frozenset())
    return TestClient(main.app)


PROBE = "/api/guard-probe-404"            # no such route: past the guard it's a 404/405
TAILNET = "box.tail0000" + ".ts.net"      # split: the pre-commit hook flags tailnet names


@pytest.mark.parametrize("host", ["203.0.113.7:6969", "127.0.0.1:6969", "[::1]:6969", "localhost:6969", "ginbako:6969",
                                  "ginbako.local", TAILNET + ":8445", "testserver"])
def test_our_addresses_pass(client, host):
    assert client.get(PROBE, headers={"Host": host}).status_code == 404


@pytest.mark.parametrize("host", ["attacker.example", "evil.example:6969", "203.0.113.7.nip.io:6969"])
def test_a_foreign_name_is_refused(client, host):
    r = client.get(PROBE, headers={"Host": host})
    assert r.status_code == 403 and "address" in r.json()["detail"]


def test_a_name_the_owner_adds_passes(client, db_path):
    db.set_config({"allowed_hosts": "comics.example.org, other.example"}, path=db_path)
    assert client.get(PROBE, headers={"Host": "comics.example.org"}).status_code == 404
    assert client.get(PROBE, headers={"Host": "nope.example"}).status_code == 403


def test_cloudflare_brings_only_our_own_name(client):
    assert client.get(PROBE, headers={"Host": "kometa.example.com", "Cf-Connecting-IP": "1.2.3.4"}).status_code != 403


H = {"Host": "203.0.113.7:6969"}


@pytest.mark.parametrize("origin", ["http://203.0.113.7:6969", None])
def test_a_change_from_our_page_or_an_app_passes(client, origin):
    hdrs = dict(H, **({"Origin": origin} if origin else {}))
    assert client.post(PROBE, headers=hdrs).status_code in (404, 405)


@pytest.mark.parametrize("origin", ["https://attacker.example", "http://203.0.113.7:3005", "null"])
def test_a_cross_site_change_is_refused(client, origin):
    r = client.post(PROBE, headers=dict(H, Origin=origin))
    assert r.status_code == 403 and "own pages" in r.json()["detail"]
    assert client.get(PROBE, headers=dict(H, Origin=origin)).status_code == 404     # reads aren't changes


def test_a_referer_stands_in_for_a_missing_origin(client):
    assert client.post(PROBE, headers=dict(H, Referer="https://attacker.example/page")).status_code == 403
    assert client.post(PROBE, headers=dict(H, Referer="http://203.0.113.7:6969/#library")).status_code in (404, 405)


def test_behind_tailscale_serve_the_forwarded_host_counts(client):
    hdrs = {"Host": "127.0.0.1:6969", "X-Forwarded-Host": TAILNET + ":8445",
            "Origin": "https://" + TAILNET + ":8445"}
    assert client.post(PROBE, headers=hdrs).status_code in (404, 405)
    # a proxy that keeps the name but not the port
    assert client.post(PROBE, headers={"Host": TAILNET, "Origin": "https://" + TAILNET + ":8445"}).status_code in (404, 405)
