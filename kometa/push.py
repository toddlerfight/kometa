"""Push to YondeB: 'it landed' on your phone the moment a download finishes.

APNs, token auth: one .p8 key (Team-scoped, sandbox + production) signs a short
JWT, curl_cffi speaks the HTTP/2 Apple insists on. Devices register themselves
(YondeB POSTs its token every launch). Pull-list series only (push_scope), and
batched: issues landing within BATCH_S of each other go out as ONE notification,
not a machine-gun of buzzes while a week's pull list drains.

The key lives beside the DB (data/apns/), never in the repo; its id and the team
id are config (apns_key_id, apns_team_id).

Android (2026-10-15): the same landings go out over Firebase Cloud Messaging, HTTP v1.
A Google service-account JSON beside the APNs key (data/fcm/service-account.json)
signs an RS256 JWT that Google swaps for a one-hour OAuth2 access token; one FCM
message per device token. A device says which it is at registration (platform
'apns' or 'fcm'); the alert text and the deep-link data keys are built once and
dressed for whichever platform the device speaks. Either platform may be set up
alone — devices of the other kind are simply skipped.
"""
import base64
import json
import logging
import os
import random
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import kometa.db as db
from kometa.auth import current_reader, is_admin

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
BATCH_S = 60                        # quiet this long after the last landing, then send
HOLD_MAX_S = 30 * 60                # a series mid-pull holds its landings this long at most
ACTIVE_STATES = ("queued", "searching", "downloading", "processing")
MAX_BATCH_IDS = 20                  # book ids a batch carries (APNs payloads cap at 4 KB)
JWT_TTL_S = 40 * 60                 # Apple wants it 20–60 min old at most; refresh at 40
HOSTS = {"sandbox": "https://api.sandbox.push.apple.com", "production": "https://api.push.apple.com"}
TOPIC = "dev.kometa.yondeb"
PLATFORMS = ("apns", "fcm")
FCM_SEND = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_TOKEN_TTL_S = 60 * 60           # Google's access tokens last an hour; we ask for exactly that
FCM_TOKEN_SLACK_S = 5 * 60          # ...and stop trusting ours five minutes early
FCM_CHANNEL = "kometa-new"
FCM_TOKEN_MAX = 4096                # FCM registration tokens are ~160 chars today; this is a sanity cap, not a spec

_lock = threading.Lock()
_pending: list[dict] = []
_timer: threading.Timer | None = None
_jwt: dict = {"token": None, "at": 0.0}
_fcm: dict = {"token": None, "exp": 0.0}          # the OAuth2 access token, cached until ~5 min before it dies


def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS push_devices (
            token TEXT PRIMARY KEY, env TEXT NOT NULL DEFAULT 'sandbox', name TEXT, app TEXT,
            seen_at TEXT DEFAULT (datetime('now')), dead_at TEXT)""")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(push_devices)")}
        if "reader_id" not in cols:                     # 2026-10-14: a device belongs to someone
            conn.execute("ALTER TABLE push_devices ADD COLUMN reader_id TEXT NOT NULL DEFAULT 'me'")
        if "platform" not in cols:                      # 2026-10-15: and speaks APNs or FCM
            conn.execute("ALTER TABLE push_devices ADD COLUMN platform TEXT NOT NULL DEFAULT 'apns'")


def _key_path(path=None) -> str:
    return os.path.join(os.path.dirname(path or DB_PATH) or ".", "apns", "AuthKey.p8")


def _sa_path(path=None) -> str:
    """The Google service-account JSON: data/fcm/service-account.json, beside the APNs key."""
    return os.path.join(os.path.dirname(path or DB_PATH) or ".", "fcm", "service-account.json")


def platforms(path=None) -> dict:
    """Which platforms can actually be sent to right now. APNs wants the .p8 plus its
    key/team ids in config; FCM wants only the service-account file (the project id
    is inside it)."""
    cfg = db.get_config(path or DB_PATH)
    return {"apns": bool(cfg.get("apns_key_id") and cfg.get("apns_team_id") and os.path.exists(_key_path(path))),
            "fcm": os.path.exists(_sa_path(path))}


def configured(path=None) -> bool:
    return any(platforms(path).values())


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


# --- FCM: a service-account RS256 JWT, swapped at Google for an hour's access token ------
def _service_account(path=None) -> dict:
    with open(_sa_path(path), "rb") as fh:
        sa = json.load(fh)
    for k in ("client_email", "private_key", "project_id"):
        if not sa.get(k):
            raise ValueError(f"service-account.json is missing {k}")
    return sa


def _make_sa_jwt(sa: dict, now: float) -> str:
    """RS256 over header.claims — PKCS#1 v1.5, which is already the JWS byte shape (no
    DER-to-raw dance like ES256 needs)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iss": sa["client_email"], "scope": FCM_SCOPE, "aud": sa.get("token_uri") or FCM_TOKEN_URL,
              "iat": int(now), "exp": int(now) + FCM_TOKEN_TTL_S}
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(sig)}"


def _fcm_bearer(path=None, post=None) -> str:
    """The OAuth2 access token, fetched through the same post seam the sends use (so the
    tests never touch Google) and kept until FCM_TOKEN_SLACK_S before it expires."""
    now = time.time()
    if _fcm["token"] and now < _fcm["exp"] - FCM_TOKEN_SLACK_S:
        return _fcm["token"]
    sa = _service_account(path)
    from urllib.parse import urlencode
    body = urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                      "assertion": _make_sa_jwt(sa, now)}).encode()
    r = (post or _post)(sa.get("token_uri") or FCM_TOKEN_URL,
                        {"content-type": "application/x-www-form-urlencoded"}, body)
    if r.status_code != 200:
        raise RuntimeError(f"FCM token exchange answered {r.status_code}")
    tok = r.json()
    _fcm.update(token=tok["access_token"], exp=now + float(tok.get("expires_in") or FCM_TOKEN_TTL_S))
    return _fcm["token"]


# --- sending ------------------------------------------------------------------------------
def _post(url: str, headers: dict, body: bytes):
    from curl_cffi import CurlHttpVersion
    from curl_cffi import requests as cr
    return cr.post(url, headers=headers, data=body, timeout=15, http_version=CurlHttpVersion.V2_0)


def _bury(token: str, path) -> None:
    with db._connect(path) as conn:
        conn.execute("UPDATE push_devices SET dead_at = datetime('now') WHERE token = ?", (token,))


def send(payload: dict, path=None, post=None, reader_id: str | None = None) -> dict:
    """One payload to every live device — or only one person's. Returns {sent, failed,
    dead}. `payload` is the APNs shape build_payload makes; an FCM device gets the same
    alert and data keys re-dressed by fcm_message. A token the platform disowns (Apple:
    Unregistered / BadDeviceToken; Google: UNREGISTERED / a bad registration token) is
    marked dead and never tried again. Devices of a platform that isn't set up are
    skipped, not failed."""
    path = path or DB_PATH
    ensure_tables(path)
    up = platforms(path)
    if not any(up.values()):
        return {"sent": 0, "failed": 0, "dead": 0, "error": "not configured"}
    post = post or _post
    with db._connect(path) as conn:
        devices = [dict(r) for r in conn.execute(
            "SELECT token, env, platform FROM push_devices WHERE dead_at IS NULL AND (? IS NULL OR reader_id = ?)",
            (reader_id, reader_id))]
    out = {"sent": 0, "failed": 0, "dead": 0}
    apns = [d for d in devices if d["platform"] == "apns"]
    fcm = [d for d in devices if d["platform"] == "fcm"]
    if apns and up["apns"]:
        _send_apns(apns, payload, path, post, out)
    if fcm and up["fcm"]:
        _send_fcm(fcm, payload, path, post, out)
    return out


def _send_apns(devices: list[dict], payload: dict, path, post, out: dict) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
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
            _bury(d["token"], path)
            out["dead"] += 1
        else:
            if reason in ("ExpiredProviderToken", "InvalidProviderToken"):
                _jwt.update(token=None, at=0.0)
            out["failed"] += 1
        logger.info(f"Push: {d['token'][:8]}… answered {r.status_code} {reason}")


def _send_fcm(devices: list[dict], payload: dict, path, post, out: dict) -> None:
    try:
        project = _service_account(path)["project_id"]
        bearer = _fcm_bearer(path, post)
    except Exception as e:                               # no token = nobody on Android hears a thing; say so once
        logger.warning(f"Push: FCM auth failed, {len(devices)} device(s) skipped: {e}")
        out["failed"] += len(devices)
        return
    url = FCM_SEND.format(project=project)
    for d in devices:
        headers = {"authorization": f"Bearer {bearer}", "content-type": "application/json"}
        body = json.dumps(fcm_message(d["token"], payload), separators=(",", ":")).encode()
        try:
            r = post(url, headers, body)
        except Exception as e:
            logger.warning(f"Push: {d['token'][:8]}… didn't go: {e}")
            out["failed"] += 1
            continue
        if r.status_code == 200:
            out["sent"] += 1
            continue
        status, message, codes = "", "", set()
        try:
            err = (r.json() or {}).get("error") or {}
            status, message = err.get("status", ""), err.get("message", "")
            codes = {x.get("errorCode") for x in err.get("details") or [] if isinstance(x, dict)}
        except Exception:
            pass
        # 404 UNREGISTERED = the app was uninstalled or its token rotated. 400 INVALID_ARGUMENT
        # is also what a malformed MESSAGE gets, so only the token flavour buries the device.
        bad_token = r.status_code == 400 and "registration token" in message.lower()
        if r.status_code == 404 or "UNREGISTERED" in codes or bad_token:
            _bury(d["token"], path)
            out["dead"] += 1
        else:
            if r.status_code == 401:
                _fcm.update(token=None, exp=0.0)
            out["failed"] += 1
        logger.info(f"Push: {d['token'][:8]}… answered {r.status_code} {status} {message[:80]}")


# --- what landed -----------------------------------------------------------------------
def issue_landed(series_id: int, number, path=None):
    """Called once per finished download (db.complete_download). Batched — the send
    waits for BATCH_S of quiet — and sorted out per person at the send: who cares
    about this series is a question for each reader, not for the landing."""
    global _timer
    path = path or DB_PATH
    try:
        cfg = db.get_config(path)
        if cfg.get("push_enabled", "1") == "0" or not configured(path):
            return
        with db._connect(path) as conn:
            s = conn.execute("SELECT title FROM tracked_series WHERE id = ?", (series_id,)).fetchone()
        if not s:
            return
        with _lock:
            _pending.append({"series_id": series_id, "number": number, "title": s["title"], "at": time.time()})
            _save_pending(path)
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


# The headline rotates — same news, never the same words twice running (the owner,
# 2026-10-24: "mix it up"). Each has a one-comic form and a batch form.
LANDED = (("Just landed", "{n} just landed"), ("In hand", "{n} in hand"), ("Fresh in", "{n} fresh in"),
          ("Pulled", "{n} pulled"), ("Just in", "{n} just in"), ("Just dropped", "{n} just dropped"),
          ("Off the rack", "{n} off the rack"), ("Off the press", "{n} off the press"))
_last_landed = None


def _landed_words() -> tuple[str, str]:
    global _last_landed
    _last_landed = random.choice([w for w in LANDED if w != _last_landed])
    return _last_landed


def build_alert(items: list[dict]) -> dict:
    """The words on the lock screen — the same on every platform."""
    one, many = _landed_words()
    if len(items) == 1:
        return {"title": one, "body": _label(items[0])}
    return {"title": many.format(n=len(items)),
            "body": ", ".join(_label(i) for i in items[:4]) + ("…" if len(items) > 4 else "")}


def build_data(items: list[dict]) -> dict:
    """The deep link the tap follows: kind + series_id always; one issue carries book_id
    (straight into the reader), a batch carries dest=recent + book_ids (On Deck's Recently
    added with these lit up — not the first one, arbitrarily)."""
    first = items[0]
    d = {"kind": "new", "series_id": first["series_id"]}
    if len(items) > 1:
        d["dest"] = "recent"
        d["book_ids"] = [i["book_id"] for i in items if i.get("book_id")]
    elif first.get("book_id"):
        d["book_id"] = first["book_id"]
    return d


def build_payload(items: list[dict]) -> dict:
    """The APNs payload: aps.alert + the data keys at the top level. This is the shape
    send() takes for every platform; fcm_message re-dresses it for Google."""
    return {"aps": {"alert": build_alert(items), "sound": "default", "thread-id": FCM_CHANNEL}, **build_data(items)}


def fcm_message(token: str, payload: dict) -> dict:
    """One FCM v1 message for one device, from the APNs-shaped payload. FCM data values
    must be strings, so ints are str()'d and book_ids rides as a JSON array in a string.
    The Android tag is per destination: a fresh landing for the same series replaces its
    stale notification instead of stacking, but issue A's buzz survives issue B's."""
    alert = (payload.get("aps") or {}).get("alert")
    notif = dict(alert) if isinstance(alert, dict) else {"body": str(alert or "")}
    data = {k: (json.dumps(v, separators=(",", ":")) if isinstance(v, (list, dict)) else str(v))
            for k, v in payload.items() if k != "aps"}
    tag = f"{FCM_CHANNEL}-{data['dest']}" if "dest" in data else f"{FCM_CHANNEL}-s{data.get('series_id', '')}"
    return {"message": {"token": token, "notification": notif, "data": data,
                        "android": {"priority": "high", "collapse_key": tag,
                                    "notification": {"channel_id": FCM_CHANNEL, "sound": "default", "tag": tag}}}}


def _held_back(items: list[dict], now: float, path) -> tuple[list[dict], list[dict]]:
    """Split landings into (held, ready): held = the series is still pulling and the
    oldest held landing is younger than HOLD_MAX_S."""
    active: dict[int, bool] = {}
    with db._connect(path) as conn:
        for sid in {it["series_id"] for it in items}:
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM download_queue WHERE tracked_series_id = ? AND state IN "
                                 f"({','.join('?' * len(ACTIVE_STATES))})", (sid, *ACTIVE_STATES)).fetchone()[0]
            except Exception:
                n = 0
            active[sid] = n > 0
    # The cap is per SERIES, not per landing: when a run's oldest held landing ages
    # out, the whole run goes together. Per landing, Puzzlebox #2 aged out alone at
    # 07:13 (2026-10-16) and buzzed "1 new" while #3, #4 and #12 stayed held — a
    # drip of singles, the exact thing the hold exists to prevent.
    oldest: dict[int, float] = {}
    for it in items:
        oldest[it["series_id"]] = min(oldest.get(it["series_id"], now), it.get("at", now))
    held, ready = [], []
    for it in items:
        sid = it["series_id"]
        if active.get(sid) and now - oldest[sid] < HOLD_MAX_S:
            held.append(it)
        else:
            ready.append(it)
    return held, ready


PENDING_KEY = "push_pending"       # the held landings, on disk: a deploy must not eat them


def _save_pending(path) -> None:
    """Called under _lock. Two deploys on 2026-10-16 each wiped a held Puzzlebox
    landing (#1, then #3/#4/#12) — the hold is minutes long and deploys are frequent."""
    try:
        db.set_config({PENDING_KEY: json.dumps(_pending)}, path)
    except Exception as e:
        logger.info(f"Push: couldn't save held landings: {e}")


def restore_pending(path=None) -> int:
    """Startup: reload landings a restart interrupted and give them a flush timer."""
    global _timer
    path = path or DB_PATH
    try:
        raw = db.get_config(path).get(PENDING_KEY) or "[]"
        items = [it for it in json.loads(raw) if isinstance(it, dict) and "series_id" in it]
    except Exception as e:
        logger.info(f"Push: couldn't restore held landings: {e}")
        return 0
    if not items:
        return 0
    with _lock:
        _pending[:0] = items
        if _timer:
            _timer.cancel()
        _timer = threading.Timer(BATCH_S, flush, kwargs={"path": path})
        _timer.daemon = True
        _timer.start()
    logger.info(f"Push: {len(items)} held landing(s) restored after a restart")
    return len(items)


def flush(path=None, post=None) -> dict | None:
    global _timer
    path = path or DB_PATH
    with _lock:
        items = list(_pending)
        _pending.clear()
        _save_pending(path)
        _timer = None
    if not items:
        return None
    # A series still pulling — more of its issues queued, searching or downloading — keeps
    # its landings back, so a run that drains over twenty minutes buzzes ONCE ("6 new on
    # the shelf") when the last one lands, like the Activity row that collapses them. Held
    # for HOLD_MAX_S at most: a stuck queue must not hold a notification hostage.
    now = time.time()
    held, items = _held_back(items, now, path)
    if held:
        # whatever is held gets a timer, whether or not something else goes out now —
        # held landings with no timer only ever flushed when the NEXT landing arrived
        with _lock:
            _pending[:0] = held
            _save_pending(path)
            if _timer:
                _timer.cancel()
            _timer = threading.Timer(BATCH_S, flush, kwargs={"path": path})
            _timer.daemon = True
            _timer.start()
    if not items:
        return None
    seen, uniq = set(), []
    for it in items:
        k = (it["series_id"], it["number"])
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        owners = [r[0] for r in conn.execute("SELECT DISTINCT reader_id FROM push_devices WHERE dead_at IS NULL")]
    total = {"sent": 0, "failed": 0, "dead": 0}
    for rid in owners:                                   # each person: their series, their devices, one buzz
        mine = [it for it in uniq if it["series_id"] in interested(rid, path)]
        if not mine:
            continue
        for it in mine[:MAX_BATCH_IDS]:                  # indexed now, so the tap (reader or row) finds them
            if "book_id" not in it:
                it["book_id"] = _book_id(it["series_id"], it["number"])
        res = send(build_payload(mine), path, post, reader_id=rid)
        for k in total:
            total[k] += res.get(k, 0)
    logger.info(f"Push: {len(uniq)} landed → {total}")
    return total


def interested(reader_id: str, path=None) -> set[int]:
    """The series a landing should buzz this person for. Everyone: what's on their Want to
    read (they asked for it — that's the whole point of asking). The admin: the pull list
    (push_scope 'pull', the default) or everything ('all'). A reader: their own pull
    list, plus — unless their pref push.reading is off — what they're reading or
    have favourited."""
    from kometa import auth, wantread
    path = path or DB_PATH
    wantread.ensure_tables(path)
    with db._connect(path) as conn:
        wanted = {r[0] for r in conn.execute("SELECT DISTINCT series_id FROM want_to_read WHERE reader_id = ? AND series_id IS NOT NULL",
                                              (reader_id,))}
        if reader_id == auth.OWNER:
            if (db.get_config(path).get("push_scope") or "pull") == "all":
                return {r[0] for r in conn.execute("SELECT id FROM tracked_series")}
            return wanted | {r[0] for r in conn.execute("SELECT id FROM tracked_series WHERE on_pull_list = 1")}
    ids = auth.pull_ids(reader_id, path) | wanted
    if auth.get_prefs(reader_id, path).get("push.reading", True):
        from kometa import marks
        marks.ensure_tables(path)                        # made on first use; a reader may beat it here
        with db._connect(path) as conn:
            ids |= {r[0] for r in conn.execute("""SELECT DISTINCT b.tracked_series_id FROM read_progress p
                JOIN books b ON b.id = p.book_id WHERE p.reader_id = ? AND b.tracked_series_id IS NOT NULL""", (reader_id,))}
            ids |= {r[0] for r in conn.execute("""SELECT target_id FROM marks
                WHERE reader_id = ? AND kind = 'series' AND favourite = 1""", (reader_id,))}
    return ids


# --- routes -------------------------------------------------------------------------------
class DeviceRequest(BaseModel):
    token: str
    env: str = "sandbox"
    name: str | None = None
    app: str | None = None
    platform: str = "apns"


def _clean_token(token: str, platform: str) -> str:
    """APNs: 64 hex chars, lowercased. FCM: a long base64url-ish string Google mints —
    case matters, so it's kept as sent; only the length and the charset are checked."""
    tok = (token or "").strip()
    if platform == "fcm":
        if not tok or len(tok) > FCM_TOKEN_MAX or any(c.isspace() or not c.isprintable() for c in tok):
            raise HTTPException(422, "token must be the FCM registration token")
        return tok
    tok = tok.lower()
    if not tok or len(tok) > 200 or any(c not in "0123456789abcdef" for c in tok):
        raise HTTPException(422, "token must be the device token in hex")
    return tok


@router.post("/api/push/devices")
def register_device(req: DeviceRequest):
    platform = req.platform if req.platform in PLATFORMS else "apns"
    tok = _clean_token(req.token, platform)
    # FCM has no sandbox/production split — Google routes on the token; env is a no-op there
    env = "production" if platform == "fcm" else (req.env if req.env in HOSTS else "sandbox")
    ensure_tables()
    with db._connect(DB_PATH) as conn:
        conn.execute("""INSERT INTO push_devices (token, env, name, app, seen_at, dead_at, reader_id, platform)
            VALUES (?, ?, ?, ?, datetime('now'), NULL, ?, ?)
            ON CONFLICT(token) DO UPDATE SET env = excluded.env, name = excluded.name, app = excluded.app,
                seen_at = excluded.seen_at, dead_at = NULL, reader_id = excluded.reader_id,
                platform = excluded.platform""",
                     (tok, env, req.name, req.app, current_reader(), platform))   # whoever's signed in on it now
    return {"ok": True}


class ForgetRequest(BaseModel):
    token: str


@router.post("/api/push/devices/forget")
def forget_device(req: ForgetRequest):
    """Notifications turned off on a device: mark its token dead so send() skips it.
    Reader-scoped — you can only forget a device registered to you (admin: any).
    Matches the token as sent OR lowercased: APNs tokens were stored lowercased, FCM
    tokens verbatim."""
    raw = (req.token or "").strip()
    if not raw:
        raise HTTPException(422, "token required")
    ensure_tables()
    from kometa.auth import current_reader, is_admin
    with db._connect(DB_PATH) as conn:
        if is_admin():
            conn.execute("UPDATE push_devices SET dead_at = datetime('now') WHERE token IN (?, ?)", (raw, raw.lower()))
        else:
            conn.execute("UPDATE push_devices SET dead_at = datetime('now') WHERE token IN (?, ?) AND reader_id = ?",
                         (raw, raw.lower(), current_reader()))
    return {"ok": True}


@router.get("/api/push/status")
def push_status():
    ensure_tables()
    with db._connect(DB_PATH) as conn:
        devs = [dict(r) for r in conn.execute(           # yours; the admin sees everyone's
            "SELECT substr(token, 1, 8) AS token, platform, env, name, seen_at, dead_at, reader_id FROM push_devices "
            "WHERE ? OR reader_id = ? ORDER BY seen_at DESC", (int(is_admin()), current_reader()))]
    cfg = db.get_config(DB_PATH)
    up = platforms()
    return {"configured": any(up.values()), "platforms": up, "scope": cfg.get("push_scope") or "pull",
            "enabled": cfg.get("push_enabled", "1") != "0", "devices": devs}
