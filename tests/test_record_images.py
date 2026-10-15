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

    def get(self, url, headers=None):
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


def test_the_fandom_lane_downloads_without_manual_token_and_sends_token_when_configured(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    monkeypatch.setattr(im, "LANES", {im.FANDOM_HOST: (5, 0, 100)})
    sid = db.add_series(title="X", publisher="P", folder_path=None, on_pull_list=False, path=db_path)
    db.upsert_issue_status(sid, 1.0, "2020-01-01", 1, metron_image=f"https://{im.FANDOM_HOST}/a.jpg/revision/latest?cb=1", path=db_path)
    sent = []

    class L(_Lim):
        def get(self, url, headers=None):
            sent.append((url, headers))
            return super().get(url)
    lim = L({f"https://{im.FANDOM_HOST}/a.jpg/revision/latest?cb=1&format=original": b"img",
             f"https://{im.FANDOM_HOST}/b.jpg/revision/latest?cb=1&format=original": b"img"})
    monkeypatch.setattr(im, "_lane_limiter", lambda host: lim)
    assert im.images_lanes(path=db_path, sleep=lambda s: None) >= 1
    url, headers = sent[0]
    assert url.endswith("&format=original") and not headers
    sent.clear()
    db.upsert_issue_status(sid, 2.0, "2020-01-01", 1, metron_image=f"https://{im.FANDOM_HOST}/b.jpg/revision/latest?cb=1", path=db_path)
    db.set_config({"fandom_cf_clearance": "abc", "fandom_user_agent": "Safari/605"}, db_path)
    assert im.images_lanes(path=db_path, sleep=lambda s: None) >= 1
    url, headers = sent[0]
    assert url.endswith("&format=original") and headers == {"Cookie": "cf_clearance=abc", "User-Agent": "Safari/605"}


def test_a_403_with_the_pass_forgets_it(db_path, tmp_path, monkeypatch):
    from kometa import polite
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    db.set_config({"fandom_cf_clearance": "abc", "fandom_user_agent": "Safari/605"}, db_path)

    class Refuse:
        def get(self, url, headers=None):
            raise polite.SourceUnavailable("Images x refused us (403)")
    get = im._lane_fetch(Refuse(), im.FANDOM_HOST, db_path)
    import pytest
    with pytest.raises(polite.SourceUnavailable):
        get(f"https://{im.FANDOM_HOST}/a.jpg")
    assert db.get_config(db_path).get("fandom_cf_clearance") == ""


def test_a_resting_host_stops_the_lane_without_failing_the_cover(db_path, tmp_path, monkeypatch):
    from kometa import polite
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))

    def resting(url):
        raise polite.SourceUnavailable("Images x paused")
    import pytest
    with pytest.raises(polite.SourceUnavailable):
        im.fetch_image(im.issue_key(1, 1.0), "https://h/1.jpg", "locg", db_path, http=resting)
    assert im.get(im.issue_key(1, 1.0), db_path) is None          # not marked failed for a day


def test_s3s_403_is_a_missing_key_not_a_refusal(monkeypatch):
    from kometa import polite
    monkeypatch.setattr(im, "_lane_limiters", {})
    assert 403 not in im._lane_limiter("s3.amazonaws.com").refused_statuses
    assert 403 in im._lane_limiter("static.metron.cloud").refused_statuses
    assert polite.REFUSED_STATUSES == (403, 429, 503)


def test_a_fandom_burst_takes_its_lane_and_the_tick_stands_aside(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(im, "COVERS_DIR", str(tmp_path / "covers"))
    monkeypatch.setattr(im, "LANES", {im.FANDOM_HOST: (2, 0, 100)})
    monkeypatch.setattr(im, "_lane_locks", {})
    sid = db.add_series(title="X", publisher="P", folder_path=None, on_pull_list=False, path=db_path)
    for n in range(1, 6):
        db.upsert_issue_status(sid, float(n), "2020-01-01", 1, metron_image=f"https://{im.FANDOM_HOST}/{n}.jpg", path=db_path)
    db.set_config({"fandom_cf_clearance": "abc", "fandom_user_agent": "Safari/605"}, db_path)
    lim = _Lim({f"https://{im.FANDOM_HOST}/{n}.jpg?format=original": b"img" for n in range(1, 6)})
    monkeypatch.setattr(im, "_lane_limiter", lambda host: lim)
    with im._lane_lock(im.FANDOM_HOST):
        assert im.images_lanes(path=db_path, sleep=lambda s: None) == 0          # the burst has it
    assert im.fandom_burst(db_path, sleep=lambda s: None) >= 5                    # all of it, not the tick's 2


def test_issue_numbers_in_keys_never_use_scientific_notation(db_path):
    """Batman #1,000,000 (DC One Million) minted 'issue/66/1e+06/main' through f"{n:g}";
    the key check refused it and the whole cover lane died on it every tick."""
    assert im.num(12.0) == "12" and im.num(12.5) == "12.5" and im.num(1_000_000.0) == "1000000"
    assert im.issue_key(66, 1_000_000.0) == "issue/66/1000000/main"
    assert im.file_for(im.issue_key(66, 1_000_000.0), "/tmp/x").endswith("issue/66/1000000/main.jpg")
    assert im.issue_key(5, 1.0) == "issue/5/1/main"            # everything below a million is unchanged


def test_ensure_tables_rewrites_keys_minted_in_scientific_notation(db_path):
    import kometa.db as db
    im.ensure_tables(db_path)
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO image_record (key, url, source) VALUES (?, ?, ?)",
                     ("issue/66/1e+06/main", "https://static/m.jpg", "metron"))
        conn.execute("INSERT INTO image_record (key, url, source) VALUES (?, ?, ?)",
                     ("issue/66/1e+06/variant/v1", "https://static/v.jpg", "locg"))
        conn.execute("INSERT INTO image_record (key, url, source) VALUES (?, ?, ?)",
                     ("issue/66/1000000/variant/v1", "https://static/v-good.jpg", "locg"))   # the proper row already exists
    im.ensure_tables.__wrapped__(db_path)        # once_per_db already ran for this file; the migration itself, again
    with db._connect(db_path) as conn:
        keys = sorted(r[0] for r in conn.execute("SELECT key FROM image_record WHERE key LIKE 'issue/66/%'"))
    assert keys == ["issue/66/1000000/main", "issue/66/1000000/variant/v1"]


def test_a_bad_key_skips_the_item_not_the_lane(db_path, monkeypatch):
    calls = []
    def fetch(key, url, source, path, http=None):
        calls.append(key)
        if "bad key" in key:
            raise ValueError("bad image key")
        return {"key": key}
    monkeypatch.setattr(im, "fetch_image", fetch)
    monkeypatch.setattr("kometa.activity.yield_to_reader", lambda sleep: None)
    items = [("issue/1/1/main", "https://s/a.jpg", "metron"), ("issue/1/bad key/main", "https://s/b.jpg", "metron"),
             ("issue/1/2/main", "https://s/c.jpg", "metron")]
    done = im._run_lane(items, db_path, http=None, sleep=lambda s: None, gap=0, label="s")
    assert calls == [k for k, _, _ in items] and done == 2
