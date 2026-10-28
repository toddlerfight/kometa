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
    auth._basic_seen.clear(); auth._basic_bad.clear()
    monkeypatch.setattr(auth, "FAIL_PAUSE", 0)                    # the failed-login pause
    monkeypatch.setattr(auth, "_ENABLED_TTL", 0)                  # the switch bites at once in tests
    auth._fails.clear()
    auth._last_used.clear()
    auth.forget_enabled()
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


ADMIN_PW, WRONG = "hunter2222", "nope"


def test_off_is_the_old_world_everyone_is_the_admin(a):
    c = TestClient(_app())
    assert c.get("/api/ondeck").json() == {"reader": "me"}
    assert c.patch("/api/config").status_code == 200
    assert c.get("/api/auth/me").json()["role"] == "admin"


def test_the_first_admin_claims_the_owners_reading_and_readers_get_their_own(a):
    admin = auth.create_user("marcus", "hunter2222", "admin", a)
    tester = auth.create_user("Tess", "secret99", "reader", a)
    assert admin["id"] == "me"
    assert tester["id"].startswith("u-tess-")
    with pytest.raises(ValueError):
        auth.create_user("TESS", "another1", path=a)                 # usernames ignore case


def test_on_nobody_gets_in_without_a_credential(a):
    auth.create_user("marcus", "hunter2222", "admin", a)
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
    auth.create_user("marcus", "hunter2222", "admin", a)
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
    auth.create_user("marcus", "hunter2222", "admin", a)
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
    auth.create_user("marcus", "hunter2222", "admin", a)
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


def test_a_reader_reads_issue_details_from_the_record_and_never_fetches(a, monkeypatch, series):
    """Summary, credits and covers for YondeB's Issue details sheet: the admin's path may fill from a
    catalogue; a reader's answers from what's stored — a tester must not spend Metron's budget."""
    from kometa import issue_meta, record
    assert auth.reader_may("GET", f"/api/series/{series}/issues/3/locg-details")
    monkeypatch.setattr(issue_meta, "details_for", lambda *x, **k: (_ for _ in ()).throw(AssertionError("fetched")))
    monkeypatch.setattr(record, "fill_variants", lambda *x, **k: (_ for _ in ()).throw(AssertionError("fetched")))
    issue = {"tracked_series_id": series, "number": 3.0, "store_date": "2026-01-07"}
    assert record.details(issue, a, stored_only=True)["record"] is False       # nothing kept: empty, no fetch
    assert record.variants(issue, a, stored_only=True) == {"covers": [], "source": None, "record": False}


def test_the_switch_bites_without_a_restart(tmp_path):
    """The CLI flips auth_enabled from another process; the app's config cache must not hide it."""
    a = str(tmp_path / "a.db")
    db.init_db(a)
    db.get_config(a)                       # the app has its config cached
    import sqlite3
    with sqlite3.connect(a) as c:          # the CLI, elsewhere
        c.execute("INSERT INTO config (key, value) VALUES ('auth_enabled', '1') "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    assert auth.enabled(a)
    with sqlite3.connect(a) as c:
        c.execute("UPDATE config SET value = '0' WHERE key = 'auth_enabled'")
    assert not auth.enabled(a)


CF = {"Cf-Connecting-IP": "203.0.113.9"}


def test_through_the_tunnel_the_web_app_is_a_closed_door(a):
    """The public address carries /api and /opds; anything else is the name and nowhere to go —
    even with an admin's own token. Home and tailnet (no Cf header) are untouched."""
    auth.create_user("marcus", ADMIN_PW, "admin", a)
    db.set_config({"auth_enabled": "1"}, a)
    c = TestClient(_app())
    tok = c.post("/api/auth/login", json={"username": "marcus", "password": ADMIN_PW}).json()["token"]
    front = c.get("/", headers=CF)
    assert front.status_code == 200 and "コメタ" in front.text and "Sign in" not in front.text and "<form" not in front.text
    assert c.get("/login", headers=CF).status_code == 404
    assert c.get("/app.js", headers={**CF, "Authorization": f"Bearer {tok}"}).status_code == 404
    assert "コメタ" in c.get("/", headers={**CF, "Authorization": f"Bearer {tok}"}).text     # even signed in: the name only
    anon = TestClient(_app())                                                         # no cookie from the login above
    assert anon.get("/api/ondeck", headers=CF).status_code == 401                    # the apps' door: Kometa's login
    assert anon.get("/api/ondeck", headers={**CF, "Authorization": f"Bearer {tok}"}).status_code == 200
    assert anon.get("/opds", headers=CF).status_code == 401
    assert c.get("/", headers={"Authorization": f"Bearer {tok}"}).json() == {"web": True}   # home: the web app


def test_with_login_off_the_tunnel_carries_nothing(a):
    c = TestClient(_app())
    assert c.get("/api/ondeck").status_code == 200                                   # home: the old world
    assert c.get("/api/ondeck", headers=CF).status_code == 503
    assert c.get("/opds", headers=CF).status_code == 503


# --- failed-login throttle (the one door the internet can reach) -----------------------------------
def _ip(ip):
    return {"cf-connecting-ip": ip}


def test_five_misses_lock_the_name_and_the_ip(a):
    auth.create_user("marcus", ADMIN_PW, "admin", a)
    db.set_config({"auth_enabled": "1"}, a)
    c = TestClient(_app())
    for _ in range(auth.FAIL_FREE):
        assert c.post("/api/auth/login", json={"username": "marcus", "password": WRONG}, headers=_ip("1.2.3.4")).status_code == 401
    r = c.post("/api/auth/login", json={"username": "marcus", "password": ADMIN_PW}, headers=_ip("1.2.3.4"))
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1      # even the RIGHT password waits now
    # the name is locked from anywhere, and the IP is locked for any name
    assert c.post("/api/auth/login", json={"username": "marcus", "password": ADMIN_PW}, headers=_ip("9.9.9.9")).status_code == 429
    assert c.post("/api/auth/login", json={"username": "someone", "password": WRONG}, headers=_ip("1.2.3.4")).status_code == 429
    # a different name from a different address is untouched
    assert c.post("/api/auth/login", json={"username": "someone", "password": WRONG}, headers=_ip("9.9.9.9")).status_code == 401


def test_a_right_password_before_the_lock_clears_the_count(a):
    auth.create_user("marcus", ADMIN_PW, "admin", a)
    db.set_config({"auth_enabled": "1"}, a)
    c = TestClient(_app())
    for _ in range(auth.FAIL_FREE - 1):
        c.post("/api/auth/login", json={"username": "marcus", "password": WRONG}, headers=_ip("1.2.3.4"))
    assert c.post("/api/auth/login", json={"username": "marcus", "password": ADMIN_PW}, headers=_ip("1.2.3.4")).status_code == 200
    assert not any(k.endswith("marcus") or k.endswith("1.2.3.4") for k in auth._fails)


def test_the_lock_doubles_and_caps(a):
    for n in range(auth.FAIL_FREE + 12):
        auth.note_failure("x", "1.1.1.1")
    assert auth.locked_for("x", "1.1.1.1") <= auth.FAIL_LOCK_MAX + 1
    assert auth.locked_for("x", "1.1.1.1") > 60


def test_admin_passwords_are_ten_plus_readers_six(a):
    with pytest.raises(ValueError):
        auth.create_user("boss", "short123", "admin", a)
    auth.create_user("boss", "longenough1", "admin", a)
    t = auth.create_user("tess", "secret", "reader", a)
    with pytest.raises(ValueError):
        auth.set_password(OWNER_ID := "me", "nine_char", a)
    auth.set_password(t["id"], "sixsix", a)


def test_a_password_change_drops_a_cached_basic_pair_at_once(a):
    t = auth.create_user("tess", "secret99", "reader", a)
    db.set_config({"auth_enabled": "1"}, a)
    c = TestClient(_app())
    basic = "Basic " + base64.b64encode(b"tess:secret99").decode()
    assert c.get("/opds", headers={"authorization": basic}).status_code == 200
    auth.set_password(t["id"], "newpass77", a)
    assert c.get("/opds", headers={"authorization": basic}).status_code == 401


# --- what the public address gives away --------------------------------------------------------------
def test_version_through_the_tunnel_is_blank_until_signed_in(a, monkeypatch):
    import kometa.main as main
    monkeypatch.setattr(main, "DB_PATH", a)
    auth.create_user("marcus", ADMIN_PW, "admin", a)
    db.set_config({"auth_enabled": "1"}, a)
    c = TestClient(main.app)
    anon = c.get("/api/version", headers=_ip("1.2.3.4")).json()
    assert anon == {"app": "kometa", "sha": ""}                            # YondeB wants a sha key, nothing more
    home = c.get("/api/version").json()
    assert home.get("sha") not in ("", None)
    tok = c.post("/api/auth/login", json={"username": "marcus", "password": ADMIN_PW}).json()["token"]
    signed = c.get("/api/version", headers={"authorization": f"Bearer {tok}", **_ip("1.2.3.4")}).json()
    assert signed["sha"] == home["sha"]


def test_every_response_carries_the_hardening_headers(a, monkeypatch):
    import kometa.main as main
    monkeypatch.setattr(main, "DB_PATH", a)
    r = TestClient(main.app).get("/api/version")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert r.headers["referrer-policy"] == "same-origin"


# --- the gate's DB work stays off the event loop -----------------------------------------------------
def test_the_switch_is_remembered_for_a_moment(a, monkeypatch):
    monkeypatch.setattr(auth, "_ENABLED_TTL", 60)
    auth.forget_enabled()
    assert auth.enabled() is False
    db.set_config({"auth_enabled": "1"}, a)
    assert auth.enabled() is False                                  # cached
    auth.forget_enabled()
    assert auth.enabled() is True


def test_last_used_is_written_by_the_flusher_not_the_request(a):
    auth.create_user("marcus", ADMIN_PW, "admin", a)
    db.set_config({"auth_enabled": "1"}, a)
    c = TestClient(_app())
    tok = c.post("/api/auth/login", json={"username": "marcus", "password": ADMIN_PW}).json()["token"]
    assert c.get("/api/ondeck", headers={"authorization": f"Bearer {tok}"}).status_code == 200
    with db._connect(a) as conn:
        assert conn.execute("SELECT last_used FROM auth_tokens").fetchone()[0] is None
    assert auth._last_used
    assert auth.flush_last_used(a) == 1
    with db._connect(a) as conn:
        assert conn.execute("SELECT last_used FROM auth_tokens").fetchone()[0]
    assert auth.flush_last_used(a) == 0


def test_users_list_carries_each_readers_reading(a, series):
    """The admin's Accounts list says what each reader is up to: the last book they
    touched (series, number, page of pages, when), counts, and push platforms."""
    jane = auth.create_user("jane", "secret99", "reader", a)
    b1 = db.upsert_book("/comics/Image Comics/Saga/Saga 001.cbz", 1, 1, ["p1.jpg"] * 40, series, 1, path=a)
    b2 = db.upsert_book("/comics/Image Comics/Saga/Saga 002.cbz", 1, 1, ["p1.jpg"] * 44, series, 2, path=a)
    with db._connect(a) as conn:
        conn.execute("INSERT INTO read_progress (reader_id, book_id, page, completed, updated_at) VALUES (?, ?, 40, 1, '2026-10-15 09:00:00')", (jane["id"], b1))
        conn.execute("INSERT INTO read_progress (reader_id, book_id, page, completed, updated_at) VALUES (?, ?, 7, 0, '2026-10-15 10:21:00')", (jane["id"], b2))
    c = TestClient(_app())                                               # auth off: the caller is the admin
    rows = {r["username"]: r for r in c.get("/api/users").json()}
    r = rows["jane"]["reading"]
    assert r["in_progress"] == 1 and r["finished"] == 1 and r["push"] == []
    assert r["last"]["number"] == 2 and r["last"]["page"] == 7 and r["last"]["pages"] == 44
    assert r["last"]["finished"] is False and r["last"]["at"] == "2026-10-15 10:21:00"
    assert r["last"]["series"]                                            # the fixture series' title
    auth.create_user("jotham", "secret99", "reader", a)
    rows = {r["username"]: r for r in c.get("/api/users").json()}
    assert rows["jotham"]["reading"] == {"last": None, "in_progress": 0, "finished": 0, "push": []}


def test_basic_guessing_is_throttled_like_the_login_route(a, monkeypatch):
    """OPDS is on the public address too: wrong Basic pairs count against the name and the
    address, a locked one gets 429 without scrypt, and the same wrong pair sent again by a
    retrying app is answered from memory (one miss counted, one scrypt run)."""
    auth.create_user("tess", "secret99", "reader", a)
    db.set_config({"auth_enabled": "1"}, a)
    c = TestClient(_app())
    runs = []
    real = auth.user_for_password
    monkeypatch.setattr(auth, "user_for_password", lambda u, p, path=None: runs.append(u) or real(u, p, path))
    b = lambda pw: {"Authorization": "Basic " + base64.b64encode(f"tess:{pw}".encode()).decode(), **_ip("1.2.3.4")}
    assert c.get("/opds", headers=b("wrong")).status_code == 401
    assert c.get("/opds", headers=b("wrong")).status_code == 401       # the same pair: remembered, not re-run
    assert len(runs) == 1
    for i in range(auth.FAIL_FREE - 1):
        assert c.get("/opds", headers=b(f"wrong{i}")).status_code == 401
    r = c.get("/opds", headers=b("secret99"))
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1    # locked: even the right password waits
    n = len(runs)
    assert c.get("/opds", headers=b("secret99")).status_code == 429 and len(runs) == n   # and scrypt never ran
    # the name is locked for the login route too, and from anywhere
    right = "secret99"
    assert c.post("/api/auth/login", json={"username": "tess", "password": right}, headers=_ip("9.9.9.9")).status_code == 429


def test_a_right_basic_pair_clears_the_count(a):
    auth.create_user("tess", "secret99", "reader", a)
    db.set_config({"auth_enabled": "1"}, a)
    c = TestClient(_app())
    b = lambda pw: {"Authorization": "Basic " + base64.b64encode(f"tess:{pw}".encode()).decode(), **_ip("1.2.3.4")}
    for i in range(auth.FAIL_FREE - 1):
        assert c.get("/opds", headers=b(f"wrong{i}")).status_code == 401
    assert c.get("/opds", headers=b("secret99")).status_code == 200
    assert auth.locked_for("tess", "1.2.3.4") == 0
