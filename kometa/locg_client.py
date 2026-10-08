import re
import time
import logging
import threading
from datetime import datetime, timezone

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE = "https://leagueofcomicgeeks.com"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
S3_THUMB = "https://s3.amazonaws.com/comicgeeks/comics/covers/medium-{}.jpg"
S3_LARGE = "https://s3.amazonaws.com/comicgeeks/comics/covers/large-{}.jpg"
_COVER_ID_RE = re.compile(r'covers/(?:large|medium|small)-(\d+)\.jpg')


# One warmed anon session, rebuilt on a timer. Building a fresh impersonated
# session — homepage warm-up and all — on EVERY anon call was paying the
# Cloudflare toll over and over for nothing. The TTL keeps the cookies from
# rotting under a long-running server.
#
# The raw Session lives in the dict (not just inside a closure) so rotation can
# .close() the outgoing one instead of orphaning its connection pool — the old
# closure-only capture meant every replaced session leaked until GC felt like it.
_ANON_SESSION_TTL = 1800  # seconds
_anon_session = {"session": None, "get": None, "ts": 0.0}
# One session shared by every anon caller — and grid renders now fire thumbnail
# fallbacks in PARALLEL. curl_cffi sessions make no thread-safety promises, and
# CF gets twitchy about request bursts anyway. Serialize; politeness is cheap.
#
# The SAME lock also guards the TTL check-and-rebuild in _anon_get_fn (see the
# double-check there). One lock, never nested — the rebuild finishes and releases
# before any closure .get() can grab it, so no ordering games to lose.
_anon_lock = threading.Lock()


# --- your own browser's pass ------------------------------------------------------
# 2026-10-08: LOCG's Cloudflare challenges every non-browser. A real Chrome at
# home passes it and is handed a cf_clearance cookie bound to the house's public
# IP and that browser's exact User-Agent. Kometa sits behind the same IP, so a
# cookie YOU paste from YOUR browser (Settings → LOCG) lets it ride your session
# — at the trickle, for a few hours, until Cloudflare lets it rot. Not a bypass:
# no solver, no headless browser, nothing automated. When it dies we forget it
# and go back to waiting, same as before.
_access_cache = {"ts": 0.0, "val": ("", "")}
_ACCESS_TTL = 30


def _access() -> tuple[str, str]:
    """(cf_clearance, user_agent) from Settings, or ('', '')."""
    now = time.time()
    if now - _access_cache["ts"] > _ACCESS_TTL:
        try:
            from kometa import db
            cfg = db.get_config(db.DB_PATH)
            _access_cache["val"] = ((cfg.get("locg_cf_clearance") or "").strip(),
                                    (cfg.get("locg_user_agent") or "").strip())
        except Exception:
            _access_cache["val"] = ("", "")
        _access_cache["ts"] = now
    return _access_cache["val"]


def _impersonation() -> str:
    """The TLS handshake to wear. Cloudflare binds a pass to the browser that
    earned it, and a Safari User-Agent on a Chrome handshake reads as a lie —
    so match the fingerprint family to the pasted UA. No pass → Chrome, as ever."""
    cookie, ua = _access()
    u = (ua or "").lower()
    if not cookie or not u:
        return "chrome"
    if "firefox/" in u:
        return "firefox"
    if "safari/" in u and "chrome/" not in u and "chromium/" not in u:
        return "safari"
    if "edg/" in u:
        return "edge"
    return "chrome"


def _apply_access(s) -> bool:
    """Put the pasted pass on a curl_cffi session. True if one was applied."""
    cookie, ua = _access()
    if not cookie:
        return False
    try:
        s.cookies.set("cf_clearance", cookie, domain="leagueofcomicgeeks.com")
    except Exception:
        s.headers["Cookie"] = f"cf_clearance={cookie}"
    if ua:
        s.headers["User-Agent"] = ua
    return True


def _forget_access():
    """The pass stopped working — drop it so Settings shows 'expired', not a lie."""
    _access_cache.update(ts=0.0, val=("", ""))
    try:
        from kometa import db
        db.set_config({"locg_cf_clearance": "", "locg_cf_set_at": ""}, db.DB_PATH)
    except Exception:
        pass


# --- backoff ----------------------------------------------------------------------
# LOCG is a site we read without an API or an agreement. When it says "too much"
# — a Cloudflare challenge or a 429 — we STOP: every LOCG call (syncs, matching,
# cover fallbacks) pauses for a few hours instead of retrying into the wall.
# 2026-10-08: challenged from ~03:00; the 06:00 sync then threw 276 more requests
# at it. Persisted in config so a restart doesn't reset the clock.
PAUSE_SECONDS = 3 * 3600
_pause = {"until": None}


class LocgPaused(RuntimeError):
    pass


def _paused_until() -> float:
    if _pause["until"] is None:
        try:
            from kometa import db
            _pause["until"] = float(db.get_config(db.DB_PATH).get("locg_paused_until") or 0)
        except Exception:
            _pause["until"] = 0.0
    return _pause["until"]


def pause_label(until: float) -> str:
    """'2:50pm' in YOUR timezone. The container runs on UTC, so time.localtime
    printed '04:50' for a 2:50pm Brisbane pause."""
    from datetime import datetime
    from kometa.scheduler import TZ
    return datetime.fromtimestamp(until, TZ).strftime("%-I:%M%p").lower()


def _check_paused():
    until = _paused_until()
    if time.time() < until:
        raise LocgPaused(f"LOCG is pausing us until {pause_label(until)} — it refused our requests")


def _note_refusal(r):
    refused = r.status_code == 429 or (
        r.status_code == 403 and (r.headers.get("cf-mitigated") == "challenge"
                                  or "cloudflare" in (r.headers.get("server") or "").lower()))
    if not refused:
        return
    if _access()[0]:
        logger.info("LOCG refused a request carrying your browser's pass — it has expired; forgetting it")
        _forget_access()
    # A refusal while ALREADY paused (a 'Test LOCG now' knock) doesn't push the
    # clock out again — four hopeful clicks were turning 3h into most of a day.
    if time.time() < _paused_until():
        return
    until = time.time() + PAUSE_SECONDS
    _pause["until"] = until
    logger.warning(f"LOCG refused us ({r.status_code}) — pausing ALL LOCG traffic for "
                   f"{PAUSE_SECONDS // 3600}h")
    try:
        from kometa import db
        db.set_config({"locg_paused_until": str(until)}, db.DB_PATH)
    except Exception:
        pass


def probe() -> dict:
    """ONE request to LOCG, ignoring Kometa's own pause — 'is the door open?'.
    This is our throttle, not LOCG's, so checking it is fair. Open: the pause
    is lifted and everything resumes. Refused: the pause simply carries on."""
    from curl_cffi import requests as _cffi
    s = _cffi.Session(impersonate=_impersonation())
    with_pass = _apply_access(s)
    try:
        r = s.get(f"{BASE}/search/ajax_issues", params={"query": "Batman"},
                  headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/"}, timeout=25)
    except Exception as e:
        return {"ok": False, "status": None, "detail": f"no answer: {e}"}
    finally:
        try:
            s.close()
        except Exception:
            pass
    _note_refusal(r)
    if r.status_code == 200:
        _pause["until"] = 0.0
        try:
            from kometa import db
            db.set_config({"locg_paused_until": "0"}, db.DB_PATH)
        except Exception:
            pass
        return {"ok": True, "status": 200, "with_pass": with_pass,
                "detail": "LOCG answered" + (" with your browser's pass" if with_pass else "") + " — pause lifted"}
    return {"ok": False, "status": r.status_code, "with_pass": with_pass,
            "detail": ("your browser's pass has expired — LOCG refused it" if with_pass else
                       "LOCG still refusing (" + str(r.status_code) + ") — staying paused")}


def locg_paused() -> float | None:
    """Epoch seconds the pause lifts, or None if LOCG is open to us."""
    until = _paused_until()
    return until if time.time() < until else None


def _anon_get_fn():
    """A get(url, **kw) callable that gets past Cloudflare without login. cloudscraper's
    TLS fingerprint is blocked from some hosts (e.g. the NAS container — 403 even on the
    homepage); curl_cffi impersonates a real Chrome TLS handshake, which CF accepts.
    Warms the homepage once for the ci_session cookie; the session is cached and
    reused until _ANON_SESSION_TTL expires.

    Rotation is double-checked-locked: the parallel thumbnail fallbacks used to all
    see the TTL expire at once and EACH pay the CF warm-up toll, last writer clobbering
    the rest into leaked connection pools. Now one thread rebuilds under _anon_lock,
    the losers re-check inside the lock and reuse its work. The warm-up GET runs while
    holding the lock — deliberate: it serializes against in-flight closure .get()s,
    which is the politeness we wanted anyway."""
    _check_paused()   # paused = not even the homepage warm-up
    cookie = _access()[0]
    def _fresh():
        # a session built with a different (or no) pass than the one in Settings
        # is stale too — you just pasted one, use it
        return (_anon_session["get"] is not None and time.time() - _anon_session["ts"] <= _ANON_SESSION_TTL
                and _anon_session.get("cookie", "") == cookie)
    # Unlocked fast path — a stale read here just falls through to the lock,
    # where the truth gets re-checked. Fresh-session reads skip the lock entirely.
    if _fresh():
        return _anon_session["get"]
    with _anon_lock:
        # The double-check: whoever won the lock race may have already rebuilt.
        now = time.time()
        if _fresh():
            return _anon_session["get"]
        from curl_cffi import requests as _cffi
        s = _cffi.Session(impersonate=_impersonation())
        _apply_access(s)
        _anon_session["cookie"] = cookie
        try:
            s.get(BASE + "/", timeout=20)
        except Exception:
            pass
        # Close the outgoing session before swapping it in. We hold _anon_lock,
        # and every closure .get() needs it too — so nothing is mid-flight on
        # the old session when it dies.
        old = _anon_session["session"]
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        _anon_session["session"] = s
        if _anon_session["get"] is None:
            # The closure reads the CURRENT session out of the dict under the lock
            # (instead of capturing one), so callers holding a get fn from before a
            # rotation transparently ride the new session — no zombie handles.
            def _get(url, **kw):
                kw.setdefault("timeout", 25)
                _check_paused()
                with _anon_lock:
                    r = _anon_session["session"].get(url, **kw)
                _note_refusal(r)
                return r
            _anon_session["get"] = _get
        _anon_session["ts"] = now
        return _anon_session["get"]

def _parse_search_html(html: str) -> list[dict]:
    """Parse LOCG search AJAX response HTML into [{id, title, publisher, year, cover}].

    A hit is usually a SERIES (/comics/series/{id}/) — but LOCG's ranking sometimes
    hands a one-shot back at the ISSUE level (/comic/{id}/) instead: search 'rogue one'
    and Saw Gerrera comes as a comic, not the series it clearly has. We used to drop
    every non-series <li> on the floor, so those books just vanished. Now we keep them,
    flagged {comic: True, slug}, to be lazily resolved to their parent series on pick."""
    soup = BeautifulSoup(html, "lxml")
    results, seen = [], set()
    # Publisher/date are siblings of the <a> inside the <li>, not children of it.
    for li in soup.find_all("li", class_="media"):
        a = li.find("a", href=re.compile(r"/comics/series/\d+/"))
        is_comic = False
        if a:
            m = re.search(r"/comics/series/(\d+)/", a["href"])
            slug = None
        else:
            a = li.find("a", href=re.compile(r"/comic/\d+/"))
            if not a:
                continue
            m = re.search(r"/comic/(\d+)/([a-z0-9-]+)", a["href"])
            slug = m.group(2) if m else None
            is_comic = True
        if not m:
            continue
        cid = int(m.group(1))
        key = ("c" if is_comic else "s", cid)
        if key in seen:
            continue
        seen.add(key)
        img = li.find("img")
        title_el = li.find(class_="title")
        title_text = title_el.get_text(strip=True) if title_el else ""
        if not title_text:
            title_text = img.get("alt", "").strip() if img else ""
        if not title_text:
            continue
        # Series cover — lazy-loaded into data-src on some pages, plain src on others.
        cover = (img.get("data-src") or img.get("src")) if img else None
        if cover and "covers/" not in cover:  # skip spacers/placeholders
            cover = None
        pub_el = li.find(class_=re.compile(r"\bpublisher\b"))
        date_el = li.find(class_=re.compile(r"\bdate\b"))
        year = None
        if date_el:
            # Series dates lead with the year; comic dates read "Jul 1st, 2026" — so
            # grab the 4-digit year anywhere in the string, not just at the start.
            dm = re.search(r"(\d{4})", date_el.get_text(strip=True))
            if dm:
                year = int(dm.group(1))
        row = {
            "id": cid,
            "title": title_text,
            "publisher": pub_el.get_text(strip=True) if pub_el else "",
            "year": year,
            "cover": cover,
        }
        if is_comic:
            row["comic"] = True
            row["slug"] = slug
        results.append(row)
    return results


_SERIES_LINK_RE = re.compile(r"/comics/series/(\d+)/")


def _comic_series_id(get_fn, comic_id, slug: str):
    """Follow a one-shot (/comic/{id}/{slug}) to its parent series id. The bare
    /comic/{id}/ URL 200s but drops the series link — the slugged URL is required, so
    we carry the slug from the search result rather than guessing it. slug is validated
    to a strict charset so this can't be turned into an arbitrary-path fetch."""
    if not slug or not re.fullmatch(r"[a-z0-9-]+", slug):
        return None
    try:
        r = get_fn(f"{BASE}/comic/{int(comic_id)}/{slug}")
        r.raise_for_status()
        m = _SERIES_LINK_RE.search(r.text)
        return int(m.group(1)) if m else None
    except Exception as e:
        logger.warning(f"LoCG comic {comic_id} series-resolve failed: {e}")
        return None


def resolve_comic_series_anon(comic_id, slug):
    """Anon path: one-shot comic id + slug -> parent series id (or None)."""
    return _comic_series_id(_anon_get_fn(), comic_id, slug)


def search_series_strict(title: str) -> list[dict]:
    """Like search_series_anon, but a failure RAISES. For callers that must tell
    'LOCG has nothing' apart from 'LOCG didn't answer' — the shelf matcher, which
    once read a Cloudflare 403 as 'no match' and handed you a decision for it."""
    r = _anon_get_fn()(
        f"{BASE}/search/ajax_issues",
        params={"query": title},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/"},
    )
    r.raise_for_status()
    return _parse_search_html(r.text)


def search_series_anon(title: str) -> list[dict]:
    """Search LOCG without credentials — the search endpoint works without auth."""
    try:
        return search_series_strict(title)
    except Exception as e:
        logger.warning(f"LoCG anon search({title!r}) failed: {e}")
        return []


_ELLIPSIS_RE = re.compile(r"\s*(?:\.{2,}|…)\s*$")


def is_truncated(title: str) -> bool:
    return bool(_ELLIPSIS_RE.search(title or ""))


def full_series_title_anon(series_id: int, truncated: str) -> str | None:
    """LOCG clips long ISSUE-level titles ('…Training Day... #1') but its SERIES rows
    carry the whole name. Search on the clipped stem and take the series row whose
    id is ours. None if LOCG doesn't hand that row back — caller strips the dots."""
    stem = _ELLIPSIS_RE.sub("", truncated)
    for r in search_series_anon(stem):
        if not r.get("comic") and r["id"] == int(series_id) and not is_truncated(r["title"]):
            return r["title"]
    return None


def _parse_num(title: str) -> float | None:
    m = re.search(r'#(\d+(?:\.\d+)?)', title)
    if m:
        return float(m.group(1))
    m = re.search(r'\bVol\.?\s*(\d+)\b', title, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def find_series_id_anon(title: str, year: int | None = None) -> int | None:
    """Best-matching LOCG series id for a title+year, keyless. The search endpoint
    ignores auth (same /search/ajax_issues the wizard already hits anonymously),
    so this is exactly what the old authed find_series_id did minus the login."""
    results = search_series_anon(title)
    if not results:
        return None
    title_l = title.lower()
    _punct = re.compile(r"[\-\–\—\:\,\.\!\?\'\"]+")
    title_norm = _punct.sub(" ", title_l).split()

    def score(r):
        s = 0
        r_title = r["title"].lower()
        r_norm = _punct.sub(" ", r_title).split()
        if r_title == title_l:
            s += 10
        elif title_l in r_title or r_title in title_l:
            s += 4
        elif title_norm == r_norm:
            s += 9
        elif all(w in r_norm for w in title_norm):
            s += 3
        if year and r.get("year") == year:
            s += 5
        return s

    results.sort(key=score, reverse=True)
    best = results[0]
    return best["id"] if score(best) >= 4 else None


def _get_issues_with_get(series_id: int, get_fn) -> list[dict]:
    """Shared issue-list scraping. get_fn(url, **kwargs) must return a requests-like
    Response. Works with both the authed session and an anonymous cloudscraper."""
    try:
        r = get_fn(
            f"{BASE}/comic/get_comics",
            params={
                "list": "series",
                "series_id": series_id,
                "view": "thumbs",
                "format[]": 1,
                "order": "asc",
            },
        )
        data = r.json()
        soup = BeautifulSoup(data["list"], "lxml")
        issues = []
        for li in soup.find_all("li"):
            title_el = li.find(class_="title")
            date_el = li.find(class_="date")
            img_el = li.find("img")
            if not title_el:
                continue
            num = _parse_num(title_el.text.strip())
            if num is None:
                continue
            store_date = None
            if date_el:
                ts = date_el.get("data-date")
                if ts:
                    try:
                        store_date = datetime.fromtimestamp(
                            int(ts), tz=timezone.utc
                        ).strftime("%Y-%m-%d")
                    except (ValueError, OverflowError, OSError):
                        pass
            cover = img_el.get("data-src") if img_el else None
            if cover and "covers/" not in cover:
                # LOCG's no-art placeholder (/assets/images/no-cover-*.jpg) — storing
                # it poisons metron_image with a relative URL that can never load
                cover = None
            # The issue id lives in the <a href="/comic/{id}/{slug}"> on every row,
            # art or no art. The old cover-URL parse silently dropped the id for any
            # issue whose cover wasn't posted yet — which is exactly the issues
            # (upcoming) where the variants tab matters most. Keep it as fallback.
            locg_issue_id = None
            a_el = li.find("a", href=re.compile(r"/comic/(\d+)/"))
            if a_el:
                m = re.search(r"/comic/(\d+)/", a_el["href"])
                if m:
                    locg_issue_id = m.group(1)
            if not locg_issue_id and cover:
                m = _COVER_ID_RE.search(cover)
                if m:
                    locg_issue_id = m.group(1)
            issues.append({"number": num, "store_date": store_date, "cover": cover, "locg_issue_id": locg_issue_id})
        time.sleep(0.3)
        return issues
    except Exception as e:
        logger.warning(f"LoCG get_issues({series_id}) failed: {e}")
        return []


def get_issues_anon(series_id: int) -> list[dict]:
    """Fetch a series' issue list with no login, via cloudscraper. This is what
    lets sync build an issue list — and therefore detect missing issues — for a
    keyless, Komga-less install. Mirrors search_series_anon / fetch_variants."""
    return _get_issues_with_get(series_id, _anon_get_fn())


# LOCG format codes on the series-issue endpoint. 1=singles, 2=variants are used
# elsewhere; 3=TPB and 4=HC are the collected editions. Verified against East of
# West (series 102652): format 3 returned all 10 trades + the compendium, format
# 4 the Apocalypse hardcovers.
_FMT_TPB, _FMT_HC = 3, 4
# "Vol. 1", "Vol 1", "Volume 1" — and ranges: "Vol. 1 - 6", "Vol 1-6"
_VOL_RANGE_RE = re.compile(r'\bvol(?:ume|\.)?\s*(\d+)\s*[-–—]\s*(\d+)', re.I)
_VOL_RE = re.compile(r'\bvol(?:ume|\.)?\s*(\d+)', re.I)


def _get_trades_with_get(series_id: int, get_fn) -> list[dict]:
    """Scrape a series' collected editions (TPB + HC). Returns rows of
    {format, title, locg_id, vol, vol_range, is_variant}. NOTE: LOCG does not
    publish the issue range a trade collects — only its name/volume. The caller
    fills the actual #X-Y mapping from Metron (when keyed) or by asking the user."""
    out = []
    for fmt, label in ((_FMT_TPB, "TPB"), (_FMT_HC, "HC")):
        try:
            r = get_fn(
                f"{BASE}/comic/get_comics",
                params={"list": "series", "series_id": series_id,
                        "view": "thumbs", "format[]": fmt, "order": "asc"},
            )
            soup = BeautifulSoup(r.json()["list"], "lxml")
        except Exception as e:
            logger.warning(f"LoCG get_trades({series_id}, fmt={fmt}) failed: {e}")
            continue
        for li in soup.find_all("li"):
            title_el = li.find(class_="title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            a_el = li.find("a", href=re.compile(r"/comic/(\d+)/"))
            locg_id = None
            if a_el:
                m = re.search(r"/comic/(\d+)/", a_el["href"])
                if m:
                    locg_id = m.group(1)
            # Same cover handling as the issue scraper: real art lives under
            # /covers/, anything else is LOCG's no-art placeholder.
            img_el = li.find("img")
            cover = img_el.get("data-src") if img_el else None
            if cover and "covers/" not in cover:
                cover = None
            if not cover and locg_id:
                cover = S3_THUMB.format(locg_id)
            # A title with extra words after the format ("DCBS ... Variant",
            # "Con Exc Var", "3rd Printing") is a variant printing of the same
            # trade — fold them. No word boundaries: LOCG mashes the format in
            # ("HCDCBS", "TP3rd"), so \bDCBS\b would miss exactly those.
            is_variant = bool(re.search(r"variant|dcbs|\bexc\b|exclusive|printing", title, re.I))
            rng = _VOL_RANGE_RE.search(title)
            vol = _VOL_RE.search(title)
            out.append({
                "format": label,
                "title": title,
                "locg_id": locg_id,
                "cover": cover,
                "vol": int(vol.group(1)) if vol and not rng else None,
                "vol_range": [int(rng.group(1)), int(rng.group(2))] if rng else None,
                "is_variant": is_variant,
            })
        time.sleep(0.3)
    return out


def get_trades_anon(series_id: int) -> list[dict]:
    """Keyless collected-edition discovery. Mirrors get_issues_anon."""
    return _get_trades_with_get(series_id, _anon_get_fn())


def select_editions(trades: list[dict]) -> list[dict]:
    """The editions worth showing: drop variant printings, then keep ONE per
    (volume, format) so a vol's reprints/year-editions collapse to a single tile
    while TP and HC of the same volume stay distinct. No-volume editions
    (compendiums, box sets, deluxe HCs) are unique products — all kept. Order is
    preserved, so LOCG's base printing (listed first) wins over later reprints."""
    out, seen = [], set()
    for t in trades:
        if t.get("is_variant"):
            continue
        if t.get("vol") is not None:
            key = (t["vol"], t["format"])
            if key in seen:
                continue
            seen.add(key)
        out.append(t)
    return out


# Variant lists barely change, but they DO change — fresh issues grow new
# variants for weeks after release. Cache hard enough to make modal reopens
# free, short enough that a new variant shows up same-day.
_VARIANT_CACHE_TTL = 6 * 3600  # seconds
_variant_cache: dict[str, tuple[float, dict]] = {}


def _fetch_variants_with_get(locg_issue_id: str, get_fn) -> dict:
    """Shared variant-scraping logic. get_fn(url, **kwargs) must return a
    requests.Response. Results are cached per issue id for _VARIANT_CACHE_TTL."""
    cached = _variant_cache.get(locg_issue_id)
    if cached and time.time() - cached[0] < _VARIANT_CACHE_TTL:
        return cached[1]

    url = f"{BASE}/comic/{locg_issue_id}/comic"
    r = get_fn(url, headers={'Referer': BASE + '/', 'Accept': 'text/html'})
    if hasattr(r, 'raise_for_status'):
        r.raise_for_status()

    soup = BeautifulSoup(r.text, 'html.parser')
    title_tag = soup.find('h1') or soup.find('title')
    raw_title = title_tag.get_text(strip=True) if title_tag else ''
    issue_title = re.split(r'[\n|]', raw_title)[0].strip()

    covers = [{'id': locg_issue_id, 'name': 'Cover A (Main)',
               'thumb': S3_THUMB.format(locg_issue_id),
               'large': S3_LARGE.format(locg_issue_id)}]

    variant_div = soup.find(class_='variant-cover-list')
    if variant_div:
        seen = set()
        for a in variant_div.find_all('a', href=re.compile(r'\?variant=')):
            m = re.search(r'\?variant=(\d+)', a.get('href', ''))
            if not m:
                continue
            vid = m.group(1)
            if vid in seen:
                continue
            seen.add(vid)
            img = a.find('img')
            name = img.get('alt', f'Variant {vid}') if img else f'Variant {vid}'
            name = re.sub(rf'^{re.escape(issue_title)}\s*', '', name).strip() or f'Variant {vid}'
            covers.append({'id': vid, 'name': name,
                           'thumb': S3_THUMB.format(vid),
                           'large': S3_LARGE.format(vid)})

    result = {'title': issue_title, 'covers': covers}
    _variant_cache[locg_issue_id] = (time.time(), result)
    return result


def fetch_variants(locg_issue_id: str) -> dict:
    """Fetch variant covers for an issue, keyless."""
    return _fetch_variants_with_get(locg_issue_id, _anon_get_fn())


# LOCG's own chrome that has no business being an issue description. The login
# modal blurb is 113 chars, which beat the "longest paragraph" fallback every
# time a real solicit ran short -- so half the library was advertising a signup
# form instead of telling you what the comic is about. Never again.
_BOILERPLATE = (
    "already have an account",
    "league of comic geeks",
    "could not find",
    "sign up",
    "sign in",
    "log in",
    "create an account",
)


def _is_site_boilerplate(text: str) -> bool:
    t = (text or "").lower()
    return any(phrase in t for phrase in _BOILERPLATE)


def _parse_issue_details(html: str) -> dict:
    """Pull description + credits (with roles + people ids) from a LOCG issue page.
    Returns {"desc": str, "credits": [{"role","name","people_id"}]}. Powers the
    issue Details tab; people_id is also the taste signal the external
    kometa-recommend project consumes from this cache."""
    soup = BeautifulSoup(html, "lxml")
    desc = ""
    for sel in (".listing-description", "[itemprop=description]", "div.copy",
                "section.copy", "div.comic-description"):
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            desc = el.get_text(" ", strip=True)
            break
    if not desc:  # fallback: the longest real paragraph
        ps = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        ps = [t for t in ps if len(t) > 60 and not _is_site_boilerplate(t)]
        desc = max(ps, key=len) if ps else ""
    if _is_site_boilerplate(desc):
        desc = ""

    credits, seen = [], set()
    for name_el in soup.select(".name"):
        a = name_el.find("a", href=re.compile(r"/people/(\d+)"))
        if not a:
            continue
        name = a.get_text(strip=True)
        m = re.search(r"/people/(\d+)/([^/?#\"]+)", a.get("href", ""))
        pid = m.group(1) if m else None
        slug = m.group(2) if m else None
        wrap = name_el.parent
        role_el = wrap.find(class_="role") if wrap else None
        role = role_el.get_text(strip=True) if role_el else "Other"
        key = (name, role)
        if name and key not in seen:
            seen.add(key)
            credits.append({"role": role, "name": name, "people_id": pid, "people_slug": slug})
    return {"desc": desc, "credits": credits}


def get_issue_details_anon(comic_id) -> dict:
    """Fetch an issue's description + credits from LOCG with no login. Same /comic/
    {id}/comic page the variant scraper uses. Keyless — works for any install."""
    g = _anon_get_fn()
    r = g(f"{BASE}/comic/{comic_id}/comic",
          headers={'Referer': BASE + '/', 'Accept': 'text/html'})
    r.raise_for_status()
    return _parse_issue_details(r.text)
