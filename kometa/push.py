"""Push to YondeB: 'it landed' on your phone the moment a download finishes.

APNs, token auth: one .p8 key (Team-scoped, sandbox + production) signs a short
JWT, curl_cffi speaks the HTTP/2 Apple insists on. Devices register themselves
(YondeB POSTs its token every launch). Pull-list series only (push_scope), and
batched: issues landing within BATCH_S of each other go out as ONE notification,
not a machine-gun of buzzes while a week's pull list drains.

The key lives beside the DB (data/apns/), never in the repo; its id and the team
id are config (apns_key_id, apns_team_id).
"""
import base64
import json
import logging
import os
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import kometa.db as db

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
BATCH_S = 60                        # quiet this long after the last landing, then send
JWT_TTL_S = 40 * 60                 # Apple wants it 20–60 min old at most; refresh at 40
HOSTS = {"sandbox": "https://api.sandbox.push.apple.com", "production": "https://api.push.apple.com"}
TOPIC = "dev.kometa.yondeb"

_lock = threading.Lock()
_pending: list[dict] = []
_timer: threading.Timer | None = None
_jwt: dict = {"token": None, "at": 0.0}


def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS push_devices (
            token TEXT PRIMARY KEY, env TEXT NOT NULL DEFAULT 'sandbox', name TEXT, app TEXT,
            seen_at TEXT DEFAULT (datetime('now')), dead_at TEXT)""")


def _key_path(path=None) -> str:
    return os.path.join(os.path.dirname(path or DB_PATH) or ".", "apns", "AuthKey.p8")


def configured(path=None) -> bool:
    cfg = db.get_config(path or DB_PATH)
    return bool(cfg.get("apns_key_id") and cfg.get("apns_team_id") and os.path.exists(_key_path(path)))


# --- the JWT: ES256 over header.claims, raw r||s, base64url --------------------------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _make_jwt(key_pem: bytes, key_id: str, team_id: str, now: float) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    head = _b64(json.dumps({"alg": "ES256", "kid": key_id}, separators=(",", ":")).encode())
    body = _b64(json.dumps({"iss": team_id, "iat": int(now)}, separators=(",", ":")).encode())
    key = serialization.load_pem_private_key(key_pem, password=None)
    der = key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)                      # JWS wants the raw 64 bytes, not DER
    return f"{head}.{body}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def _bearer(path=None) -> str:
    now = time.time()
    if _jwt["token"] and now - _jwt["at"] < JWT_TTL_S:
        return _jwt["token"]
    cfg = db.get_config(path or DB_PATH)
    with open(_key_path(path), "rb") as fh:
        tok = _make_jwt(fh.read(), cfg["apns_key_id"], cfg["apns_team_id"], now)
    _jwt.update(token=tok, at=now)
    return tok


# --- sending ------------------------------------------------------------------------------
def _post(url: str, headers: dict, body: bytes):
    from curl_cffi import CurlHttpVersion
    from curl_cffi import requests as cr
    return cr.post(url, headers=headers, data=body, timeout=15, http_version=CurlHttpVersion.V2_0)


def send(payload: dict, path=None, post=None) -> dict:
    """One payload to every live device. Returns {sent, failed, dead}. A token Apple
    calls Unregistered / BadDeviceToken is marked dead and never tried again."""
    path = path or DB_PATH
    ensure_tables(path)
    if not configured(path):
        return {"sent": 0, "failed": 0, "dead": 0, "error": "not configured"}
    post = post or _post
    with db._connect(path) as conn:
        devices = [dict(r) for r in conn.execute("SELECT token, env FROM push_devices WHERE dead_at IS NULL")]
    body = json.dumps(payload, separators=(",", ":")).encode()
    out = {"sent": 0, "failed": 0, "dead": 0}
    for d in devices:
        headers = {"authorization": f"bearer {_bearer(path)}", "apns-topic": TOPIC, "apns-push-type": "alert",
                   "apns-priority": "10", "content-type": "application/json"}
        try:
            r = post(f"{HOSTS.get(d['env'], HOSTS['sandbox'])}/3/device/{d['token']}", headers, body)
        except Exception as e:
            logger.warning(f"Push: {d['token'][:8]}… didn't go: {e}")
            out["failed"] += 1
            continue
        if r.status_code == 200:
            out["sent"] += 1
            continue
        reason = ""
        try:
            reason = (r.json() or {}).get("reason", "")
        except Exception:
            pass
        if r.status_code == 410 or reason in ("Unregistered", "BadDeviceToken", "DeviceTokenNotForTopic"):
            with db._connect(path) as conn:
                conn.execute("UPDATE push_devices SET dead_at = datetime('now') WHERE token = ?", (d["token"],))
            out["dead"] += 1
        else:
            if reason in ("ExpiredProviderToken", "InvalidProviderToken"):
                _jwt.update(token=None, at=0.0)
            out["failed"] += 1
        logger.info(f"Push: {d['token'][:8]}… answered {r.status_code} {reason}")
    return out


# --- what landed -----------------------------------------------------------------------
def issue_landed(series_id: int, number, path=None):
    """Called once per finished download (db.complete_download). Pull-list series
    only; batched — the send waits for BATCH_S of quiet."""
    global _timer
    path = path or DB_PATH
    try:
        cfg = db.get_config(path)
        if cfg.get("push_enabled", "1") == "0" or not configured(path):
            return
        with db._connect(path) as conn:
            s = conn.execute("SELECT title, on_pull_list FROM tracked_series WHERE id = ?", (series_id,)).fetchone()
        if not s or ((cfg.get("push_scope") or "pull") == "pull" and not s["on_pull_list"]):
            return
        with _lock:
            _pending.append({"series_id": series_id, "number": number, "title": s["title"]})
            if _timer:
                _timer.cancel()
            _timer = threading.Timer(BATCH_S, flush, kwargs={"path": path})
            _timer.daemon = True
            _timer.start()
    except Exception as e:
        logger.warning(f"Push: couldn't note a landing: {e}")


def _label(it: dict) -> str:
    n = it.get("number")
    return f"{it['title']} #{n:g}" if isinstance(n, (int, float)) else it["title"]


def _book_id(series_id: int, number) -> int | None:
    if number is None:
        return None
    try:
        from kometa import reader
        b = reader._known_book(series_id, number)
        if not b:
            b = reader._issue_book_on_share(series_id, number)      # the new file: index it now, open is instant
        return b and b.get("id")
    except Exception:
        return None


def build_payload(items: list[dict]) -> dict:
    first = items[0]
    if len(items) == 1:
        alert = {"title": "New on the shelf", "body": _label(first)}
    else:
        alert = {"title": f"{len(items)} new on the shelf",
                 "body": ", ".join(_label(i) for i in items[:4]) + ("…" if len(items) > 4 else "")}
    p = {"aps": {"alert": alert, "sound": "default", "thread-id": "kometa-new"}, "kind": "new",
         "series_id": first["series_id"]}
    if first.get("book_id"):
        p["book_id"] = first["book_id"]
    return p


def flush(path=None, post=None) -> dict | None:
    global _timer
    with _lock:
        items = list(_pending)
        _pending.clear()
        _timer = None
    if not items:
        return None
    seen, uniq = set(), []
    for it in items:
        k = (it["series_id"], it["number"])
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    uniq[0]["book_id"] = _book_id(uniq[0]["series_id"], uniq[0]["number"])     # the tap opens the first one
    res = send(build_payload(uniq), path, post)
    logger.info(f"Push: {len(uniq)} landed → {res}")
    return res


# --- routes -------------------------------------------------------------------------------
class DeviceRequest(BaseModel):
    token: str
    env: str = "sandbox"
    name: str | None = None
    app: str | None = None


@router.post("/api/push/devices")
def register_device(req: DeviceRequest):
    tok = req.token.strip().lower()
    if not tok or len(tok) > 200 or any(c not in "0123456789abcdef" for c in tok):
        raise HTTPException(422, "token must be the device token in hex")
    env = req.env if req.env in HOSTS else "sandbox"
    ensure_tables()
    with db._connect(DB_PATH) as conn:
        conn.execute("""INSERT INTO push_devices (token, env, name, app, seen_at, dead_at)
            VALUES (?, ?, ?, ?, datetime('now'), NULL)
            ON CONFLICT(token) DO UPDATE SET env = excluded.env, name = excluded.name, app = excluded.app,
                seen_at = excluded.seen_at, dead_at = NULL""", (tok, env, req.name, req.app))
    return {"ok": True}


@router.get("/api/push/status")
def push_status():
    ensure_tables()
    with db._connect(DB_PATH) as conn:
        devs = [dict(r) for r in conn.execute(
            "SELECT substr(token, 1, 8) AS token, env, name, seen_at, dead_at FROM push_devices ORDER BY seen_at DESC")]
    cfg = db.get_config(DB_PATH)
    return {"configured": configured(), "scope": cfg.get("push_scope") or "pull",
            "enabled": cfg.get("push_enabled", "1") != "0", "devices": devs}


@router.post("/api/push/test")
def push_test():
    if not configured():
        raise HTTPException(409, "Push isn't set up: key or ids missing")
    return send({"aps": {"alert": {"title": "Kometa", "body": "Test from Kometa — push works"}, "sound": "default"},
                 "kind": "test"})
