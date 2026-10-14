"""Push to YondeB (kometa/push.py): offline — a throwaway EC key, a fake Apple."""
import base64
import json
import os

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

import kometa.db as db
from kometa import push


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
    assert p["aps"]["alert"]["title"] == "2 new on the shelf"
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
