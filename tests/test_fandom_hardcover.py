"""Enrichment 1i-d (Fandom cover galleries, the worker) and 1i-f (Hardcover).
Fully offline: the Fandom worker takes its gallery lookup as a seam, Hardcover's
one HTTP seam (polite.requests.post) is stubbed, and every answer here is a
small hand-made shape, not a copy of either site."""
import io
import json

import pytest
from PIL import Image, ImageDraw

import kometa.db as db
import kometa.record as rec
from kometa import fandom_client, fandom_covers, hardcover, harvest, images, main, polite


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "COVERS_DIR", str(tmp_path / "covers"))
    return tmp_path / "covers"


@pytest.fixture(autouse=True)
def fresh_fandom_budget(monkeypatch):
    monkeypatch.setattr(fandom_client, "_last", {"t": 0.0, "day": "", "count": 0, "paused_until": 0.0})


def _issue(db_path, title="Midnight Spider-Man", publisher="Marvel", n=1.0, owned=1, pull=True, extra=None, isbn=None,
           sid=None):
    if sid is None:
        sid = db.add_series(title=title, publisher=publisher, folder_path=None, on_pull_list=pull, path=db_path)
    db.upsert_issue_status(sid, n, None, owned, metron_issue_id=int(n) + 100, path=db_path)
    rec.write_issue(sid, n, {"desc": "d", "isbn": isbn, "extra": {"series_volume": 1} if extra is None else extra},
                    "metron", "full", {"metron_issue_id": int(n) + 100}, db_path)
    return sid


COVERS = [{"name": "616 Comics Exclusive Variant", "url": "https://img.example/616.jpg", "width": 1332, "height": 2048},
          {"name": "Textless", "url": "https://img.example/textless.jpg", "width": 1400, "height": 2100}]


class FakeWiki:
    """title → covers list (None = no such page); every ask logged."""
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def __call__(self, wiki, title):
        self.asked.append((wiki, title))
        return self.pages.get(title)


# --- Fandom: the worker -------------------------------------------------------------
def test_owned_marvel_issue_gets_its_gallery_listed_once(db_path):
    sid = _issue(db_path)
    _issue(db_path, title="Saga", publisher="Image Comics")                 # no wiki for Image
    _issue(db_path, n=2.0, owned=0, sid=sid)                                # not owned
    wiki = FakeWiki({"Midnight Spider-Man Vol 1 1": COVERS})
    assert fandom_covers.tick(path=db_path, gallery=wiki) == 1
    assert wiki.asked == [("marvel", "Midnight Spider-Man Vol 1 1")]
    g = rec.get_issue(sid, 1.0, db_path)["extra"]["fandom"]
    assert g["page"] == "Midnight Spider-Man Vol 1 1" and g["at"] and len(g["covers"]) == 2
    assert rec.get_issue(sid, 1.0, db_path)["extra"]["series_volume"] == 1  # Metron's keys untouched
    assert fandom_covers.tick(path=db_path, gallery=wiki) == 0 and len(wiki.asked) == 1   # not again within 30 days


def test_a_missing_page_is_remembered_and_the_bare_title_goes_first(db_path):
    sid = _issue(db_path, title="The Mighty Thor", extra={"series_volume": 2})
    wiki = FakeWiki({})
    fandom_covers.tick(path=db_path, gallery=wiki)
    assert [t for _, t in wiki.asked] == ["Mighty Thor Vol 2 1", "The Mighty Thor Vol 2 1"]
    g = rec.get_issue(sid, 1.0, db_path)["extra"]["fandom"]
    assert g == {"page": "Mighty Thor Vol 2 1", "at": g["at"], "missing": True}


def test_asked_again_after_thirty_days(db_path, monkeypatch):
    sid = _issue(db_path)
    rec.merge_extra(sid, 1.0, {"fandom": {"page": "x", "at": "2020-01-01", "missing": True}}, db_path)
    wiki = FakeWiki({"Midnight Spider-Man Vol 1 1": COVERS})
    assert fandom_covers.tick(path=db_path, gallery=wiki) == 1


def test_multi_run_series_takes_the_runs_name_only_when_the_record_says(db_path):
    sid = _issue(db_path, title="Uncanny X-Men", extra={"series_volume": 1, "series_name": "The X-Men"})
    _issue(db_path, n=142.0, sid=sid, extra={"series_volume": 1})          # the record doesn't say which run
    db.set_metron_extra_ids(sid, [1581], db_path)
    wiki = FakeWiki({"X-Men Vol 1 1": COVERS, "Uncanny X-Men Vol 1 142": []})
    assert fandom_covers.tick(path=db_path, gallery=wiki) == 2
    assert [t for _, t in wiki.asked] == ["X-Men Vol 1 1", "Uncanny X-Men Vol 1 142"]
    assert rec.get_issue(sid, 142.0, db_path)["extra"]["fandom"]["covers"] == []   # page there, no covers


def test_single_run_ignores_metrons_run_name(db_path):
    _issue(db_path, title="Batman (2016)", publisher="DC Comics", extra={"series_volume": 3, "series_name": "Batman"})
    wiki = FakeWiki({})
    fandom_covers.tick(path=db_path, gallery=wiki)
    assert wiki.asked[0] == ("dc", "Batman Vol 3 1")


def test_pull_list_first_and_ten_a_tick(db_path):
    later = _issue(db_path, title="Old Run", pull=False)
    pull = _issue(db_path, title="Pull Run")
    for n in range(2, 13):
        _issue(db_path, n=float(n), sid=pull)
    wiki = FakeWiki({})
    assert fandom_covers.tick(path=db_path, gallery=wiki) == fandom_covers.PER_TICK
    assert all(t.startswith("Pull Run") for _, t in wiki.asked)
    assert "fandom" not in rec.get_issue(later, 1.0, db_path)["extra"]


def test_off_capped_or_resting_asks_nothing(db_path, monkeypatch):
    _issue(db_path)
    wiki = FakeWiki({"Midnight Spider-Man Vol 1 1": COVERS})
    db.set_config({"fandom_enabled": "0"}, db_path)
    assert fandom_covers.tick(path=db_path, gallery=wiki) == 0
    db.set_config({"fandom_enabled": "1"}, db_path)
    import time
    monkeypatch.setattr(fandom_client, "_last", {"t": 0.0, "day": time.strftime("%Y-%m-%d"),
                                                 "count": fandom_client.DAILY_CAP, "paused_until": 0.0})
    assert fandom_covers.tick(path=db_path, gallery=wiki) == 0
    monkeypatch.setattr(fandom_client, "_last", {"t": 0.0, "day": "", "count": 0, "paused_until": 9e18})
    assert fandom_covers.tick(path=db_path, gallery=wiki) == 0
    assert wiki.asked == []


def test_wiki_going_quiet_mid_tick_records_nothing_for_that_issue(db_path):
    sid = _issue(db_path)
    _issue(db_path, n=2.0, sid=sid)

    def flaky(wiki, title):
        if title.endswith(" 2"):
            raise fandom_client.FandomUnavailable("paused")
        return COVERS
    assert fandom_covers.tick(path=db_path, gallery=flaky) == 1
    assert "fandom" not in rec.get_issue(sid, 2.0, db_path)["extra"]


def test_a_metron_refresh_keeps_what_other_workers_wrote(db_path):
    sid = _issue(db_path)
    rec.merge_extra(sid, 1.0, {"fandom": {"page": "p", "at": "2026-10-12", "covers": []}, "prh": None,
                               "cv_original": "https://cv/x.jpg"}, db_path)
    rec.write_issue(sid, 1.0, {"desc": "new", "extra": {"series_volume": 1, "upc": "1"}}, "metron", "full",
                    {"metron_issue_id": 101}, db_path)
    extra = rec.get_issue(sid, 1.0, db_path)["extra"]
    assert extra["upc"] == "1" and extra["fandom"]["page"] == "p" and "prh" in extra and extra["cv_original"]


# --- Fandom: downloads (behind fandom_download) ---------------------------------------
def _jpeg(seed: int, size=(300, 450)) -> bytes:
    im = Image.new("RGB", size, (20, 20, 20))
    d = ImageDraw.Draw(im)
    w, h = size
    for i in range(6):
        k = (seed * 7 + i * 13) % 10
        d.rectangle([w * k // 12, h * i // 6, w * (k + 2) // 12, h * (i + 1) // 6], fill=(240, 240, 240))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _with_gallery(db_path):
    sid = _issue(db_path)
    rec.merge_extra(sid, 1.0, {"fandom": {"page": "Midnight Spider-Man Vol 1 1", "at": "2026-10-12",
                                          "covers": [dict(c) for c in COVERS]}}, db_path)
    return sid


def test_downloads_are_off_unless_switched_on(db_path, store):
    sid = _with_gallery(db_path)
    assert not [k for k, _, s in images.pending(db_path, limit=1000) if s == "fandom"]
    db.set_config({"fandom_download": "1"}, db_path)
    queued = images.pending(db_path, limit=1000)
    fk = [(k, u) for k, u, s in queued if s == "fandom"]
    assert fk == [(images.fandom_key(sid, 1.0, "616 Comics Exclusive Variant"), COVERS[0]["url"]),
                  (images.fandom_key(sid, 1.0, "Textless"), COVERS[1]["url"])]
    assert queued[-1][2] == "fandom"                                         # last in the queue
    assert fk[0][0] == f"issue/{sid}/1/fandom/616_Comics_Exclusive_Variant"


def test_trickle_ignores_gallery_urls_while_off(db_path, store):
    _with_gallery(db_path)
    fetched = []
    images.images_trickle(path=db_path, http=lambda u: fetched.append(u) or _jpeg(1), sleep=lambda s: None)
    assert not [u for u in fetched if "img.example" in u]


def test_a_downloaded_cover_matching_one_we_hold_is_an_upgrade(db_path, store):
    sid = _with_gallery(db_path)
    images.store_bytes(images.variant_key(sid, 1.0, 77), _jpeg(1, (120, 180)), "https://locg/77.jpg", "locg", db_path)
    images.store_bytes(f"issue/{sid}/1/file", _jpeg(1, (120, 180)), "file://x", "file", db_path)
    db.set_config({"fandom_download": "1"}, db_path)
    pages = {COVERS[0]["url"]: _jpeg(1, (1332, 2048)), COVERS[1]["url"]: _jpeg(4, (1400, 2100))}
    n = images.images_trickle(path=db_path, http=lambda u: pages.get(u) or _jpeg(9), sleep=lambda s: None)
    assert n >= 2
    covers = {c["name"]: c for c in rec.get_issue(sid, 1.0, db_path)["extra"]["fandom"]["covers"]}
    assert covers["616 Comics Exclusive Variant"]["upgrade_of"] == images.variant_key(sid, 1.0, 77)
    assert covers["Textless"]["upgrade_of"] is None                          # a cover we didn't have
    held = images.get(images.fandom_key(sid, 1.0, "Textless"), db_path)
    assert held["source"] == "fandom" and held["bytes"] > 0
    st = fandom_covers.status(db_path)
    assert st["galleries"] == 1 and st["covers"] == 2 and st["upgrades"] == 1 and st["downloaded"] == 2


def test_a_failed_gallery_download_is_remembered_for_a_day(db_path, store):
    sid = _with_gallery(db_path)
    key = images.fandom_key(sid, 1.0, "Textless")
    assert fandom_covers.download(key, COVERS[1]["url"], db_path, http=lambda u: None) is None
    assert images.get(key, db_path)["failed_at"]


def test_fandom_status_counts_missing_pages(db_path):
    sid = _issue(db_path)
    rec.merge_extra(sid, 1.0, {"fandom": {"page": "p", "at": "2026-10-12", "missing": True}}, db_path)
    st = fandom_covers.status(db_path)
    assert st["missing"] == 1 and st["galleries"] == 0 and st["download"] is False


# --- Hardcover ----------------------------------------------------------------------
ISBN = "9781607066019"
FAKE = "-".join(["placeholder", "value"])     # a stand-in, not a credential


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


@pytest.fixture
def hc(monkeypatch):
    """answers: a list of (status, body[, headers]) handed out in order; calls logged."""
    answers, calls = [], []

    def fake_post(url, headers=None, timeout=None, json=None, **kw):
        calls.append({"url": url, "headers": headers, "body": json})
        assert headers["User-Agent"].startswith("Kometa/")
        status, body, *h = answers.pop(0)
        return _Resp(status, body, h[0] if h else None)

    monkeypatch.setattr(polite.requests, "post", fake_post)
    monkeypatch.setattr(polite.requests, "get", lambda *a, **k: pytest.fail("Hardcover only POSTs"))
    hardcover.limiter.reset()
    monkeypatch.setattr(hardcover.limiter, "min_interval_s", 0)
    return answers, calls


def _answer(*eds):
    return {"data": {"editions": [{"id": i, "isbn_13": isbn, "book": {"id": i * 10, "slug": slug, "title": slug,
                                                                         "rating": rating, "ratings_count": count}}
                                  for i, (isbn, slug, rating, count) in enumerate(eds, 1)]}}


def test_isbn13_converts_isbn10():
    assert hardcover.isbn13("1-60706-601-6") == ISBN
    assert hardcover.isbn13("978-1-60706-601-9") == ISBN
    assert hardcover.isbn13("not an isbn") is None


def test_hardcover_batches_isbns_and_snapshots_beside_locg(db_path, hc):
    answers, calls = hc
    db.set_config({"hardcover_token": "Bearer " + FAKE}, db_path)
    sid = _issue(db_path, title="Saga", publisher="Image Comics", isbn="978-1-60706-601-9")
    rec.set_community(sid, 1.0, {"rating": 4.0}, db_path)
    with db._connect(db_path) as conn:
        conn.execute("INSERT INTO trade_record (tracked_series_id, key, title, isbn) VALUES (?, 't2', 'Saga Vol 2', ?)",
                     (sid, "9781607066927"))
    answers.append((200, _answer((ISBN, "saga-volume-one", 4.3712, 812), (ISBN, "saga-dupe", 3.0, 2))))
    assert hardcover.tick(path=db_path) == 2
    assert len(calls) == 1                                                   # one request for the whole batch
    c = calls[0]
    assert c["url"] == hardcover.API and c["headers"]["authorization"] == "Bearer " + FAKE
    assert c["body"]["variables"] == {"isbns": [ISBN, "9781607066927"]}
    assert "_in" in c["body"]["query"] and "me" not in c["body"]["query"].split()
    got = hardcover.get(ISBN, db_path)
    assert got["state"] == "found" and got["rating"] == 4.37 and got["rating_count"] == 812 and got["slug"] == "saga-volume-one"
    assert hardcover.get("9781607066927", db_path)["state"] == "none"
    com = rec.get_issue(sid, 1.0, db_path)["community"]
    assert com["rating"] == 4.0                                              # LOCG's, kept
    assert com["hardcover"]["rating"] == 4.37 and com["hardcover"]["count"] == 812
    assert com["hardcover"]["url"] == "https://hardcover.app/books/saga-volume-one" and com["hardcover"]["at"]
    assert hardcover.tick(path=db_path) == 0 and len(calls) == 1             # answered: not asked again for months


def test_hardcover_needs_a_token_and_the_toggle(db_path, hc):
    answers, calls = hc
    _issue(db_path, title="Saga", publisher="Image Comics", isbn=ISBN)
    assert hardcover.tick(path=db_path) == 0
    db.set_config({"hardcover_token": "tok", "hardcover_enabled": "0"}, db_path)
    assert hardcover.tick(path=db_path) == 0
    assert calls == []


def test_hardcover_refusals_and_errors_record_nothing(db_path, hc):
    answers, calls = hc
    db.set_config({"hardcover_token": "tok"}, db_path)
    _issue(db_path, title="Saga", publisher="Image Comics", isbn=ISBN)
    answers.append((401, {"error": "invalid_token"}))
    assert hardcover.tick(path=db_path) == 0
    answers.append((200, {"errors": [{"message": "field 'x' not found"}]}))
    assert hardcover.tick(path=db_path) == 0
    assert hardcover.get(ISBN, db_path) is None
    answers.append((429, {"error": "Too Many Requests"}, {"Retry-After": "60"}))
    assert hardcover.tick(path=db_path) == 0
    assert hardcover.limiter.budget()["paused"]
    assert hardcover.tick(path=db_path) == 0 and len(calls) == 3             # resting: not even sent


def test_hardcover_rests_when_its_own_daily_count_runs_low(db_path, hc):
    answers, calls = hc
    db.set_config({"hardcover_token": "tok"}, db_path)
    _issue(db_path, title="Saga", publisher="Image Comics", isbn=ISBN)
    answers.append((200, _answer((ISBN, "saga", 4.0, 3)), {"X-RateLimit-Daily-Remaining": "12",
                                                            "X-RateLimit-Daily-Reset": "9999999999"}))
    assert hardcover.tick(path=db_path) == 1
    assert hardcover.limiter.budget()["paused"]


def test_hardcover_limits():
    assert hardcover.limiter.min_interval_s >= 1.2 and hardcover.limiter.daily_cap == 4000


# --- Settings: config, test, disconnect, nag ----------------------------------------
@pytest.fixture
def wired(tmp_path, monkeypatch):
    dbp = str(tmp_path / "k.db")
    db.init_db(dbp)
    root = tmp_path / "comics"
    root.mkdir()
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(main, "DB_PATH", dbp)
    monkeypatch.setattr(main, "_comics_root", lambda: str(root))
    monkeypatch.setattr(main, "_locg_status", lambda: {"locg_pass_live": False, "locg_topup_waiting": 0})
    db.set_config({"metron_token": "t", "cv_api_key": "k", "gcd_user": "u", "gcd_pass": "p",
                   "prowlarr_url": "http://p", "prowlarr_apikey": "k", "sab_url": "http://s", "sab_apikey": "k",
                   "qbit_url": "http://q", "qbit_user": "u"}, dbp)
    return dbp


def test_hardcover_token_is_never_echoed_and_disconnects(wired):
    cfg = main.update_config(main.ConfigRequest(hardcover_token=FAKE))
    assert cfg["hardcover_token"] == "" and cfg["hardcover_configured"] is True
    assert FAKE not in json.dumps(cfg)
    cfg = main.disconnect_integration("hardcover")
    assert cfg["hardcover_configured"] is False


def test_hardcover_nags_only_when_on_and_tokenless(wired):
    assert {i["card"] for i in main.settings_attention()} == {"hardcover"}
    db.set_config({"hardcover_enabled": "0"}, wired)
    assert main.settings_attention() == []
    db.set_config({"hardcover_enabled": "1", "hardcover_token": "t"}, wired)
    assert main.settings_attention() == []


def test_hardcover_test_endpoint_asks_one_light_question(wired, hc):
    answers, calls = hc
    assert main.test_hardcover() == {"ok": False, "error": "Not configured"} and calls == []
    db.set_config({"hardcover_token": "t"}, wired)
    answers.append((200, {"data": {"editions": [{"id": 1}]}}))
    r = main.test_hardcover()
    assert r["ok"] and len(calls) == 1 and calls[0]["body"]["variables"] == {"isbn": hardcover.TEST_ISBN}
    answers.append((401, {"error": "invalid_token"}))
    assert main.test_hardcover() == {"ok": False, "error": "Hardcover refused the token"}


def test_fandom_toggles_round_trip(wired):
    cfg = main.get_config()
    assert cfg["fandom_enabled"] is True and cfg["fandom_download"] is False      # downloads OFF by default
    assert cfg["extra_budgets"]["fandom"]["cap"] == fandom_client.DAILY_CAP
    assert cfg["extra_budgets"]["hardcover"]["cap"] == 4000
    cfg = main.update_config(main.ConfigRequest(fandom_enabled="0", fandom_download="1"))
    assert cfg["fandom_enabled"] is False and cfg["fandom_download"] is True


def test_harvest_progress_has_fandom_and_hardcover_rows(db_path):
    rec.ensure_tables(db_path)
    p = harvest.progress(db_path)
    assert {"galleries", "missing", "covers"} <= set(p["fandom"])
    assert {"rated", "none", "configured"} <= set(p["hardcover"]) and p["hardcover"]["budget"]["cap"] == 4000
