"""Cover art in the record's own store (kometa/images.py)."""
import os

import kometa.db as db
import kometa.images as im


def _series(db_path):
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, 1.0, "2012-03-14", 1, metron_image="https://static/1.jpg", path=db_path)
    db.upsert_issue_status(sid, 2.0, "2012-04-11", 0, metron_image="https://static/2.jpg", path=db_path)
    return sid


def test_fetch_once_keep_forever_refetch_only_when_url_changes(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    calls = []
    http = lambda url: (calls.append(url) or b"\xff\xd8jpeg-" + url.encode())
    key = im.issue_key(5, 1.0)
    r1 = im.fetch_image(key, "https://static/1.jpg", "metron", db_path, http=http)
    assert r1["path"].endswith("issue/5/1/main.jpg") and os.path.exists(r1["path"]) and r1["bytes"] > 0
    assert im.fetch_image(key, "https://static/1.jpg", "metron", db_path, http=http)["fetched_at"] == r1["fetched_at"]
    assert calls == ["https://static/1.jpg"]                                      # served from disk, not re-fetched
    im.fetch_image(key, "https://static/1-new.jpg", "metron", db_path, http=http)
    assert calls[-1] == "https://static/1-new.jpg" and im.get(key, db_path)["url"].endswith("1-new.jpg")
    assert im.local_path(key, db_path) == r1["path"]


def test_a_failure_is_remembered_for_a_day(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    calls = []
    bad = lambda url: (calls.append(url) or None)
    key = im.series_key(7)
    assert im.fetch_image(key, "https://static/dead.jpg", "metron", db_path, http=bad) is None
    assert im.fetch_image(key, "https://static/dead.jpg", "metron", db_path, http=bad) is None
    assert len(calls) == 1                                                        # not hammered
    assert im.local_path(key, db_path) is None


def test_trickle_walks_issue_mains_and_series_covers_owned_first(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    sid = _series(db_path)
    want = [k for k, _, _ in im.pending(db_path)]
    assert want[:3] == [im.issue_key(sid, 1.0), im.series_key(sid), im.issue_key(sid, 2.0)]
    n = im.images_trickle(limit=10, path=db_path, http=lambda u: b"img", sleep=lambda s: None)
    assert n == 3 and im.pending(db_path) == []
    assert im.store_size(db_path)["images"] == 3
    # the serve path answers from disk with the header a probe can read
    resp = im.serve(im.issue_key(sid, 1.0), "https://static/1.jpg", "metron", db_path)
    assert resp.headers["X-Kometa-Image"] == "disk" and resp.body == b"img"


def test_trickle_stops_after_a_run_of_failures(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    monkeypatch.setattr(im, "TRICKLE_MAX_FAILS", 2)
    sid = db.add_series(title="X", publisher="P", folder_path=None, on_pull_list=False, path=db_path)
    for n in range(1, 6):
        db.upsert_issue_status(sid, float(n), "2020-01-01", 0, metron_image=f"https://static/{n}.jpg", path=db_path)
    calls = []
    assert im.images_trickle(limit=10, path=db_path, http=lambda u: (calls.append(u) or None), sleep=lambda s: None) == 0
    assert len(calls) == 2
