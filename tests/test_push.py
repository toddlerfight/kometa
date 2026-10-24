"""Push to YondeB (kometa/push.py): offline — a throwaway EC key, a fake Apple."""
import base64
import json
import os
from urllib.parse import parse_qs

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

import kometa.db as db
from kometa import push

# the headline rotates (push.LANDED): any of its words will do
_ONE = {one for one, _ in push.LANDED}


def _many(n):
    return {many.format(n=n) for _, many in push.LANDED}


class R:
    def __init__(self, code, body=None):
        self.status_code, self._b = code, body or {}

    def json(self):
        return self._b


@pytest.fixture
def setup(db_path, monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    d = os.path.join(os.path.dirname(db_path), "apns")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "AuthKey.p8"), "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    db.set_config({"apns_key_id": "ABC123DEFG", "apns_team_id": "TEAM123456"}, db_path)
    monkeypatch.setattr(push, "DB_PATH", db_path)
    monkeypatch.setattr(push, "_jwt", {"token": None, "at": 0.0})
    push.ensure_tables(db_path)
    return key


def _dev(db_path, tok="ab" * 32, env="sandbox"):
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO push_devices (token, env) VALUES (?, ?)", (tok, env))


def test_the_jwt_is_a_real_es256_signature(setup, db_path):
    tok = push._bearer(db_path)
    head, body, sig = tok.split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    assert json.loads(pad(head)) == {"alg": "ES256", "kid": "ABC123DEFG"}
    assert json.loads(pad(body))["iss"] == "TEAM123456"
    raw = pad(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    setup.public_key().verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))   # raises if wrong
    assert push._bearer(db_path) == tok                                                     # reused, not re-signed


def test_send_goes_to_the_right_host_and_buries_dead_tokens(setup, db_path):
    _dev(db_path, "aa" * 32, "sandbox")
    _dev(db_path, "bb" * 32, "production")
    calls = []

    def post(url, headers, body):
        calls.append((url, headers))
        return R(200) if "aaaa" in url else R(410, {"reason": "Unregistered"})
    out = push.send({"aps": {"alert": "x"}}, db_path, post)
    assert out == {"sent": 1, "failed": 0, "dead": 1}
    assert calls[0][0].startswith("https://api.sandbox.push.apple.com/3/device/")
    assert calls[1][0].startswith("https://api.push.apple.com/3/device/")
    assert calls[0][1]["apns-topic"] == "dev.kometa.yondeb" and calls[0][1]["authorization"].startswith("bearer ")
    calls.clear()
    push.send({"aps": {"alert": "x"}}, db_path, post)
    assert len(calls) == 1                                      # the dead one isn't asked again


def test_pull_list_only_and_batched_into_one(setup, db_path, monkeypatch):
    monkeypatch.setattr(push, "BATCH_S", 999)
    a = db.add_series(title="Midnight X-Men", publisher="Marvel", folder_path="/c/a", path=db_path)
    b = db.add_series(title="Not Pulled", publisher="Marvel", folder_path="/c/b", path=db_path)
    with db._connect(db_path) as conn:
        conn.execute("UPDATE tracked_series SET on_pull_list = 1 WHERE id = ?", (a,))
        conn.execute("UPDATE tracked_series SET on_pull_list = 0 WHERE id = ?", (b,))
    monkeypatch.setattr(push, "_book_id", lambda s, n: 77)
    _dev(db_path)
    sent = []
    push.issue_landed(a, 2.0, db_path)
    push.issue_landed(b, 5.0, db_path)                          # not on the pull list: nothing
    push.issue_landed(a, 3.0, db_path)
    push.issue_landed(a, 3.0, db_path)                          # the same issue twice is once
    push._timer.cancel()
    res = push.flush(db_path, post=lambda u, h, body: sent.append(json.loads(body)) or R(200))
    assert res["sent"] == 1 and len(sent) == 1
    p = sent[0]
    assert p["aps"]["alert"]["title"] in _many(2)
    assert p["aps"]["alert"]["body"] == "Midnight X-Men #2, Midnight X-Men #3"
    assert p["dest"] == "recent" and p["book_ids"] == [77, 77] and "book_id" not in p and p["kind"] == "new"
    db.set_config({"push_scope": "all"}, db_path)
    push.issue_landed(b, 5.0, db_path)
    push._timer.cancel()
    push.flush(db_path, post=lambda u, h, body: sent.append(json.loads(body)) or R(200))
    assert sent[-1]["aps"]["alert"]["body"] == "Not Pulled #5"
    assert sent[-1]["book_id"] == 77 and "dest" not in sent[-1]           # one issue: the reader


def test_unconfigured_sends_nothing(db_path, monkeypatch):
    monkeypatch.setattr(push, "DB_PATH", db_path)
    assert push.send({"aps": {}}, db_path, post=lambda *a: 1 / 0)["sent"] == 0
    push.issue_landed(1, 1.0, db_path)                          # no key: not even queued
    assert push._pending == []


def test_device_registration_is_idempotent_and_revives(setup, db_path):
    push.register_device(push.DeviceRequest(token="AB" * 32, name="iPad"))
    with db._connect(db_path) as conn:
        conn.execute("UPDATE push_devices SET dead_at = datetime('now')")
    push.register_device(push.DeviceRequest(token="ab" * 32, name="iPad"))
    st = push.push_status()
    assert len(st["devices"]) == 1 and st["devices"][0]["dead_at"] is None and st["configured"]
    not_hex = "zz" * 32
    with pytest.raises(Exception):
        push.register_device(push.DeviceRequest(token=not_hex))


def test_each_person_gets_their_own_landings_on_their_own_devices(setup, db_path, monkeypatch):
    """You: your pull list. A reader: their pull list + what they're reading. One buzz each."""
    from kometa import auth
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth._ready.clear()
    monkeypatch.setattr(push, "BATCH_S", 999)
    monkeypatch.setattr(push, "_book_id", lambda s, n: None)
    pulled = db.add_series(title="Pulled", publisher="Marvel", folder_path="/c/p", path=db_path)
    theirs = db.add_series(title="Tess Pulls", publisher="Image", folder_path="/c/t", path=db_path)
    reading = db.add_series(title="Tess Reads", publisher="DC", folder_path="/c/r", path=db_path)
    with db._connect(db_path) as conn:
        conn.execute("UPDATE tracked_series SET on_pull_list = (id = ?)", (pulled,))
    tess = auth.create_user("tess", "secret99", "reader", db_path)
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO reader_pull (reader_id, series_id, added_at) VALUES (?, ?, 'x')", (tess["id"], theirs))
        bid = conn.execute("INSERT INTO books (path, tracked_series_id, number) VALUES ('/c/r/1.cbz', ?, 1)", (reading,)).lastrowid
    db.set_progress(tess["id"], bid, 3, False, "2026-10-14T00:00:00Z", db_path)
    _dev(db_path, "aa" * 32)                                                    # the owner's
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO push_devices (token, env, reader_id) VALUES (?, 'sandbox', ?)", ("bb" * 32, tess["id"]))
    for s in (pulled, theirs, reading):
        push.issue_landed(s, 2.0, db_path)
    push._timer.cancel()
    got = {}
    push.flush(db_path, post=lambda u, h, body: got.setdefault(u.rsplit("/", 1)[1][:4], json.loads(body)) and R(200) or R(200))
    assert got["aaaa"]["aps"]["alert"]["body"] == "Pulled #2"
    assert got["bbbb"]["aps"]["alert"]["body"] == "Tess Pulls #2, Tess Reads #2"


def test_forget_marks_a_device_dead_and_scopes_to_the_reader(setup, db_path, monkeypatch):
    from kometa import push, auth
    monkeypatch.setattr(push, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    with db._connect(db_path) as c:
        c.execute("DELETE FROM push_devices")
        c.execute("INSERT INTO push_devices (token, env, reader_id, dead_at) VALUES ('aa','sandbox','me',NULL)")
        c.execute("INSERT INTO push_devices (token, env, reader_id, dead_at) VALUES ('bb','sandbox','u-tess',NULL)")
    # a reader can only forget their own device
    monkeypatch.setattr(auth, "current_reader", lambda: "me")
    monkeypatch.setattr(auth, "is_admin", lambda: False)
    push.forget_device(push.ForgetRequest(token="BB"))                 # someone else's: untouched
    with db._connect(db_path) as c:
        assert c.execute("SELECT dead_at FROM push_devices WHERE token='bb'").fetchone()[0] is None
    push.forget_device(push.ForgetRequest(token="aa"))                 # mine: dead
    with db._connect(db_path) as c:
        assert c.execute("SELECT dead_at FROM push_devices WHERE token='aa'").fetchone()[0] is not None


# --- Android: FCM HTTP v1 with a throwaway RSA service account and a fake Google --------
TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_URL = "https://fcm.googleapis.com/v1/projects/kometa-test/messages:send"


@pytest.fixture
def fcm_setup(db_path, monkeypatch):
    """FCM only — no APNs key, no apns_* config. Combine with `setup` for both."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    d = os.path.join(os.path.dirname(db_path), "fcm")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "service-account.json"), "w") as fh:
        json.dump({"type": "service_account", "project_id": "kometa-test", "private_key": pem,
                   "client_email": "push@kometa-test.iam.gserviceaccount.com", "token_uri": TOKEN_URL}, fh)
    monkeypatch.setattr(push, "DB_PATH", db_path)
    monkeypatch.setattr(push, "_fcm", {"token": None, "exp": 0.0})
    monkeypatch.setattr(push, "_jwt", {"token": None, "at": 0.0})
    push.ensure_tables(db_path)
    return key


def _fdev(db_path, tok="dAbC:APA91b-android_token_1", reader_id="me"):
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO push_devices (token, env, platform, reader_id) VALUES (?, 'production', 'fcm', ?)",
                     (tok, reader_id))


class Google:
    """Answers the token exchange and records every FCM send. `answer(token)` picks the
    reply per device token; default is a 200 with a message name."""
    def __init__(self, answer=None, token_status=200):
        self.token_calls, self.sends, self.answer, self.token_status = [], [], answer, token_status

    def __call__(self, url, headers, body):
        if url == TOKEN_URL:
            self.token_calls.append((headers, parse_qs(body.decode())))
            return R(self.token_status, {"access_token": f"ya29.n{len(self.token_calls)}", "expires_in": 3600,
                                         "token_type": "Bearer"})
        msg = json.loads(body)
        self.sends.append((url, headers, msg))
        if self.answer:
            return self.answer(msg["message"]["token"])
        return R(200, {"name": "projects/kometa-test/messages/0:1"})


def test_fcm_registration_keeps_the_token_verbatim_and_status_shows_the_platform(fcm_setup, db_path):
    mixed = "dAbC:APA91b-Mixed_Case"                 # a made-up registration token; case must survive
    push.register_device(push.DeviceRequest(token=f" {mixed} ", platform="fcm", name="Pixel", env="sandbox"))
    st = push.push_status()
    assert st["platforms"] == {"apns": False, "fcm": True} and st["configured"]
    dev = st["devices"][0]
    assert dev["platform"] == "fcm" and dev["env"] == "production" and dev["name"] == "Pixel"
    with db._connect(db_path) as conn:
        assert conn.execute("SELECT token FROM push_devices").fetchone()[0] == mixed   # case kept
    with pytest.raises(Exception):
        push.register_device(push.DeviceRequest(token="x" * 5000, platform="fcm"))
    with pytest.raises(Exception):
        push.register_device(push.DeviceRequest(token=mixed.replace("-", " "), platform="fcm"))   # whitespace: no
    with pytest.raises(Exception):
        push.register_device(push.DeviceRequest(token="zz" * 32))                 # APNs is still hex-only
    push.register_device(push.DeviceRequest(token="ab" * 32, platform="nonsense"))   # unknown platform → apns
    with db._connect(db_path) as conn:
        assert conn.execute("SELECT platform FROM push_devices WHERE token = ?", ("ab" * 32,)).fetchone()[0] == "apns"
    # forget works on the verbatim FCM token even though APNs tokens are lowercased
    push.forget_device(push.ForgetRequest(token=mixed))
    with db._connect(db_path) as conn:
        assert conn.execute("SELECT dead_at FROM push_devices WHERE platform = 'fcm'").fetchone()[0] is not None


def test_fcm_send_shape_signed_assertion_and_token_caching(fcm_setup, db_path):
    _fdev(db_path)
    g = Google()
    single = push.build_payload([{"series_id": 12, "number": 3.0, "title": "Saga", "book_id": 77}])
    assert push.send(single, db_path, g) == {"sent": 1, "failed": 0, "dead": 0}
    # the assertion: a real RS256 JWT with the service-account claims, signed by its key
    headers, form = g.token_calls[0]
    assert headers["content-type"] == "application/x-www-form-urlencoded"
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    head, body, sig = form["assertion"][0].split(".")

    def pad(s):
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    assert json.loads(pad(head)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(pad(body))
    assert claims["iss"] == "push@kometa-test.iam.gserviceaccount.com" and claims["aud"] == TOKEN_URL
    assert claims["scope"] == "https://www.googleapis.com/auth/firebase.messaging"
    assert claims["exp"] - claims["iat"] == 3600
    fcm_setup.public_key().verify(pad(sig), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    # the message: v1 shape, string-only data, high priority on the kometa-new channel
    url, h, msg = g.sends[0]
    assert url == FCM_URL and h["authorization"] == "Bearer ya29.n1" and h["content-type"] == "application/json"
    m = msg["message"]
    assert m["token"] == "dAbC:APA91b-android_token_1"
    assert m["notification"]["title"] in _ONE and m["notification"]["body"] == "Saga #3"
    assert m["data"] == {"kind": "new", "series_id": "12", "book_id": "77"}
    assert all(isinstance(v, str) for v in m["data"].values())
    assert m["android"]["priority"] == "high"
    assert m["android"]["notification"] == {"channel_id": "kometa-new", "sound": "default", "tag": "kometa-new-s12"}
    assert m["android"]["collapse_key"] == "kometa-new-s12"
    # a batch: dest=recent, book_ids as a JSON string, its own tag
    batch = push.build_payload([{"series_id": 12, "number": 3.0, "title": "Saga", "book_id": 77},
                                {"series_id": 13, "number": 1.0, "title": "Ice Cream Man", "book_id": 78}])
    push.send(batch, db_path, g)
    m = g.sends[1][2]["message"]
    assert m["notification"]["title"] in _many(2)
    assert m["data"] == {"kind": "new", "series_id": "12", "dest": "recent", "book_ids": "[77,78]"}
    assert m["android"]["notification"]["tag"] == "kometa-new-recent"
    # one token exchange for two sends: cached
    assert len(g.token_calls) == 1
    # ...until it's within five minutes of dying, then a fresh one
    push._fcm["exp"] = push.time.time() + 200
    push.send(single, db_path, g)
    assert len(g.token_calls) == 2 and g.sends[2][1]["authorization"] == "Bearer ya29.n2"


def test_fcm_buries_unregistered_and_bad_tokens_but_not_our_own_bad_messages(fcm_setup, db_path):
    _fdev(db_path, "gone")
    _fdev(db_path, "garbage")
    _fdev(db_path, "ourfault")
    _fdev(db_path, "fine")

    def answer(tok):
        return {
            "gone": R(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "Requested entity was not found.",
                                      "details": [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                                                   "errorCode": "UNREGISTERED"}]}}),
            "garbage": R(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                         "message": "The registration token is not a valid FCM registration token",
                                         "details": [{"errorCode": "INVALID_ARGUMENT"}]}}),
            "ourfault": R(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                          "message": "Invalid JSON payload received. Unknown name \"foo\"",
                                          "details": [{"errorCode": "INVALID_ARGUMENT"}]}}),
        }.get(tok, R(200, {"name": "ok"}))
    g = Google(answer)
    out = push.send(push.build_payload([{"series_id": 1, "number": 1.0, "title": "T"}]), db_path, g)
    assert out == {"sent": 1, "failed": 1, "dead": 2}
    with db._connect(db_path) as conn:
        dead = {r[0] for r in conn.execute("SELECT token FROM push_devices WHERE dead_at IS NOT NULL")}
    assert dead == {"gone", "garbage"}
    g.sends.clear()
    push.send(push.build_payload([{"series_id": 1, "number": 1.0, "title": "T"}]), db_path, g)
    assert {s[2]["message"]["token"] for s in g.sends} == {"ourfault", "fine"}       # the dead aren't asked again


def test_fcm_401_drops_the_cached_token(fcm_setup, db_path):
    _fdev(db_path)
    g = Google(lambda tok: R(401, {"error": {"status": "UNAUTHENTICATED", "message": "expired"}}))
    assert push.send(push.build_payload([{"series_id": 1, "number": 1.0, "title": "T"}]), db_path, g)["failed"] == 1
    assert push._fcm["token"] is None
    push.send(push.build_payload([{"series_id": 1, "number": 1.0, "title": "T"}]), db_path, g)
    assert len(g.token_calls) == 2


def test_fcm_token_exchange_failure_fails_the_devices_without_raising(fcm_setup, db_path):
    _fdev(db_path, "a")
    _fdev(db_path, "b")
    g = Google(token_status=500)
    assert push.send(push.build_payload([{"series_id": 1, "number": 1.0, "title": "T"}]), db_path, g) == \
        {"sent": 0, "failed": 2, "dead": 0}
    assert g.sends == []


def test_mixed_apns_and_fcm_fan_out_from_one_landing(setup, fcm_setup, db_path, monkeypatch):
    monkeypatch.setattr(push, "BATCH_S", 999)
    monkeypatch.setattr(push, "_book_id", lambda s, n: 77)
    a = db.add_series(title="Midnight X-Men", publisher="Marvel", folder_path="/c/a", path=db_path)
    with db._connect(db_path) as conn:
        conn.execute("UPDATE tracked_series SET on_pull_list = 1 WHERE id = ?", (a,))
    _dev(db_path, "aa" * 32, "production")                     # the iPhone
    _fdev(db_path, "dAbC:APA91b-pixel")                        # the Pixel
    assert push.platforms(db_path) == {"apns": True, "fcm": True}
    g = Google()
    apple = []

    def post(url, headers, body):
        if "push.apple.com" in url:
            apple.append((url, headers, json.loads(body)))
            return R(200)
        return g(url, headers, body)
    push.issue_landed(a, 2.0, db_path)
    push._timer.cancel()
    assert push.flush(db_path, post=post) == {"sent": 2, "failed": 0, "dead": 0}
    assert len(apple) == 1 and len(g.sends) == 1
    assert apple[0][0] == "https://api.push.apple.com/3/device/" + "aa" * 32
    alert = apple[0][2]["aps"]["alert"]
    assert alert["title"] in _ONE and alert["body"] == "Midnight X-Men #2"
    assert apple[0][2]["book_id"] == 77
    m = g.sends[0][2]["message"]
    assert m["notification"] == apple[0][2]["aps"]["alert"]                 # same words on both lock screens
    assert m["data"] == {"kind": "new", "series_id": str(a), "book_id": "77"}


def test_a_missing_service_account_skips_fcm_devices_and_vice_versa(setup, db_path, monkeypatch):
    # APNs only: the Pixel is skipped quietly, not failed, and Google is never called
    _dev(db_path, "aa" * 32)
    _fdev(db_path, "dAbC:APA91b-pixel")
    assert push.platforms(db_path) == {"apns": True, "fcm": False}
    urls = []
    out = push.send(push.build_payload([{"series_id": 1, "number": 1.0, "title": "T"}]), db_path,
                    lambda u, h, b: urls.append(u) or R(200))
    assert out == {"sent": 1, "failed": 0, "dead": 0}
    assert len(urls) == 1 and "apple" in urls[0]
    # FCM only: now the iPhone is the one skipped
    os.remove(push._key_path(db_path))
    monkeypatch.setattr(push, "_fcm", {"token": None, "exp": 0.0})
    d = os.path.join(os.path.dirname(db_path), "fcm")
    os.makedirs(d, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with open(os.path.join(d, "service-account.json"), "w") as fh:
        json.dump({"project_id": "kometa-test", "client_email": "p@x", "private_key": key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}, fh)
    assert push.platforms(db_path) == {"apns": False, "fcm": True}
    g = Google()
    assert push.send(push.build_payload([{"series_id": 1, "number": 1.0, "title": "T"}]), db_path, g) == \
        {"sent": 1, "failed": 0, "dead": 0}
    assert g.sends[0][0] == FCM_URL
    # neither: the old "not configured" answer, and a landing isn't even queued
    os.remove(os.path.join(d, "service-account.json"))
    assert push.send({"aps": {}}, db_path, post=lambda *a: 1 / 0)["error"] == "not configured"
    assert push.push_status()["platforms"] == {"apns": False, "fcm": False}


def test_a_series_still_pulling_holds_its_landings_for_one_notification(setup, db_path, monkeypatch):
    """Tracking an existing run queued six issues; each landing used to buzz on its own.
    Now the series holds its landings while more of it is queued, and buzzes once."""
    monkeypatch.setattr(push, "BATCH_S", 999)
    a = db.add_series(title="A Puzzlebox", publisher="DC", folder_path="/c/p", path=db_path)
    with db._connect(db_path) as conn:
        conn.execute("UPDATE tracked_series SET on_pull_list = 1 WHERE id = ?", (a,))
    for n in (1.0, 2.0, 3.0):
        db.queue_issue(a, n, db_path)
    monkeypatch.setattr(push, "_book_id", lambda s, n: 77)
    _dev(db_path)
    sent = []
    post = lambda u, h, body: sent.append(json.loads(body)) or R(200)
    with db._connect(db_path) as conn:
        conn.execute("UPDATE download_queue SET state = 'done' WHERE tracked_series_id = ? AND issue_number = 1", (a,))
    push.issue_landed(a, 1.0, db_path); push._timer.cancel()
    assert push.flush(db_path, post=post) is None and sent == []           # #2 and #3 still queued: held
    assert len(push._pending) == 1
    if push._timer: push._timer.cancel()
    with db._connect(db_path) as conn:
        conn.execute("UPDATE download_queue SET state = 'done' WHERE tracked_series_id = ?", (a,))
    push.issue_landed(a, 2.0, db_path); push.issue_landed(a, 3.0, db_path); push._timer.cancel()
    res = push.flush(db_path, post=post)
    assert res["sent"] == 1 and sent[-1]["aps"]["alert"]["title"] in _many(3)
    assert sent[-1]["aps"]["alert"]["body"] == "A Puzzlebox #1, A Puzzlebox #2, A Puzzlebox #3"
    # a stuck queue cannot hold a landing hostage past HOLD_MAX_S
    db.queue_issue(a, 9.0, db_path)
    monkeypatch.setattr(push, "HOLD_MAX_S", 0)
    push.issue_landed(a, 8.0, db_path); push._timer.cancel()
    assert push.flush(db_path, post=post)["sent"] == 1 and sent[-1]["aps"]["alert"]["body"] == "A Puzzlebox #8"


def test_a_series_whose_hold_expires_releases_every_landing_together(setup, db_path, monkeypatch):
    """2026-10-16 07:13: Puzzlebox #2 aged past HOLD_MAX_S alone and buzzed '1 new' while
    #3, #4 and #12 stayed held. The cap is per series: the oldest ages out, all go."""
    monkeypatch.setattr(push, "BATCH_S", 999)
    a = db.add_series(title="A Puzzlebox", publisher="DC", folder_path="/c/p", path=db_path)
    for n in (2.0, 3.0, 9.0):
        db.queue_issue(a, n, db_path)                                        # 9 keeps the series active
    monkeypatch.setattr(push, "_book_id", lambda s, n: 77)
    _dev(db_path)
    sent = []
    post = lambda u, h, body: sent.append(json.loads(body)) or R(200)
    push.issue_landed(a, 2.0, db_path); push.issue_landed(a, 3.0, db_path); push._timer.cancel()
    push._pending[0]["at"] -= push.HOLD_MAX_S + 1                            # #2 is old, #3 is fresh
    res = push.flush(db_path, post=post)
    assert res["sent"] == 1 and sent[-1]["aps"]["alert"]["title"] in _many(2)
    assert push._pending == []
    if push._timer: push._timer.cancel()


def test_held_landings_survive_a_restart(setup, db_path, monkeypatch):
    """Two deploys in one afternoon each ate a held landing. They live in the config
    table now and come back with a timer when the process starts."""
    monkeypatch.setattr(push, "BATCH_S", 999)
    a = db.add_series(title="A Puzzlebox", publisher="DC", folder_path="/c/p", path=db_path)
    db.queue_issue(a, 2.0, db_path)
    push.issue_landed(a, 1.0, db_path); push._timer.cancel()
    assert push.flush(db_path, post=lambda *a: R(200)) is None               # held: #2 still queued
    if push._timer: push._timer.cancel()
    assert push._timer is not None                                           # held landings always keep a timer
    saved = json.loads(db.get_config(db_path)["push_pending"])
    assert [(i["series_id"], i["number"]) for i in saved] == [(a, 1.0)]
    push._pending.clear(); push._timer = None                                # "the container restarted"
    assert push.restore_pending(db_path) == 1
    assert [(i["series_id"], i["number"]) for i in push._pending] == [(a, 1.0)] and push._timer is not None
    push._timer.cancel()


def test_the_headline_never_repeats_twice_running():
    seen = [push.build_alert([{"title": "Saga", "number": 1}])["title"] for _ in range(40)]
    assert all(a != b for a, b in zip(seen, seen[1:]))
    assert set(seen) <= _ONE and len(set(seen)) > 3
    assert push.build_alert([{"title": "Saga", "number": 1}] * 3)["title"] in _many(3)
