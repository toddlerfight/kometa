"""Accounts (kometa/auth.py, docs/accounts-spec.md): who's asking, and may they. No network."""
import asyncio
import base64
import json
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI

import kometa.db as db
from kometa import auth, marks


@pytest.fixture
def a(db_path, monkeypatch):
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(marks, "DB_PATH", db_path)
    auth._ready.clear()
    auth._seen.clear()
    auth._basic_seen.clear()
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)       # the failed-login pause
    return db_path


class _Resp:
    def __init__(self, status, headers, body):
        self.status_code, self.headers, self.content = status, headers, body
        self.text = body.decode("utf-8", "replace")
        self.cookies = {}
        for k, v in headers.items():
            if k == "set-cookie":
                c = SimpleCookie(); c.load(v)
                self.cookies.update({n: m.value for n, m in c.items()})

    def json(self):
        return json.loads(self.content)


class TestClient:
    """Just enough of a client to drive the ASGI app in-process (starlette's
    wants httpx2, which this repo doesn't carry). Keeps cookies like a browser."""
    __test__ = False

    def __init__(self, app):
        self.app, self.jar = app, {}

    def request(self, method, path, json_body=None, headers=None):
        body = json.dumps(json_body).encode() if json_body is not None else b""
        hs = {k.lower(): v for k, v in (headers or {}).items()}
        if json_body is not None:
            hs["content-type"] = "application/json"
        if self.jar and "cookie" not in hs:
            hs["cookie"] = "; ".join(f"{k}={v}" for k, v in self.jar.items())
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                 "root_path": "", "headers": [(k.encode(), v.encode()) for k, v in hs.items()],
                 "client": ("test", 1), "server": ("test", 80)}
        out = {"status": 0, "headers": {}, "body": b""}
        sent = {"done": False}

        async def receive():
            if sent["done"]:
                return {"type": "http.disconnect"}
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(m):
            if m["type"] == "http.response.start":
                out["status"] = m["status"]
                for k, v in m.get("headers", []):
                    out["headers"][k.decode().lower()] = v.decode("latin-1")
            elif m["type"] == "http.response.body":
                out["body"] += m.get("body", b"")

        asyncio.run(self.app(scope, receive, send))
        r = _Resp(out["status"], out["headers"], out["body"])
        for k, v in r.cookies.items():
            if v:
                self.jar[k] = v
            else:
                self.jar.pop(k, None)
        return r

    def get(self, path, headers=None):
        return self.request("GET", path, headers=headers)

    def post(self, path, json=None, headers=None):
        return self.request("POST", path, json, headers)

    def put(self, path, json=None, headers=None):
        return self.request("PUT", path, json, headers)

    def patch(self, path, json=None, headers=None):
        return self.request("PATCH", path, json, headers)

    def delete(self, path, headers=None):
        return self.request("DELETE", path, headers=headers)


def _app():
    """The gate and the auth routes, plus stand-ins for a reader route and an admin one."""
    app = FastAPI()
    app.add_middleware(auth.AuthGate)
    app.include_router(auth.router)

    @app.get("/api/ondeck")
    def deck():
        return {"reader": auth.current_reader()}

    @app.patch("/api/config")
    def config():
        return {"by": auth.current_reader()}

    @app.get("/opds")
    def opds():
        return {"reader": auth.current_reader()}

    @app.get("/")
    def web():
        return {"web": True}
    return app


ADMIN_PW, WRONG = "hunter22", "nope"


def test_off_is_the_old_world_everyone_is_the_admin(a):
    c = TestClient(_app())
    assert c.get("/api/ondeck").json() == {"reader": "me"}
    assert c.patch("/api/config").status_code == 200
    assert c.get("/api/auth/me").json()["role"] == "admin"


def test_the_first_admin_claims_the_owners_reading_and_readers_get_their_own(a):
    admin = auth.create_user("marcus", "hunter22", "admin", a)
    tester = auth.create_user("Tess", "secret99", "reader", a)
    assert admin["id"] == "me"
    assert tester["id"].startswith("u-tess-")
    with pytest.raises(ValueError):
        auth.create_user("TESS", "another1", path=a)                 # usernames ignore case


def test_on_nobody_gets_in_without_a_credential(a):
    auth.create_user("marcus", "hunter22", "admin", a)
    db.set_config({"auth_enabled": "1"}, a)
    c = TestClient(_app())
    assert c.get("/api/ondeck").status_code == 401
    assert c.patch("/api/config").status_code == 401
    r = c.get("/opds")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    web = c.get("/")
    assert web.status_code == 200 and "Sign in" in web.text            # the web app shows the login page
    assert c.get("/api/version").status_code != 401                     # how YondeB spots a Kometa


def test_login_gives_a_token_and_a_cookie_and_the_token_works(a):
    auth.create_user("marcus", "hunter22", "admin", a)
    db.set_config({"auth_enabled": "1"}, a)
    c = TestClient(_app())
    assert c.post("/api/auth/login", json={"username": "marcus", "password": WRONG}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "Marcus", "password": ADMIN_PW, "device": "iPad"})
    assert r.status_code == 200 and auth.COOKIE in r.cookies
    token = r.json()["token"]
    bare = TestClient(_app())
    assert bare.get("/api/ondeck", headers={"Authorization": f"Bearer {token}"}).json() == {"reader": "me"}
    assert c.get("/api/ondeck").json() == {"reader": "me"}               # the cookie, as the web app
    c.post("/api/auth/logout")
    assert bare.get("/api/ondeck", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_a_reader_reads_and_cannot_touch_the_admin_side(a):
    auth.create_user("marcus", "hunter22", "admin", a)
    t = auth.create_user("tess", "secret99", "reader", a)
    db.set_config({"auth_enabled": "1"}, a)
    h = {"Authorization": f"Bearer {auth.issue_token(t['id'], 'phone', a)}"}
    c = TestClient(_app())
    assert c.get("/api/ondeck", headers=h).json() == {"reader": t["id"]}
    assert c.patch("/api/config", headers=h).status_code == 403
    web = c.get("/", headers=h)
    assert web.status_code == 403 and "YondeB" in web.text


def test_opds_apps_sign_in_with_basic(a):
    t = auth.create_user("tess", "secret99", "reader", a)
    db.set_config({"auth_enabled": "1"}, a)
    good = base64.b64encode(b"tess:secret99").decode()
    bad = base64.b64encode(b"tess:wrong").decode()
    c = TestClient(_app())
    assert c.get("/opds", headers={"Authorization": f"Basic {good}"}).json() == {"reader": t["id"]}
    assert c.get("/opds", headers={"Authorization": f"Basic {bad}"}).status_code == 401


def test_a_new_password_or_disabling_signs_every_device_out(a):
    t = auth.create_user("tess", "secret99", "reader", a)
    tok = auth.issue_token(t["id"], "phone", a)
    assert auth.user_for_token(tok, a)
    auth.set_password(t["id"], "fresh-one", a)
    assert auth.user_for_token(tok, a) is None
    assert auth.user_for_password("tess", "fresh-one", a)


def test_marks_follow_whoever_is_asking(a):
    t = auth.create_user("tess", "secret99", "reader", a)
    marks.set_mark("book", 7, rating=5, path=a)                          # outside a request: the owner
    tok = auth.as_reader(auth.get_user(t["id"], a))
    try:
        assert marks.get_mark("book", 7, path=a)["rating"] is None      # tess never rated it
        marks.set_mark("book", 7, rating=2, path=a)
    finally:
        auth._CURRENT.reset(tok)
    assert marks.get_mark("book", 7, path=a)["rating"] == 5             # the owner's stays the owner's


def test_a_readers_pull_list_is_their_own_and_never_the_admins(a, series):
    t = auth.create_user("tess", "secret99", "reader", a)
    db.set_config({"auth_enabled": "1"}, a)
    c = TestClient(_app())
    h = {"Authorization": f"Bearer {auth.issue_token(t['id'], 'phone', a)}"}
    assert c.get("/api/me/pull-list", headers=h).json() == {"series": [], "downloads": False}
    assert c.put(f"/api/me/pull-list/{series}", json={"on": True}, headers=h).json() == {"series": [series]}
    assert auth.pull_ids("me", a) == {series}                            # the fixture series is on the admin's list
    with db._connect(a) as conn:
        assert conn.execute("SELECT on_pull_list FROM tracked_series WHERE id = ?", (series,)).fetchone()[0] == 1


def test_prefs_merge_and_a_null_deletes(a):
    t = auth.create_user("tess", "secret99", "reader", a)
    db.set_config({"auth_enabled": "1"}, a)
    c = TestClient(_app())
    h = {"Authorization": f"Bearer {auth.issue_token(t['id'], 'phone', a)}"}
    c.patch("/api/me/prefs", json={"library.sort": "date", "ondeck.more": True}, headers=h)
    assert c.patch("/api/me/prefs", json={"ondeck.more": None}, headers=h).json() == {"library.sort": "date"}


def test_the_last_admin_cannot_be_demoted_or_deleted(a):
    auth.create_user("marcus", "hunter22", "admin", a)
    c = TestClient(_app())                                               # auth off: the caller is the admin
    assert c.patch("/api/users/me", json={"role": "reader"}).status_code == 409
    assert c.delete("/api/users/me").status_code == 409


@pytest.mark.parametrize("method,path,reader", [
    ("GET", "/api/series", True), ("GET", "/api/books/12/pages/3", True), ("PUT", "/api/books/12/progress", True),
    ("PUT", "/api/books/12/mark", True), ("GET", "/opds/series/4", True), ("POST", "/api/wantread", True),
    ("GET", "/api/ondeck/because", True), ("PATCH", "/api/me/prefs", True),
    ("GET", "/api/config", False), ("PATCH", "/api/config", False), ("GET", "/api/queue", False),
    ("POST", "/api/retag/batch/swap", False), ("DELETE", "/api/series/4", False),
    ("PATCH", "/api/series/4/pull-list", False), ("POST", "/api/series/4/issues/3/search", False),
    ("GET", "/api/fs/browse", False), ("POST", "/api/sync", False), ("GET", "/api/users", False),
    ("GET", "/api/harvest/progress", False), ("GET", "/api/search/locg", False),
])
def test_the_reader_allow_list(method, path, reader):
    assert auth.reader_may(method, path) is reader


def test_every_reader_route_on_the_list_is_a_real_route():
    """A typo in the allow-list would quietly lock readers out; every pattern must hit something real."""
    from kometa.main import app
    real = [(m, r.path) for r in app.routes for m in getattr(r, "methods", None) or ()]
    for p in auth.READER_GET:
        assert any(m == "GET" and p.fullmatch(_sample(path)) for m, path in real), p.pattern


def _sample(path: str) -> str:
    import re
    return re.sub(r"\{[^}]+\}", "1", path)


def test_a_revoke_from_another_process_bites_at_once(a):
    """The CLI revokes in its own process; the server must not keep honouring the token from memory."""
    t = auth.create_user("tess", "secret99", "reader", a)
    tok = auth.issue_token(t["id"], "phone", a)
    assert auth.user_for_token(tok, a)
    with db._connect(a) as conn:                                          # what the CLI does, behind the server's back
        conn.execute("DELETE FROM auth_tokens WHERE user_id = ?", (t["id"],))
    assert auth.user_for_token(tok, a) is None
