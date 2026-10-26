"""Accounts: who's asking, and are they allowed to (docs/accounts-spec.md).

Kometa can't tell callers apart by address — everything walks in through
Docker's gateway or localhost, the iPad at home and a stranger on the tailnet
alike — so there's no trusted-network shortcut. One rule: show a credential.
The web app wants a cookie, YondeB a bearer token, OPDS apps Basic, scripts a
CLI-made token. Admin gets everything; a reader gets the shelf and their own
reading state and NOTHING that buys, deletes or reconfigures.

`auth_enabled` = "0" (the default) is the old world: every request is the admin.
It flips on once the owner has signed in everywhere. Locked out? From ginbako:
    docker exec -it kometa python -m kometa.auth password <username>
"""
import argparse
import asyncio
import base64
import contextvars
import getpass
import hashlib
import hmac
import json
import logging
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import kometa.db as db

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
OWNER = "me"                     # the reader id every pre-accounts row already carries
COOKIE = "kometa_session"
COOKIE_AGE = 365 * 24 * 3600
SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)
MIN_PW = {"admin": 10, "reader": 6}

# --- failed-login throttle ------------------------------------------------------------------------
# The login route is the ONE door open to the whole internet (the tunnel carries
# /api). Before this, a wrong guess cost the guesser half a second and cost US a
# threadpool worker for that half second: forty parallel connections = eighty
# guesses a second AND every sync route stalled for everyone. Now a wrong guess
# is counted per username and per client IP; after FAIL_FREE misses the key is
# locked for a doubling wait, 1 s → 2 → 4 … up to FAIL_LOCK_MAX, and a locked
# key gets a 429 without touching scrypt. A right password clears both keys.
# In-memory, so a restart forgives everyone — fine, the lock is seconds to minutes.
FAIL_FREE = 5
FAIL_LOCK_MAX = 900
FAIL_PAUSE = 0.5                 # the old half-second, now awaited, not slept on a worker
_fails: dict[str, tuple[int, float]] = {}       # key -> (misses, locked_until monotonic)
_fails_lock = threading.Lock()


def _fail_keys(username: str, ip: str) -> list[str]:
    return [f"u:{(username or '').strip().lower()[:80]}", f"ip:{ip or '?'}"]


def locked_for(username: str, ip: str) -> int:
    """Seconds a login for this username / from this IP must still wait; 0 = go ahead."""
    now = time.monotonic()
    with _fails_lock:
        return max([0] + [int(until - now) + 1 for k in _fail_keys(username, ip)
                          if (until := _fails.get(k, (0, 0.0))[1]) > now])


def note_failure(username: str, ip: str) -> None:
    now = time.monotonic()
    with _fails_lock:
        for k in _fail_keys(username, ip):
            n = _fails.get(k, (0, 0.0))[0] + 1
            wait = min(FAIL_LOCK_MAX, 2 ** (n - FAIL_FREE)) if n >= FAIL_FREE else 0
            _fails[k] = (n, now + wait)
        if len(_fails) > 10000:                      # a flood of made-up names: keep the newest
            for k in list(_fails)[:5000]:
                _fails.pop(k, None)


def clear_failures(username: str, ip: str) -> None:
    with _fails_lock:
        for k in _fail_keys(username, ip):
            _fails.pop(k, None)


def client_ip(request: Request) -> str:
    """Cloudflare stamps the real client on the tunnel; at home it's the socket."""
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")

# who this request is: a contextvar, so it rides into the threadpool (same trick as
# kometa.interactive). Outside a request — scheduler jobs, threads — it's the owner.
_CURRENT: contextvars.ContextVar[dict | None] = contextvars.ContextVar("kometa_reader", default=None)


ANONYMOUS = {"id": "-anonymous", "username": None, "role": "none"}


def current_reader() -> str:
    u = _CURRENT.get()
    return u["id"] if u else OWNER


def current_user() -> dict:
    return _CURRENT.get() or {"id": OWNER, "username": None, "role": "admin"}


def is_admin() -> bool:
    return current_user()["role"] == "admin"


def as_reader(user: dict):
    """Run a block as someone (tests, CLI). Returns the token for reset()."""
    return _CURRENT.set(user)


# --- storage ------------------------------------------------------------------------------------
_ready: set = set()


def ensure_tables(path=None):
    path = path or DB_PATH
    if path in _ready:
        return
    with db._connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            role TEXT NOT NULL DEFAULT 'reader', pw_hash TEXT,
            disabled INTEGER NOT NULL DEFAULT 0, prefs TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS auth_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            hash TEXT NOT NULL UNIQUE, name TEXT, created_at TEXT NOT NULL, last_used TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS reader_pull (
            reader_id TEXT NOT NULL, series_id INTEGER NOT NULL, added_at TEXT NOT NULL,
            PRIMARY KEY (reader_id, series_id))""")
    _ready.add(path)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, **SCRYPT)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(pw: str, stored: str | None) -> bool:
    try:
        kind, salt, dk = (stored or "").split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), **SCRYPT)
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _public(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "role": u["role"], "disabled": bool(u["disabled"])}


def get_user(user_id: str, path=None) -> dict | None:
    ensure_tables(path)
    with db._connect(path or DB_PATH) as conn:
        r = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(r) if r else None


def find_user(username: str, path=None) -> dict | None:
    ensure_tables(path)
    with db._connect(path or DB_PATH) as conn:
        r = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    return dict(r) if r else None


def list_users(path=None) -> list[dict]:
    ensure_tables(path)
    with db._connect(path or DB_PATH) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY role, username")]


_SLUG = re.compile(r"[^a-z0-9]+")


def create_user(username: str, password: str, role: str = "reader", path=None) -> dict:
    """The first admin made claims the owner's reading state ('me'); everyone
    after gets a fresh id. Usernames are case-insensitive and unique."""
    ensure_tables(path)
    username = username.strip()
    if not username or role not in ("admin", "reader") or len(password) < MIN_PW[role]:
        raise ValueError(f"a username, a password of {MIN_PW[role]}+ characters, and role admin or reader")
    if find_user(username, path):
        raise ValueError(f"{username} already exists")
    owner_free = not any(u["id"] == OWNER for u in list_users(path))
    uid = OWNER if role == "admin" and owner_free else f"u-{_SLUG.sub('-', username.lower()).strip('-')[:24]}-{secrets.token_hex(3)}"
    with db._connect(path or DB_PATH) as conn:
        conn.execute("INSERT INTO users (id, username, role, pw_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                     (uid, username, role, hash_password(password), _now()))
    return get_user(uid, path)


def set_password(user_id: str, password: str, path=None) -> None:
    with db._connect(path or DB_PATH) as conn:
        r = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    need = MIN_PW.get(r[0] if r else "reader", 6)
    if len(password) < need:
        raise ValueError(f"{need}+ characters")
    with db._connect(path or DB_PATH) as conn:
        conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(password), user_id))
    revoke_all(user_id, path)                       # a new password signs every device out


def issue_token(user_id: str, name: str | None = None, path=None) -> str:
    ensure_tables(path)
    token = secrets.token_urlsafe(32)
    with db._connect(path or DB_PATH) as conn:
        conn.execute("INSERT INTO auth_tokens (user_id, hash, name, created_at) VALUES (?, ?, ?, ?)",
                     (user_id, _token_hash(token), (name or "")[:80], _now()))
    return token


def revoke_token(token: str, path=None) -> None:
    ensure_tables(path)
    with db._connect(path or DB_PATH) as conn:
        conn.execute("DELETE FROM auth_tokens WHERE hash = ?", (_token_hash(token),))
    _seen.clear()


def revoke_all(user_id: str, path=None) -> None:
    ensure_tables(path)
    with db._connect(path or DB_PATH) as conn:
        conn.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))
    _seen.clear()
    _basic_seen.clear()              # an OPDS app on the old password stops at once, not in a minute


# A token is checked against the DB on EVERY request — an indexed read on a kept-open
# connection, cheap — so a revoke from anywhere (the CLI is another process) bites at
# once. What's remembered for a minute is only the last-used stamp: a reader turning
# pages shouldn't write to the DB once a second.
_seen: dict[str, float] = {}
_seen_lock = threading.Lock()
_TOKEN_TTL = 60
# token_id -> last-used stamp, written in ONE transaction a minute by the flusher.
# The write used to happen inside the request, on the event loop: when a background
# job held SQLite's writer (a shelf scan, a sync), every request in the app waited.
_last_used: dict[int, str] = {}
_flusher_started = False


def flush_last_used(path=None) -> int:
    with _seen_lock:
        pending = dict(_last_used)
        _last_used.clear()
    if not pending:
        return 0
    with db._connect(path or DB_PATH) as conn:
        conn.executemany("UPDATE auth_tokens SET last_used = ? WHERE id = ?", [(ts, tid) for tid, ts in pending.items()])
    return len(pending)


def _ensure_flusher() -> None:
    global _flusher_started
    if _flusher_started:
        return
    _flusher_started = True

    def loop():
        while True:
            time.sleep(_TOKEN_TTL)
            try:
                flush_last_used()
            except Exception as e:
                logger.info(f"auth: last_used flush failed: {e}")
    threading.Thread(target=loop, daemon=True, name="auth-last-used").start()


def user_for_token(token: str, path=None) -> dict | None:
    if not token:
        return None
    h = _token_hash(token)
    ensure_tables(path)
    with db._connect(path or DB_PATH) as conn:
        r = conn.execute("""SELECT u.*, t.id AS token_id FROM auth_tokens t JOIN users u ON u.id = t.user_id
                            WHERE t.hash = ? AND u.disabled = 0""", (h,)).fetchone()
        if not r:
            return None
        now = time.monotonic()
        with _seen_lock:
            stale = now - _seen.get(h, 0) >= _TOKEN_TTL
            if stale:
                _seen[h] = now
        if stale:
            with _seen_lock:
                _last_used[r["token_id"]] = _now()
            _ensure_flusher()
    return dict(r)


def user_for_password(username: str, password: str, path=None) -> dict | None:
    u = find_user(username, path)
    if u and not u["disabled"] and check_password(password, u["pw_hash"]):
        return u
    return None


# Basic auth: OPDS apps send it on every request, and scrypt is ~50 ms by design.
# Remember a good (user, password) pair for a minute, keyed by its hash.
_basic_seen: dict[str, tuple[float, dict]] = {}


def _user_for_basic(header: str, path=None) -> dict | None:
    try:
        username, _, password = base64.b64decode(header).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    k = hashlib.sha256(header.encode()).hexdigest()
    hit = _basic_seen.get(k)
    if hit and time.monotonic() - hit[0] < _TOKEN_TTL:
        return hit[1]
    u = user_for_password(username, password, path)
    if u:
        _basic_seen[k] = (time.monotonic(), u)
    return u


_ENABLED_TTL = 2.0
_enabled_cache = {"ts": 0.0, "val": False}


def enabled(path=None) -> bool:
    """Read from the DB, NOT the config cache: the switch is flipped by the CLI —
    another process — and the app's cached config never heard about it. 2026-10-14:
    `enable on` printed 1 and every anonymous request still walked in. Remembered
    for two seconds only, so the gate isn't a DB read on every cached page."""
    now = time.monotonic()
    if path is None and now - _enabled_cache["ts"] < _ENABLED_TTL:
        return _enabled_cache["val"]
    with db._connect(path or DB_PATH) as conn:
        r = conn.execute("SELECT value FROM config WHERE key = 'auth_enabled'").fetchone()
    val = bool(r) and r[0] == "1"
    if path is None:
        _enabled_cache.update(ts=now, val=val)
    return val


def forget_enabled() -> None:
    _enabled_cache["ts"] = 0.0


# --- who may call what ------------------------------------------------------------------------------
# A reader reads, and keeps their own state. Anything not listed is admin only, so a
# new route is locked until someone decides it isn't.
_R = r"[^/]+"
READER_GET = [re.compile(p) for p in (
    r"/api/version", r"/api/auth/me", r"/api/me/.*", r"/api/changes",
    r"/api/series", rf"/api/series/{_R}",
    rf"/api/series/{_R}/(status|thumbnail|related|top-rated|trades|arcs)",
    rf"/api/series/{_R}/issues/{_R}/(book|thumbnail|variants|locg-details)",
    r"/api/shelf", rf"/api/shelf/{_R}", rf"/api/shelf/{_R}/cover",
    rf"/api/books/{_R}", rf"/api/books/{_R}/(cover|pages/{_R}|next|community)",
    r"/api/cover-store/.*", r"/api/cover-upgrade/.*", r"/api/img",
    rf"/api/creators/{_R}", r"/api/discover/.*", r"/api/ondeck(/.*)?", r"/api/trending",
    r"/api/pull-list", r"/api/readlists", rf"/api/readlists/{_R}", rf"/api/readlists/{_R}/next",
    rf"/api/readlists/{_R}/items/{_R}/cover", r"/api/listcatalog", r"/api/search", r"/api/search/people",
    r"/api/wantread(/check)?", r"/api/push/status", rf"/api/trade/{_R}/details", r"/opds(/.*)?",
)]
READER_WRITE = [re.compile(p) for p in (
    rf"PUT /api/books/{_R}/(progress|mark)", rf"POST /api/books/{_R}/dismiss", rf"PUT /api/series/{_R}/mark",
    r"POST /api/wantread", rf"DELETE /api/wantread/{_R}", r"POST /api/push/(devices(/forget)?|test)",
    r"(PUT|PATCH|POST|DELETE) /api/me/.*", r"POST /api/auth/logout",
    # reading lists are per reader (2026-10-26): their own to add and remove — the routes check whose.
    # Share (PATCH) and Get stay the owner's
    r"POST /api/readlists/import(-url)?", r"POST /api/listcatalog/import", rf"POST /api/readlists/{_R}/covers",
    rf"DELETE /api/readlists/{_R}", rf"DELETE /api/readlists/{_R}/items/{_R}",
)]
OPEN = [re.compile(p) for p in (r"GET /api/version", r"POST /api/auth/login", r"GET /login")]


def reader_may(method: str, path: str) -> bool:
    if method in ("GET", "HEAD"):
        return any(p.fullmatch(path) for p in READER_GET)
    return any(p.fullmatch(f"{method} {path}") for p in READER_WRITE)


def via_tunnel(scope) -> bool:
    """Came in through Cloudflare (the public address). Cloudflare stamps every request
    with Cf-Connecting-IP and a client can't strip it; home and tailnet never carry it."""
    return any(k.lower() == b"cf-connecting-ip" for k, _ in scope.get("headers", []))


def _app_path(path: str) -> bool:
    """What the public address is for: YondeB and OPDS readers."""
    return any(path == p or path.startswith(p + "/") for p in ("/api", "/opds"))


def _is_open(method: str, path: str) -> bool:
    return any(p.fullmatch(f"{'GET' if method == 'HEAD' else method} {path}") for p in OPEN)


class AuthGate:
    """Plain ASGI: work out who's asking, set them for the request, and turn
    away what they may not do. Off → everyone is the admin, as before."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if via_tunnel(scope):
            # The public address carries the apps and nothing else. Wave the whole internet at
            # the web app? No. It gets a green コメタ and a closed door; the web app is home or
            # tailnet only. And with login off, the tunnel carries nothing at all.
            path = scope.get("path", "")
            if not await self._enabled():
                return await self.plain(send, 503, FRONT_PAGE)
            if not _app_path(path):
                return await self.plain(send, 200 if path in ("/", "/index.html") else 404, FRONT_PAGE)
        if not await self._enabled():
            tok = _CURRENT.set(None)                       # the owner, admin — the old world
            try:
                return await self.app(scope, receive, send)
            finally:
                _CURRENT.reset(tok)
        method, path = scope["method"], scope.get("path", "")
        user = await run_in_threadpool(self.identify, scope)    # a DB read: never on the event loop
        tok = _CURRENT.set(user or ANONYMOUS)                  # never the admin by default once this is on
        try:
            if user is None and not _is_open(method, path):
                return await self.refuse(scope, send, 401)
            if user and user["role"] != "admin" and not _is_open(method, path) and not reader_may(method, path):
                return await self.refuse(scope, send, 403)
            return await self.app(scope, receive, send)
        finally:
            _CURRENT.reset(tok)

    @staticmethod
    async def _enabled() -> bool:
        if time.monotonic() - _enabled_cache["ts"] < _ENABLED_TTL:
            return _enabled_cache["val"]
        return await run_in_threadpool(enabled)

    @staticmethod
    def identify(scope) -> dict | None:
        headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        a = headers.get("authorization", "")
        if a[:7].lower() == "bearer ":
            return user_for_token(a[7:].strip())
        if a[:6].lower() == "basic ":
            return _user_for_basic(a[6:].strip())
        c = SimpleCookie()
        try:
            c.load(headers.get("cookie", ""))
        except Exception:
            return None
        return user_for_token(c[COOKIE].value) if COOKIE in c else None

    @staticmethod
    async def plain(send, code: int, html: str):
        body = html.encode()
        await send({"type": "http.response.start", "status": code,
                    "headers": [(b"content-type", b"text/html; charset=utf-8"),
                                (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def refuse(scope, send, code: int):
        path = scope.get("path", "")
        if path.startswith("/opds"):
            body, ctype = b"Sign in to this Kometa", b"text/plain; charset=utf-8"
            extra = [(b"www-authenticate", b'Basic realm="Kometa", charset="UTF-8"')] if code == 401 else []
        elif path.startswith("/api/"):
            body = json.dumps({"detail": "Sign in" if code == 401 else "Not for this account"}).encode()
            ctype, extra = b"application/json", []
        else:                                                  # the web app: sign in, or a plain no
            body = (LOGIN_PAGE if code == 401 else NOT_ADMIN_PAGE).encode()
            ctype, extra, code = b"text/html; charset=utf-8", [], 200 if code == 401 and path in ("/", "/index.html") else code
        await send({"type": "http.response.start", "status": code,
                    "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store"), *extra]})
        await send({"type": "http.response.body", "body": body})


# --- routes ----------------------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str
    device: str | None = None


@router.post("/api/auth/login")
async def login(req: LoginRequest, request: Request):
    ip = client_ip(request)
    wait = locked_for(req.username, ip)
    if wait:
        raise HTTPException(429, "Too many tries — wait a bit", headers={"Retry-After": str(wait)})
    u = await run_in_threadpool(user_for_password, req.username, req.password)   # scrypt, ~50 ms, off the loop
    if not u:
        note_failure(req.username, ip)
        await asyncio.sleep(FAIL_PAUSE)                        # a guesser waits; a person doesn't notice; no worker held
        raise HTTPException(401, "That username and password don't match")
    clear_failures(req.username, ip)
    token = issue_token(u["id"], req.device or request.headers.get("user-agent", "")[:80])
    resp = JSONResponse({"token": token, "user": _public(u)})
    resp.set_cookie(COOKIE, token, max_age=COOKIE_AGE, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https")
    return resp


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    a = request.headers.get("authorization", "")
    token = a[7:].strip() if a[:7].lower() == "bearer " else request.cookies.get(COOKIE, "")
    if token:
        revoke_token(token)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/api/auth/me")
def me():
    u = current_user()
    return {"id": u["id"], "username": u.get("username"), "role": u["role"], "auth_enabled": enabled()}


@router.get("/login")
def login_page():
    return HTMLResponse(LOGIN_PAGE, headers={"cache-control": "no-store"})


# preferences: one JSON object per person, merged on PATCH (null deletes a key)
def get_prefs(reader_id: str | None = None, path=None) -> dict:
    u = get_user(reader_id or current_reader(), path)
    try:
        return json.loads(u["prefs"]) if u else {}
    except ValueError:
        return {}


@router.get("/api/me/prefs")
def prefs_get():
    return get_prefs()


@router.patch("/api/me/prefs")
def prefs_patch(changes: dict):
    rid = current_reader()
    if not get_user(rid):
        raise HTTPException(409, "No account yet — preferences need one")
    p = get_prefs(rid)
    for k, v in changes.items():
        if v is None:
            p.pop(k, None)
        else:
            p[str(k)[:64]] = v
    with db._connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET prefs = ? WHERE id = ?", (json.dumps(p)[:20000], rid))
    return p


# a reader's own pull list: their notifications, On Deck and calendar — it never downloads.
# The admin's is tracked_series.on_pull_list, the one acquisition reads.
def pull_ids(reader_id: str | None = None, path=None) -> set[int]:
    rid = reader_id or current_reader()
    ensure_tables(path)
    with db._connect(path or DB_PATH) as conn:
        if rid == OWNER:
            return {r[0] for r in conn.execute("SELECT id FROM tracked_series WHERE on_pull_list = 1")}
        return {r[0] for r in conn.execute("SELECT series_id FROM reader_pull WHERE reader_id = ?", (rid,))}


@router.get("/api/me/pull-list")
def my_pull_list():
    return {"series": sorted(pull_ids()), "downloads": current_reader() == OWNER}


class PullToggle(BaseModel):
    on: bool


@router.put("/api/me/pull-list/{series_id}")
def my_pull_toggle(series_id: int, req: PullToggle):
    rid = current_reader()
    if rid == OWNER:                                    # the admin's pull list buys comics: the web app's switch
        raise HTTPException(409, "The admin's pull list is changed in the web app")
    ensure_tables()
    with db._connect(DB_PATH) as conn:
        if req.on:
            conn.execute("INSERT OR IGNORE INTO reader_pull (reader_id, series_id, added_at) VALUES (?, ?, ?)",
                         (rid, series_id, _now()))
        else:
            conn.execute("DELETE FROM reader_pull WHERE reader_id = ? AND series_id = ?", (rid, series_id))
    return {"series": sorted(pull_ids(rid))}


# admin: people
class NewUser(BaseModel):
    username: str
    password: str
    role: str = "reader"


class UserChange(BaseModel):
    role: str | None = None
    password: str | None = None
    disabled: bool | None = None


def reading_summary(reader_id: str, path=None) -> dict:
    """What a reader is up to, for the admin's Accounts list: the book they touched
    last (series, number, page of pages, finished?, when), how many they're mid-way
    through, how many they've finished, and whether a phone is registered for push.
    The owner's question was "what's Jane reading?" — this answers it without anyone
    opening the database."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        row = conn.execute("""
            SELECT COALESCE(ts.title, ss.title, b.path) AS series, b.number, p.page, b.page_count,
                   p.completed, p.updated_at
            FROM read_progress p JOIN books b ON b.id = p.book_id
            LEFT JOIN tracked_series ts ON ts.id = b.tracked_series_id
            LEFT JOIN shelf_series ss ON ss.id = b.shelf_series_id
            WHERE p.reader_id = ? ORDER BY p.updated_at DESC LIMIT 1""", (reader_id,)).fetchone()
        n = conn.execute("SELECT COUNT(*), COALESCE(SUM(completed), 0) FROM read_progress WHERE reader_id = ?",
                         (reader_id,)).fetchone()
        try:
            push = [r[0] for r in conn.execute(
                "SELECT DISTINCT platform FROM push_devices WHERE reader_id = ? AND dead_at IS NULL", (reader_id,))]
        except Exception:                              # push_devices only exists once a phone has registered
            push = []
    last = None
    if row:
        series = row["series"]
        if series and "/" in series:                   # a bare path: the folder is the nearest thing to a title
            series = series.rsplit("/", 2)[-2]
        last = {"series": series, "number": row["number"], "page": row["page"], "pages": row["page_count"],
                "finished": bool(row["completed"]), "at": row["updated_at"]}
    return {"last": last, "in_progress": int(n[0]) - int(n[1]), "finished": int(n[1]), "push": push}


@router.get("/api/users")
def users_list():
    out = []
    for u in list_users():
        with db._connect(DB_PATH) as conn:
            toks = [dict(r) for r in conn.execute(
                "SELECT id, name, created_at, last_used FROM auth_tokens WHERE user_id = ? ORDER BY last_used DESC", (u["id"],))]
        out.append({**_public(u), "created_at": u["created_at"], "tokens": toks, "reading": reading_summary(u["id"])})
    return out


@router.post("/api/users")
def users_add(req: NewUser):
    try:
        return _public(create_user(req.username, req.password, req.role))
    except ValueError as e:
        raise HTTPException(400, str(e))


def _admins_left_without(user_id: str) -> int:
    return sum(1 for u in list_users() if u["role"] == "admin" and not u["disabled"] and u["id"] != user_id)


@router.patch("/api/users/{user_id}")
def users_change(user_id: str, req: UserChange):
    u = get_user(user_id)
    if not u:
        raise HTTPException(404, "No such account")
    if (req.role == "reader" or req.disabled) and u["role"] == "admin" and not _admins_left_without(user_id):
        raise HTTPException(409, "That would leave no admin")
    with db._connect(DB_PATH) as conn:
        if req.role in ("admin", "reader"):
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (req.role, user_id))
        if req.disabled is not None:
            conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(req.disabled), user_id))
    if req.password:
        try:
            set_password(user_id, req.password)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if req.disabled:
        revoke_all(user_id)
    _seen.clear()
    _basic_seen.clear()
    return _public(get_user(user_id))


@router.delete("/api/users/{user_id}")
def users_delete(user_id: str):
    u = get_user(user_id)
    if not u:
        raise HTTPException(404, "No such account")
    if u["role"] == "admin" and not _admins_left_without(user_id):
        raise HTTPException(409, "That would leave no admin")
    revoke_all(user_id)
    with db._connect(DB_PATH) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    _basic_seen.clear()
    return {"ok": True}


@router.delete("/api/users/{user_id}/tokens/{token_id}")
def users_token_revoke(user_id: str, token_id: int):
    with db._connect(DB_PATH) as conn:
        conn.execute("DELETE FROM auth_tokens WHERE id = ? AND user_id = ?", (token_id, user_id))
    _seen.clear()
    return {"ok": True}


# --- pages -------------------------------------------------------------------------------------------
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><title>コメタ</title>
<style>
html,body{margin:0;height:100%;background:#000;color:#ddd;font:15px/1.4 "Victor Mono",ui-monospace,Menlo,monospace}
main{min-height:100%;display:grid;place-items:center;padding:24px;box-sizing:border-box}
form,div.box{width:min(320px,100%);display:grid;gap:12px}
h1{margin:0 0 8px;font-size:22px;color:#b0ff45;letter-spacing:.04em}
input{background:#111;border:1px solid #333;color:#eee;border-radius:4px;padding:10px 12px;font:inherit}
input:focus{outline:none;border-color:#b0ff45}
button{background:#b0ff45;color:#000;border:0;border-radius:4px;padding:10px 12px;font:inherit;font-weight:600}
p.err{color:#ff5a5a;margin:0;min-height:1.4em;font-size:13px}
</style></head><body><main>__BODY__</main></body></html>"""

LOGIN_PAGE = _PAGE.replace("__BODY__", """<form id="f">
<h1>コメタ</h1>
<input name="username" autocomplete="username" placeholder="username" autocapitalize="none" required>
<input name="password" type="password" autocomplete="current-password" placeholder="password" required>
<button>Sign in</button><p class="err" id="e"></p></form>
<script>
document.getElementById('f').onsubmit=async ev=>{ev.preventDefault();const d=new FormData(ev.target);
const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({username:d.get('username'),password:d.get('password'),device:'web'})});
if(r.ok){location.replace('/')}else{document.getElementById('e').textContent=(await r.json()).detail||'No'}};
</script>""")

# the public address, anywhere but /api and /opds: the name, nowhere to go
FRONT_PAGE = _PAGE.replace("__BODY__", """<h1 style="font-size:40px;margin:0">コメタ</h1>""")

NOT_ADMIN_PAGE = _PAGE.replace("__BODY__", """<div class="box"><h1>コメタ</h1>
<p>This account reads in YondeB or an OPDS app.</p>
<button onclick="fetch('/api/auth/logout',{method:'POST'}).then(()=>location.replace('/'))">Sign out</button></div>""")


# --- CLI: the way back in that doesn't need a login ----------------------------------------------
def _cli(argv=None):
    ap = argparse.ArgumentParser(prog="python -m kometa.auth")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("username")
    a.add_argument("--admin", action="store_true")
    p = sub.add_parser("password")
    p.add_argument("username")
    t = sub.add_parser("token")
    t.add_argument("username")
    t.add_argument("name")
    r = sub.add_parser("revoke-all")
    r.add_argument("username")
    s = sub.add_parser("enable")
    s.add_argument("state", choices=["on", "off"])
    args = ap.parse_args(argv)
    ensure_tables()
    if args.cmd == "list":
        for u in list_users():
            print(f"{u['username']:<20} {u['role']:<7} {u['id']}{'  (disabled)' if u['disabled'] else ''}")
        print(f"auth_enabled = {db.get_config(DB_PATH).get('auth_enabled', '0')}")
        return
    if args.cmd == "enable":
        if args.state == "on" and not any(u["role"] == "admin" and not u["disabled"] for u in list_users()):
            sys.exit("No admin account yet: `add <username> --admin` first")
        db.set_config({"auth_enabled": "1" if args.state == "on" else "0"}, DB_PATH)
        print(f"auth_enabled = {'1' if args.state == 'on' else '0'}")
        return
    if args.cmd == "add":
        pw = getpass.getpass("Password: ")
        u = create_user(args.username, pw, "admin" if args.admin else "reader")
        print(f"{u['username']} ({u['role']}) = {u['id']}")
        return
    u = find_user(args.username)
    if not u:
        sys.exit(f"No account {args.username}")
    if args.cmd == "password":
        set_password(u["id"], getpass.getpass("New password: "))
        print("Password set; every device signed out")
    elif args.cmd == "token":
        print(issue_token(u["id"], args.name))
    elif args.cmd == "revoke-all":
        revoke_all(u["id"])
        print("Every device signed out")


if __name__ == "__main__":
    _cli()
