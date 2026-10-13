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


# --- the largest URL, and upgrading what's already stored small ---------------------
S3 = "https://s3.amazonaws.com/comicgeeks/comics/covers/"


def _variants(db_path, sid, number, covers):
    import json
    import kometa.record as record
    record.ensure_tables(db_path)
    with db._connect(db_path) as c:
        c.execute("INSERT INTO issue_record (tracked_series_id, number, covers_json) VALUES (?, ?, ?)",
                  (sid, number, json.dumps(covers)))


def test_best_url_rewrites_locg_s3_small_and_medium_only():
    assert im.best_url(S3 + "medium-555.jpg") == S3 + "large-555.jpg"
    assert im.best_url(S3 + "small-555.jpg?1700000000") == S3 + "large-555.jpg?1700000000"
    assert im.best_url(S3 + "large-555.jpg") == S3 + "large-555.jpg"
    assert im.best_url("https://static.metron.cloud/media/issue/medium-1.jpg") == "https://static.metron.cloud/media/issue/medium-1.jpg"
    assert im.best_url(None) is None and im.best_url("") == ""
    assert im.best_cover_url({"thumb": S3 + "medium-9.jpg", "large": S3 + "large-9.jpg"}) == S3 + "large-9.jpg"
    assert im.best_cover_url({"thumb": S3 + "medium-9.jpg"}) == S3 + "large-9.jpg"          # no large given: rewritten
    assert im.best_cover_url({"thumb": "https://static.metron.cloud/v.jpg", "large": "https://static.metron.cloud/v.jpg"}) \
        == "https://static.metron.cloud/v.jpg"


def test_pending_picks_the_largest_url_for_locg_mains_and_variants(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, 1.0, "2012-03-14", 1, metron_image=S3 + "medium-100.jpg", path=db_path)
    _variants(db_path, sid, 1.0, [{"id": "v1", "thumb": S3 + "medium-101.jpg", "large": S3 + "large-101.jpg", "source": "locg"},
                                  {"id": "v2", "thumb": "https://static.metron.cloud/v2.jpg",
                                   "large": "https://static.metron.cloud/v2.jpg", "source": "metron"}])
    db.set_trades(sid, [{"locg_id": 7, "cover": S3 + "medium-700.jpg"}], path=db_path)
    got = {k: u for k, u, _ in im.pending(db_path)}
    assert got[im.issue_key(sid, 1.0)] == S3 + "large-100.jpg"
    assert got[im.series_key(sid)] == S3 + "large-100.jpg"
    assert got[im.variant_key(sid, 1.0, "v1")] == S3 + "large-101.jpg"
    assert got[im.variant_key(sid, 1.0, "v2")] == "https://static.metron.cloud/v2.jpg"
    assert got[im.trade_key(f"{sid}-7")] == S3 + "large-700.jpg"


def test_a_stored_thumbnail_is_requeued_with_the_large_url_and_replaced(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, 1.0, "2012-03-14", 1, metron_image=S3 + "medium-100.jpg", path=db_path)
    _variants(db_path, sid, 1.0, [{"id": "v1", "thumb": S3 + "medium-101.jpg", "large": S3 + "large-101.jpg"}])
    key, vkey = im.issue_key(sid, 1.0), im.variant_key(sid, 1.0, "v1")
    im.fetch_image(key, S3 + "medium-100.jpg", "locg", db_path, http=lambda u: b"small")     # yesterday's 170px
    im.fetch_image(vkey, S3 + "medium-101.jpg", "locg", db_path, http=lambda u: b"small")    # thumb-first variant
    queued = im.pending(db_path)
    assert (key, S3 + "large-100.jpg", "metron") in queued and (vkey, S3 + "large-101.jpg", "catalogue") in queued
    calls = []
    n = im.images_trickle(limit=10, path=db_path, http=lambda u: (calls.append(u) or b"big"), sleep=lambda s: None)
    assert n == 3 and not any("medium-" in u for u in calls)
    for k in (key, vkey):
        assert open(im.local_path(k, db_path), "rb").read() == b"big" and "large-" in im.get(k, db_path)["url"]
    assert im.pending(db_path) == []
    # a tile still asking with the catalogue's medium- URL gets the upgrade from
    # disk, not a re-download of the small one
    resp = im.serve(key, S3 + "medium-100.jpg", "metron", db_path)
    assert resp.headers["X-Kometa-Image"] == "disk" and resp.body == b"big"


def test_a_failed_upgrade_keeps_the_old_file_and_isnt_hammered(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 1.0, "2012-03-14", 1, metron_image=S3 + "medium-100.jpg", path=db_path)
    key = im.issue_key(sid, 1.0)
    im.fetch_image(key, S3 + "medium-100.jpg", "locg", db_path, http=lambda u: b"small")
    im.fetch_image(im.series_key(sid), S3 + "large-100.jpg", "metron", db_path, http=lambda u: b"big")
    calls = []
    dead = lambda u: (calls.append(u) or None)                                           # large- 403s
    assert im.images_trickle(limit=10, path=db_path, http=dead, sleep=lambda s: None) == 0
    assert calls == [S3 + "large-100.jpg"]
    assert open(im.local_path(key, db_path), "rb").read() == b"small"                    # never lost to the upgrade
    assert im.pending(db_path) == []                                                     # remembered for a day
    im.images_trickle(limit=10, path=db_path, http=dead, sleep=lambda s: None)
    assert im.fetch_image(key, S3 + "large-100.jpg", "metron", db_path, http=dead)["path"]  # old row back, no fetch
    assert len(calls) == 1
    resp = im.serve(key, S3 + "medium-100.jpg", "metron", db_path)                       # the small one still serves
    assert resp.body == b"small"
    # a day on it's tried again, and when it lands it replaces the small file
    with db._connect(db_path) as c:
        c.execute("UPDATE image_record SET failed_at = datetime('now', '-25 hours') WHERE key = ?", (key,))
    assert [k for k, _, _ in im.pending(db_path)] == [key]
    assert im.images_trickle(limit=10, path=db_path, http=lambda u: b"big", sleep=lambda s: None) == 1
    assert open(im.local_path(key, db_path), "rb").read() == b"big" and not im.get(key, db_path)["failed_at"]


def test_nothing_already_at_its_best_url_is_requeued(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    sid = db.add_series(title="Saga", publisher="Image", folder_path=None, on_pull_list=True, path=db_path)
    db.upsert_issue_status(sid, 1.0, "2012-03-14", 1, metron_image="https://static.metron.cloud/1.jpg", path=db_path)
    db.upsert_issue_status(sid, 2.0, "2012-04-11", 0, metron_image=S3 + "large-200.jpg", path=db_path)
    _variants(db_path, sid, 1.0, [{"id": "v1", "thumb": S3 + "medium-101.jpg", "large": S3 + "large-101.jpg"}])
    db.set_trades(sid, [{"locg_id": 7, "cover": S3 + "medium-700.jpg"}], path=db_path)
    calls = []
    assert im.images_trickle(limit=50, path=db_path, http=lambda u: (calls.append(u) or b"img"), sleep=lambda s: None) == 5
    assert not any("medium-" in u for u in calls)
    assert im.pending(db_path) == []
    calls.clear()
    assert im.images_trickle(limit=50, path=db_path, http=lambda u: (calls.append(u) or b"img"), sleep=lambda s: None) == 0
    assert calls == []


class _Lim:
    """A lane's limiter, no network: answers from `bodies`, or refuses."""
    def __init__(self, bodies=None, refuse=False):
        self.bodies, self.refuse, self.asked = bodies or {}, refuse, []

    def spent(self):
        return False

    def get(self, url):
        from kometa import polite
        self.asked.append(url)
        if self.refuse:
            raise polite.SourceUnavailable("refused us (429)")
        class R:
            status_code = 200 if url in self.bodies else 404
            content = self.bodies.get(url, b"")
        return R()


def test_lanes_run_each_host_at_its_own_pace_and_a_refusal_stops_only_its_own(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    monkeypatch.setattr(im, "LANES", {"fast.cdn": (3, 0, 100), "slow.org": (1, 0, 100)})
    sid = db.add_series(title="X", publisher="P", folder_path=None, on_pull_list=False, path=db_path)
    for n in range(1, 5):
        db.upsert_issue_status(sid, float(n), "2020-01-01", 1, metron_image=f"https://fast.cdn/{n}.jpg", path=db_path)
    sid2 = db.add_series(title="Y", publisher="P", folder_path=None, on_pull_list=False, path=db_path)
    for n in range(1, 4):
        db.upsert_issue_status(sid2, float(n), "2020-01-01", 1, metron_image=f"https://slow.org/{n}.jpg", path=db_path)
    fast = _Lim({f"https://fast.cdn/{n}.jpg": b"img" for n in range(1, 5)})
    slow = _Lim(refuse=True)
    monkeypatch.setattr(im, "_lane_limiter", lambda host: {"fast.cdn": fast, "slow.org": slow}[host])
    n = im.images_lanes(path=db_path, sleep=lambda s: None)
    assert len(fast.asked) == 3                       # its per-tick share, not the whole queue
    assert len(slow.asked) == 1 and n == 3            # refused once: that lane stops, the other doesn't care
