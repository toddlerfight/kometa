"""Enrichment 1i-e: Marvel.com's print-size covers, overnight. Fully offline —
the one HTTP seam (polite.requests.get) is stubbed, and the search answer and
issue page here are tiny hand-made stand-ins with the same structure as the
real ones (probed 2026-10-12), not copies of Marvel's pages."""
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw

import kometa.db as db
import kometa.record as rec
from kometa import harvest, images, main, marveldc, polite
from kometa.scheduler import TZ

NIGHT = datetime(2026, 10, 12, 23, 30, tzinfo=TZ)
DAY = datetime(2026, 10, 12, 14, 0, tzinfo=TZ)
UPC = "75960600000100111"
CDN = "https://cdn.marvel.com/u/prod/marvel/i/mg/"
PAGE = "https://www.marvel.com/comics/issue/9001/test_run_2026_1"


# --- synthetic shapes ------------------------------------------------------------------
def search_answer(*cards):
    return {"code": 200, "data": {"offset": 0, "limit": 20, "count": len(cards), "results": [
        {"content_type": ct, "headline": head, "link": {"link": link, "title": head}} for ct, head, link in cards]}}


def issue_page(upc=UPC, main="a1/aaa", variants=("b2/bbb",)):
    info = [{"title": "Format", "value": "Comic"}] + ([{"title": "UPC", "value": upc}] if upc else [])
    details = {"id": "9001", "title": "Test Run (2026) #1", "image": f"{CDN}{main}/detail.jpg",
               "details": {"info": info},
               "variants": [{"id": str(500 + i), "title": "Test Run (2026) #1 (Variant)",
                             "image": f"{CDN}{v}/portrait_uncanny.jpg"} for i, v in enumerate(variants)]}
    blob = {"page": {"content": {"issueDetails": details}}}
    return f"<html><script>window['__CONFIG__']={{}};window['__marvel-fitt__']={json.dumps(blob)};</script></html>"


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


class _Resp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        self.content = body if isinstance(body, bytes) else body.encode()
        self.text = self.content.decode("utf-8", "replace")
        self.headers = headers or {}

    def json(self):
        return json.loads(self.content)


@pytest.fixture
def web(monkeypatch):
    """routes[url] = (status, body[, headers]); every call logged; an unknown URL
    fails the test (nothing leaves the box)."""
    routes, calls = {}, []

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append(url)
        assert "Kometa/1.0" in (headers or {}).get("User-Agent", "")
        if url not in routes:
            raise AssertionError(f"unexpected request: {url}")
        status, body, *h = routes[url]
        return _Resp(status, body, h[0] if h else None)

    monkeypatch.setattr(polite.requests, "get", fake_get)
    for lim in (marveldc.marvel, marveldc.dc, marveldc.cdn):
        lim.reset()
        monkeypatch.setattr(lim, "sleep", lambda s: None)
    return routes, calls


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "COVERS_DIR", str(tmp_path / "covers"))
    return tmp_path / "covers"


def _issue(db_path, title="Test Run", publisher="Marvel", n=1.0, upc=UPC, year=2026, pull=True,
           store_date="2026-10-07", owned=1, sid=None):
    if sid is None:
        sid = db.add_series(title=title, publisher=publisher, year_began=year, folder_path=None,
                            on_pull_list=pull, path=db_path)
    db.upsert_issue_status(sid, n, store_date, owned, metron_issue_id=int(n) + 100, path=db_path)
    extra = {"series_volume": 1}
    if upc:
        extra["upc"] = upc
    rec.write_issue(sid, n, {"desc": "d", "extra": extra}, "metron", "full", {"metron_issue_id": int(n) + 100}, db_path)
    return sid


def _route_found(routes, title="Test Run", n=1, page=None, year=2026, url=PAGE):
    routes[marveldc.search_url(title, float(n))] = (200, search_answer(
        ("comic-series", f"{title} ({year} - Present)", "https://www.marvel.com/comics/series/1/x"),
        ("comic-issue", f"{title} ({year}) #{n} (Variant)", "https://www.marvel.com/comics/issue/9002/v"),
        ("comic-issue", f"{title} ({year}) #{n}", url)))
    routes[url] = (200, page if page is not None else issue_page())


# --- the night window -------------------------------------------------------------------
def test_window_wraps_midnight():
    assert marveldc.parse_hours("23-7") == (23, 7) and marveldc.parse_hours("junk") == (0, 0)
    assert [h for h in range(24) if marveldc.in_window(h, 23, 7)] == [0, 1, 2, 3, 4, 5, 6, 23]
    assert [h for h in range(24) if marveldc.in_window(h, 1, 4)] == [1, 2, 3]
    assert marveldc.night_of(datetime(2026, 10, 13, 2, 0, tzinfo=TZ), 23, 7) == "2026-10-12"
    assert marveldc.night_of(NIGHT, 23, 7) == "2026-10-12"


def test_outside_the_window_nothing_is_sent(db_path, web):
    _issue(db_path)
    _, calls = web
    db.set_config({"marveldc_hours": "23-7"}, db_path)                   # a window set by hand (default is all day)
    assert marveldc.tick(db_path, now=DAY) == 0
    assert marveldc.tick(db_path, now=datetime(2026, 10, 13, 7, 0, tzinfo=TZ)) == 0     # 7 is the end, exclusive
    assert calls == []
    db.set_config({"marveldc_hours": "13-15"}, db_path)                  # the user's own window is honoured
    routes, _ = web
    _route_found(routes)
    assert marveldc.tick(db_path, now=DAY, downloads=0) == 1


def test_toggle_off_is_a_no_op(db_path, web):
    _issue(db_path)
    db.set_config({"marveldc_enabled": "0"}, db_path)
    assert marveldc.tick(db_path, now=NIGHT) == 0
    assert web[1] == []


# --- the UPC rule -----------------------------------------------------------------------
def test_a_matching_upc_is_accepted_with_its_images_queued(db_path, web):
    sid = _issue(db_path)
    routes, calls = web
    _route_found(routes)
    assert marveldc.tick(db_path, now=NIGHT, downloads=0) == 1
    assert calls == [marveldc.search_url("Test Run", 1.0), PAGE]            # the variant card isn't opened
    e = rec.get_issue(sid, 1.0, db_path)["extra"]["marveldc"]
    assert e["matched"] is True and e["site"] == "marvel" and e["page"] == PAGE and e["night"] == "2026-10-12"
    assert e["main"] == f"{CDN}a1/aaa.jpg"                                    # size suffix dropped
    assert e["variants"] == [{"id": "500", "url": f"{CDN}b2/bbb.jpg", "width": None, "height": None}]
    assert [(i["key"], i["status"]) for i in e["images"]] == [
        (f"issue/{sid}/1/marveldc", "pending"), (f"issue/{sid}/1/marveldc/500", "pending")]


@pytest.mark.parametrize("page_upc, why", [
    ("75960600000100121", "UPC differs"),            # same run, another cover
    ("75960600000200111", "UPC differs"),            # another issue
    (None, "no UPC on the page"),
])
def test_anything_but_the_same_upc_is_refused(db_path, web, page_upc, why):
    sid = _issue(db_path)
    routes, _ = web
    _route_found(routes, page=issue_page(upc=page_upc))
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    e = rec.get_issue(sid, 1.0, db_path)["extra"]["marveldc"]
    assert e["matched"] is False and why in e["reason"]
    assert e["main"] is None and e["variants"] == [] and e["images"] == []


def test_upc_comparison_is_exact_after_digits():
    assert marveldc.upc_matches("7 59606 00000 1 00111", "75960600000100111")
    assert marveldc.upc_matches("075960600000100111", "75960600000100111")      # EAN-18 form
    assert not marveldc.upc_matches("759606000001", "75960600000100111")         # base alone isn't the issue
    assert not marveldc.upc_matches("", "")
    assert not marveldc.upc_matches(None, "75960600000100111")


def test_not_in_search_is_recorded_with_its_reason(db_path, web):
    sid = _issue(db_path)
    routes, calls = web
    routes[marveldc.search_url("Test Run", 1.0)] = (200, search_answer(
        ("comic-issue", "Test Run (1998) #1", "https://www.marvel.com/comics/issue/1/old")))   # 28 years off
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    e = rec.get_issue(sid, 1.0, db_path)["extra"]["marveldc"]
    assert e["matched"] is False and "search" in e["reason"] and len(calls) == 1


def test_a_search_in_a_shape_we_dont_read_records_nothing_and_rests(db_path, web):
    sid = _issue(db_path)
    routes, _ = web
    routes[marveldc.search_url("Test Run", 1.0)] = (200, {"something": "else"})
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    assert "marveldc" not in rec.get_issue(sid, 1.0, db_path)["extra"]
    assert marveldc.marvel.budget()["paused"]


# --- who gets asked ---------------------------------------------------------------------
def test_candidates_marvel_with_upc_pull_list_then_newest(db_path):
    a = _issue(db_path, title="Old Run", pull=False, store_date="2026-09-01")
    b = _issue(db_path, title="New Run", pull=False, store_date="2026-10-07")
    c = _issue(db_path, title="Pulled Run", pull=True, store_date="2020-01-01")
    _issue(db_path, title="No Barcode", upc=None)
    _issue(db_path, title="Absolute Thing", publisher="DC Comics")           # DC pages carry no UPC
    _issue(db_path, title="Saga", publisher="Image")
    _issue(db_path, title="Not Owned", owned=0)
    assert [x["sid"] for x in marveldc.candidates(db_path)] == [c, b, a]


def test_nothing_is_asked_twice(db_path, web):
    sid = _issue(db_path)
    routes, calls = web
    _route_found(routes)
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    n = len(calls)
    assert marveldc.candidates(db_path) == []
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    assert len(calls) == n
    # a miss comes back after RECHECK_DAYS; a match never does
    old = (datetime.now(timezone.utc) - timedelta(days=marveldc.RECHECK_DAYS + 1)).strftime("%Y-%m-%d %H:%M:%S")
    e = rec.get_issue(sid, 1.0, db_path)["extra"]["marveldc"]
    rec.merge_extra(sid, 1.0, {"marveldc": dict(e, at=old)}, db_path)
    assert marveldc.candidates(db_path) == []
    rec.merge_extra(sid, 1.0, {"marveldc": dict(e, at=old, matched=False)}, db_path)
    assert [c["sid"] for c in marveldc.candidates(db_path)] == [sid]


def test_a_metron_refresh_keeps_the_answer(db_path):
    sid = _issue(db_path)
    rec.merge_extra(sid, 1.0, {"marveldc": {"site": "marvel", "matched": False, "at": "2026-10-12"}}, db_path)
    rec.write_issue(sid, 1.0, {"desc": "new", "extra": {"upc": UPC}}, "metron", "full", {"metron_issue_id": 101}, db_path)
    assert rec.get_issue(sid, 1.0, db_path)["extra"]["marveldc"]["matched"] is False


# --- the limits -------------------------------------------------------------------------
def test_limits():
    assert marveldc.marvel.min_interval_s >= 60 and marveldc.marvel.daily_cap == 400
    assert marveldc.dc.min_interval_s >= 60 and marveldc.dc.daily_cap == 400
    assert marveldc.marvel.refused_pause_s == 3600 and marveldc.dc.refused_pause_s == 3600
    assert marveldc.cdn.min_interval_s >= 5 and marveldc.cdn is not marveldc.marvel
    assert marveldc.PAGES_PER_TICK <= 3


def test_a_tick_sends_at_most_three_page_requests(db_path, web):
    routes, calls = web
    for i, t in enumerate(("Run A", "Run B", "Run C", "Run D")):
        _issue(db_path, title=t, store_date=f"2026-10-0{i + 1}")
        _route_found(routes, title=t)
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    assert len(calls) <= 3


def test_the_minute_gap_is_kept(web):
    routes, _ = web
    routes["https://www.marvel.com/x"] = (200, b"ok")
    slept = []
    marveldc.marvel.sleep = slept.append
    marveldc.marvel.get("https://www.marvel.com/x")
    marveldc.marvel.get("https://www.marvel.com/x")
    assert len(slept) == 1 and 55 < slept[0] <= 60


def test_a_refusal_rests_the_site_for_the_retry_after(db_path, web):
    sid = _issue(db_path)
    routes, calls = web
    routes[marveldc.search_url("Test Run", 1.0)] = (429, b"", {"Retry-After": "5400"})
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    assert marveldc.marvel.budget()["paused"] and "marveldc" not in rec.get_issue(sid, 1.0, db_path)["extra"]
    assert marveldc.marvel._state["paused_until"] - datetime.now().timestamp() > 5000
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    assert len(calls) == 1                                                    # resting: nothing sent


def test_the_daily_cap_stops_the_night(db_path, web, monkeypatch):
    _issue(db_path)
    monkeypatch.setattr(marveldc.marvel, "daily_cap", 0)
    assert marveldc.tick(db_path, now=NIGHT, downloads=0) == 0 and web[1] == []


# --- images: bigger-only ----------------------------------------------------------------
def _matched(db_path, routes, main_seed=1, variant_seed=4, main_size=(1200, 1800), variant_size=(800, 1200)):
    sid = _issue(db_path)
    _route_found(routes)
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    routes[f"{CDN}a1/aaa.jpg"] = (200, _jpeg(main_seed, main_size))
    routes[f"{CDN}b2/bbb.jpg"] = (200, _jpeg(variant_seed, variant_size))
    return sid


def test_same_cover_bigger_is_an_upgrade_and_a_new_one_is_a_variant(db_path, web, store):
    routes, _ = web
    sid = _issue(db_path)
    images.store_bytes(f"issue/{sid}/1/file", _jpeg(1, (600, 900)), "file://x", "file", db_path)
    images.store_bytes(images.prh_key(sid, 1.0), _jpeg(1, (400, 600)), "https://prh/x", "prh", db_path)
    _route_found(routes)
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    routes[f"{CDN}a1/aaa.jpg"] = (200, _jpeg(1, (1200, 1800)))
    routes[f"{CDN}b2/bbb.jpg"] = (200, _jpeg(4, (800, 1200)))
    assert marveldc.download_pending(db_path, "2026-10-12") == 2
    imgs = {i["kind"]: i for i in rec.get_issue(sid, 1.0, db_path)["extra"]["marveldc"]["images"]}
    assert imgs["main"]["status"] == "kept" and imgs["main"]["upgrade_of"] == f"issue/{sid}/1/file"
    assert imgs["main"]["old_size"] == [600, 900] and [imgs["main"]["width"], imgs["main"]["height"]] == [1200, 1800]
    assert imgs["variant"]["status"] == "kept" and imgs["variant"]["upgrade_of"] is None
    assert images.get(images.marveldc_key(sid, 1.0), db_path)["source"] == "marveldc"
    assert images.get(images.marveldc_key(sid, 1.0, "500"), db_path)["bytes"] > 0
    e = rec.get_issue(sid, 1.0, db_path)["extra"]["marveldc"]
    assert e["variants"][0]["width"] == 800                                     # sizes filled in once measured
    st = marveldc.status(db_path)
    assert (st["asked"], st["matched"], st["kept"], st["upgrades"], st["pending"]) == (1, 1, 2, 1, 0)


def test_never_stores_anything_smaller_than_what_we_hold(db_path, web, store):
    routes, _ = web
    sid = _issue(db_path)
    images.store_bytes(f"issue/{sid}/1/file", _jpeg(1, (1600, 2400)), "file://x", "file", db_path)
    _route_found(routes, page=issue_page(variants=()))
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    routes[f"{CDN}a1/aaa.jpg"] = (200, _jpeg(1, (1200, 1800)))
    assert marveldc.download_pending(db_path, "2026-10-12") == 0
    img = rec.get_issue(sid, 1.0, db_path)["extra"]["marveldc"]["images"][0]
    assert img["status"] == "smaller" and img["same_as"] == f"issue/{sid}/1/file" and img["old_size"] == [1600, 2400]
    assert images.get(images.marveldc_key(sid, 1.0), db_path) is None


def test_downloads_are_capped_per_tick_and_a_failure_is_recorded(db_path, web, store):
    routes, calls = web
    sid = _issue(db_path)
    _route_found(routes, page=issue_page(variants=("b2/bbb", "c3/ccc", "d4/ddd")))
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    routes[f"{CDN}a1/aaa.jpg"] = (404, b"")
    for k, v in enumerate(("b2/bbb", "c3/ccc", "d4/ddd")):
        routes[f"{CDN}{v}.jpg"] = (200, _jpeg(k + 3, (800, 1200)))
    n = len(calls)
    marveldc.download_pending(db_path, "2026-10-12", limit=2)
    assert len(calls) - n == 2
    st = {i["url"]: i["status"] for i in rec.get_issue(sid, 1.0, db_path)["extra"]["marveldc"]["images"]}
    assert st[f"{CDN}a1/aaa.jpg"] == "failed" and list(st.values()).count("pending") == 2


def test_only_marvels_image_cdn_is_ever_downloaded():
    assert marveldc.full_size("https://evil.example/x/detail.jpg") is None
    assert marveldc.full_size(f"{CDN}5/b0/abc/portrait_uncanny.jpg") == f"{CDN}5/b0/abc.jpg"
    assert marveldc.full_size(f"{CDN}5/b0/abc/standard_incredible.webp") == f"{CDN}5/b0/abc.jpg"


# --- the morning report, Settings, harvest row ------------------------------------------
def test_morning_report(db_path, web, store):
    routes, _ = web
    sid = _issue(db_path)
    images.store_bytes(f"issue/{sid}/1/file", _jpeg(1, (600, 900)), "file://x", "file", db_path)
    miss = _issue(db_path, title="Other Run", store_date="2026-01-01")
    _route_found(routes)
    other = "https://www.marvel.com/comics/issue/9100/other_run_2026_1"
    _route_found(routes, title="Other Run", page=issue_page(upc="75960699999900111"), url=other)
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    marveldc.tick(db_path, now=NIGHT, downloads=0)
    routes[f"{CDN}a1/aaa.jpg"] = (200, _jpeg(1, (1200, 1800)))
    routes[f"{CDN}b2/bbb.jpg"] = (200, _jpeg(4, (800, 1200)))
    marveldc.download_pending(db_path, "2026-10-12")
    r = marveldc.report(db_path)
    assert r["night"] == "2026-10-12" and r["asked"] == 2 and r["matched"] == 1
    assert r["unmatched"] == [{"series_id": miss, "series": "Other Run", "number": 1.0, "page": other,
                               "reason": r["unmatched"][0]["reason"]}] and "UPC differs" in r["unmatched"][0]["reason"]
    kept = {k["kind"]: k for k in r["kept"]}
    assert kept["main"]["key"] == f"issue/{sid}/1/marveldc" and kept["main"]["size"] == [1200, 1800]
    assert kept["main"]["upgrade_of"] == f"issue/{sid}/1/file" and kept["main"]["old_size"] == [600, 900]
    assert kept["variant"]["upgrade_of"] is None
    assert marveldc.tonight(db_path)["kept"] == 2


@pytest.fixture
def wired(tmp_path, monkeypatch):
    dbp = str(tmp_path / "k.db")
    db.init_db(dbp)
    root = tmp_path / "comics"
    root.mkdir()
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(main, "DB_PATH", dbp)
    monkeypatch.setattr(marveldc, "DB_PATH", dbp)
    monkeypatch.setattr(main, "_comics_root", lambda: str(root))
    monkeypatch.setattr(main, "_locg_status", lambda: {"locg_pass_live": False, "locg_topup_waiting": 0})
    return dbp


def test_settings_round_trip(wired):
    cfg = main.get_config()
    assert cfg["marveldc_enabled"] is True and cfg["marveldc_hours"] == "23-7"     # on tonight, by default
    assert cfg["extra_budgets"]["marveldc"]["cap"] == 400
    assert {"requests", "matched", "kept"} <= set(cfg["marveldc_tonight"])
    cfg = main.update_config(main.ConfigRequest(marveldc_enabled="0", marveldc_hours="22 - 6"))
    assert cfg["marveldc_enabled"] is False and cfg["marveldc_hours"] == "22-6"
    with pytest.raises(HTTPException):
        main.update_config(main.ConfigRequest(marveldc_hours="25-7"))
    assert all(i["card"] != "marveldc" for i in main.settings_attention())     # no login: never a red dot
    assert main.app.url_path_for("get_report") == "/api/harvest/marveldc-report"


def test_harvest_progress_has_a_marvel_row(db_path):
    rec.ensure_tables(db_path)
    p = harvest.progress(db_path)
    assert {"asked", "matched", "kept", "upgrades"} <= set(p["marveldc"])
