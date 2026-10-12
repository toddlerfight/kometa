"""Marvel.com's print-size covers, overnight, a few at a time (enrichment 1i-e).

Marvel's CDN holds the masters: a cover path minus its size suffix
('/portrait_uncanny', '/detail') is the full image, 2750x4175 for a main and
1821x2800 for a variant (docs/research/comic-sources-2026-10-12.md). The issue
page carries a JSON blob with the main image, every variant image and the UPC.
Disney's and DC's terms forbid automated access; the user chose to run this
anyway ("build it so it can run overnight, see what it brings back in the
morning", 2026-10-12), so it's built to be the slowest worker in the app:

- NIGHT ONLY: inside config 'marveldc_hours' (default '23-7', local time,
  wrapping midnight); outside it a tick returns at once. 'marveldc_enabled'
  is the Settings toggle (absent = on).
- One polite.Limiter per site: a minute between page requests, 400 a day, an
  hour's rest after a 403/429/503 (Retry-After honoured). The image CDN has its
  own gentler limiter (5 s gap). At most PAGES_PER_TICK page requests a tick.
- A page is ACCEPTED ONLY when its UPC equals the record's (Metron's 'upc'):
  digits only, both sides; no UPC on the page, or a different one, and nothing
  is taken. The outcome goes to extra['marveldc'] so nothing is asked twice.

How a page is found (probed 2026-10-12, 4 requests): Marvel's site search has a
JSON endpoint, /v1/pagination/search_cards?query=…&content_type=comics, whose
results include issue cards headlined 'Midnight Spider-Man (2026) #1' with the
issue page's URL. One search + one page per issue. CloudFront refuses a bare
'Kometa/1.0' User-Agent (403); the 'Mozilla/5.0 (compatible; Kometa/1.0; …)'
form — the convention crawlers use to name themselves — is let through.

DC.com is NOT asked. Its issue pages (probed 2026-10-12, two of them) carry no
UPC anywhere — not in the page, not in the data blob behind it — and an empty
variant list, so under the UPC rule nothing there could ever be accepted.
The DC limiter exists so the day it gains one, it's one constant away.

Every downloaded image is dHashed against the issue's stored covers (main,
the file's page 1, ComicVine, PRH, catalogue variants, Fandom). A close match
is the SAME cover: kept only when it has more pixels than the best copy we hold
('upgrade_of' records which one); otherwise it's a new variant and stored.
Nothing smaller than what we hold is ever stored.
"""
import io
import json
import logging
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

import kometa.db as db
from kometa.naming import norm_key
from kometa.polite import Limiter, SourceUnavailable, enabled

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH

MARVEL = "https://www.marvel.com"
CDN_PREFIX = "https://cdn.marvel.com/u/prod/marvel/i/mg/"
# CloudFront 403s a bare 'Kometa/1.0 (…)' (probed 2026-10-12); this names us just as plainly
USER_AGENT = "Mozilla/5.0 (compatible; Kometa/1.0; personal comic library, nightly cover lookup)"
DEFAULT_HOURS = "23-7"
PAGES_PER_TICK = 3            # site requests per 10-min tick: one search + one or two issue pages
MAX_PAGES_PER_ISSUE = 2       # same-name runs a year apart: the UPC decides which, if either
DOWNLOADS_PER_TICK = 4        # CDN images per tick
RECHECK_DAYS = 60             # an unmatched issue is asked again after this; a matched one never
MAX_BYTES = 40 * 1024 * 1024
SHAPE_REST_S = 6 * 3600       # the search/page no longer reads as expected: rest, don't mis-record
DC_PAGES_CARRY_UPC = False    # probed 2026-10-12: they don't, so DC issues are never asked

marvel = Limiter("Marvel.com", min_interval_s=60.0, daily_cap=400, refused_pause_s=3600,
                 timeout_s=30, user_agent=USER_AGENT)
dc = Limiter("DC.com", min_interval_s=60.0, daily_cap=400, refused_pause_s=3600,
             timeout_s=30, user_agent=USER_AGENT)
cdn = Limiter("Marvel CDN", min_interval_s=5.0, daily_cap=400, refused_pause_s=3600,
              timeout_s=90, user_agent=USER_AGENT)


def _stop_aware_sleep(seconds: float):
    from kometa.activity import STOPPING
    STOPPING.wait(seconds)


for _lim in (marvel, dc, cdn):
    _lim.sleep = _stop_aware_sleep      # a deploy doesn't wait out a 60 s gap


# --- the night window ------------------------------------------------------------------
def parse_hours(spec: str | None) -> tuple[int, int]:
    """'23-7' → (23, 7). Anything unreadable falls back to the default window."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", str(spec or ""))
    if m and 0 <= int(m[1]) <= 23 and 0 <= int(m[2]) <= 23:
        return int(m[1]), int(m[2])
    return 23, 7                              # DEFAULT_HOURS


def valid_hours(spec: str | None) -> bool:
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", str(spec or ""))
    return bool(m and 0 <= int(m[1]) <= 23 and 0 <= int(m[2]) <= 23)


def in_window(hour: int, start: int, end: int) -> bool:
    """Start inclusive, end exclusive; wraps midnight when start > end. start == end = all day."""
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def night_of(now: datetime, start: int, end: int) -> str:
    """The date the window opened: 02:00 on the 13th belongs to the night of the 12th."""
    d = now.date()
    if start > end and now.hour < end:
        d -= timedelta(days=1)
    return d.isoformat()


def _hours(path) -> tuple[int, int]:
    return parse_hours(db.get_config(path).get("marveldc_hours") or DEFAULT_HOURS)


def _now_local() -> datetime:
    from kometa.scheduler import TZ
    return datetime.now(TZ)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --- UPCs -------------------------------------------------------------------------------
def norm_upc(v) -> str:
    """Digits only; a 13/18-digit EAN form of a UPC-A loses its leading 0."""
    d = re.sub(r"\D", "", str(v or ""))
    if len(d) in (13, 18) and d.startswith("0"):
        d = d[1:]
    return d


def upc_matches(ours, theirs) -> bool:
    """The same barcode, exactly. A 12-digit base against a 17-digit one with its
    add-on is NOT a match: the base is the run's, the add-on picks the issue and
    the cover — accepting on the base alone would take any issue of the run."""
    a, b = norm_upc(ours), norm_upc(theirs)
    return len(a) >= 12 and a == b


# --- Marvel: search, page, images -------------------------------------------------------
_HEADLINE = re.compile(r"^(?P<t>.+?)\s*\((?P<y>\d{4})\)\s*#(?P<n>\d+(?:\.\d+)?)\s*$")


def _title_key(t: str) -> str:
    return norm_key(re.sub(r"^the\s+", "", (t or "").strip(), flags=re.I))


def search_url(title: str, number: float) -> str:
    q = urllib.parse.urlencode({"offset": 0, "limit": 20, "query": f"{title} #{number:g}",
                                "content_type": "comics"})
    return f"{MARVEL}/v1/pagination/search_cards?{q}"


def parse_search(data) -> list[dict] | None:
    """Issue cards from a search answer: [{title, year, number, url}]. None when
    the answer isn't the shape we know (so nothing gets recorded off it)."""
    try:
        results = data["data"]["results"]
    except (KeyError, TypeError):
        return None
    if not isinstance(results, list):
        return None
    out = []
    for r in results:
        if not isinstance(r, dict) or r.get("content_type") != "comic-issue":
            continue
        m = _HEADLINE.match((r.get("headline") or "").strip())
        url = ((r.get("link") or {}).get("link") or "").strip()
        if not m or not url.startswith(MARVEL + "/comics/issue/"):
            continue
        out.append({"title": m["t"], "year": int(m["y"]), "number": float(m["n"]), "url": url})
    return out


def pick_pages(cards: list[dict], title: str, year: int | None, number: float) -> list[str]:
    """Issue pages worth opening: same run name (a leading 'The' ignored), same
    number, start year within one of ours when we know it; nearest year first."""
    want = _title_key(title)
    hits = [c for c in cards if c["number"] == float(number) and _title_key(c["title"]) == want
            and (not year or abs(c["year"] - year) <= 1)]
    hits.sort(key=lambda c: abs(c["year"] - year) if year else 0)
    return list(dict.fromkeys(c["url"] for c in hits))[:MAX_PAGES_PER_ISSUE]


def _issue_details(html: str) -> dict | None:
    """The page's own data: window['__marvel-fitt__'] = {...page.content.issueDetails...}."""
    for m in re.finditer(r"window\[['\"]__marvel-fitt__['\"]\]\s*=\s*", html):
        try:
            blob, _ = json.JSONDecoder().raw_decode(html[m.end():])
        except ValueError:
            continue
        stack = [blob]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                if isinstance(o.get("issueDetails"), dict):
                    return o["issueDetails"]
                stack.extend(o.values())
            elif isinstance(o, list):
                stack.extend(o)
    return None


def full_size(url: str | None) -> str | None:
    """A cdn.marvel.com cover at any size → the master (the size suffix dropped).
    Anything not on Marvel's image CDN → None: we only ever download from there."""
    if not url or not url.startswith(CDN_PREFIX):
        return None
    m = re.match(r"^(.+?)/(?:portrait|standard|landscape)_[a-z]+\.(jpg|jpeg|png|webp)$|^(.+?)/(?:detail|clean)\.(jpg|jpeg|png|webp)$", url)
    if not m:
        return url
    base, ext = (m[1], m[2]) if m[1] else (m[3], m[4])
    return f"{base}.{'jpg' if ext == 'webp' else ext}"


def parse_issue_page(html: str) -> dict | None:
    """{title, upc, main, variants:[{id, url}]} or None when the page has no issue data."""
    d = _issue_details(html)
    if not d:
        return None
    upc = None
    for row in ((d.get("details") or {}).get("info") or []):
        if isinstance(row, dict) and str(row.get("title") or "").strip().upper() == "UPC":
            upc = str(row.get("value") or "").strip() or None
    variants = []
    for v in d.get("variants") or []:
        u = full_size((v or {}).get("image"))
        if u:
            variants.append({"id": str(v.get("id") or len(variants) + 1), "url": u})
    return {"title": d.get("title"), "upc": upc, "main": full_size(d.get("image") or d.get("imageSmall")),
            "variants": variants}


class _OutOfRequests(Exception):
    pass


def ask(c: dict, spend, night: str, get=None) -> dict:
    """One issue's extra['marveldc'] entry. Raises _OutOfRequests when the tick's
    page requests run out before a search (nothing recorded), SourceUnavailable
    when Marvel is resting/refusing or the answers stopped reading as expected."""
    get = get or marvel.get
    if not spend():
        raise _OutOfRequests()
    r = get(search_url(c["title"], c["number"]))
    try:
        cards = parse_search(r.json()) if r.status_code == 200 else None
    except ValueError:
        cards = None
    if cards is None:
        marvel.rest(SHAPE_REST_S)
        raise SourceUnavailable(f"Marvel search answered {r.status_code} in a shape we don't read")
    entry = {"site": "marvel", "page": None, "at": _stamp(), "night": night, "matched": False,
             "main": None, "variants": [], "images": []}
    pages = pick_pages(cards, c["title"], c.get("year"), c["number"])
    if not pages:
        entry["reason"] = "not found in Marvel's search"
        return entry
    reasons = []
    for i, url in enumerate(pages):
        if not spend():
            break                                   # no requests left (only ever the runner-up: a tick starts an issue with 2)
        p = get(url)
        entry["page"] = url
        if p.status_code != 200:
            reasons.append(f"page answered {p.status_code}")
            continue
        page = parse_issue_page(p.text)
        if page is None:
            marvel.rest(SHAPE_REST_S)
            raise SourceUnavailable("Marvel issue page has no issue data we can read")
        entry["page_upc"] = page["upc"]
        if not page["upc"]:
            reasons.append("no UPC on the page")
            continue
        if not upc_matches(c["upc"], page["upc"]):
            reasons.append(f"UPC differs ({norm_upc(page['upc'])} vs ours {norm_upc(c['upc'])})")
            continue
        entry.update(matched=True, main=page["main"], reason=None,
                     variants=[{"id": v["id"], "url": v["url"], "width": None, "height": None}
                               for v in page["variants"]])
        sid, n = c["sid"], c["number"]
        from kometa import images
        if page["main"]:
            entry["images"].append({"url": page["main"], "key": images.marveldc_key(sid, n), "kind": "main",
                                    "status": "pending"})
        for v in page["variants"]:
            entry["images"].append({"url": v["url"], "key": images.marveldc_key(sid, n, v["id"]),
                                    "kind": "variant", "status": "pending"})
        return entry
    entry["reason"] = "; ".join(reasons) or "no page could be read"
    return entry


# --- candidates -------------------------------------------------------------------------
def _recent(entry: dict) -> bool:
    try:
        at = datetime.strptime(str(entry.get("at"))[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - at < timedelta(days=RECHECK_DAYS)


def candidates(path=None, limit: int = 20) -> list[dict]:
    """Owned Marvel issues whose record carries a UPC and that Marvel.com hasn't
    been asked about (a match: never again; a miss: not within RECHECK_DAYS).
    Pull list first, then the newest store date. DC runs are skipped: their
    pages carry no UPC to check against (DC_PAGES_CARRY_UPC)."""
    path = path or DB_PATH
    from kometa import fandom_client, fandom_covers, record
    record.ensure_tables(path)
    with db._connect(path) as conn:
        rows = conn.execute("""
            SELECT s.id AS sid, s.title, s.publisher, s.year_began, s.metron_extra_ids, i.number, r.extra_json
            FROM issue_status i JOIN tracked_series s ON s.id = i.tracked_series_id
            JOIN issue_record r ON r.tracked_series_id = i.tracked_series_id AND r.number = i.number
            WHERE i.owned = 1 AND r.extra_json LIKE '%"upc"%' AND (s.kind IS NULL OR s.kind != 'arc')
            ORDER BY s.on_pull_list DESC, (i.store_date IS NULL), i.store_date DESC, i.number DESC""").fetchall()
    out = []
    for r in rows:
        try:
            extra = json.loads(r["extra_json"] or "{}") or {}
        except (TypeError, ValueError):
            continue
        site = fandom_client.wiki_for(r["publisher"] or extra.get("publisher"))
        if site != "marvel" and not (site == "dc" and DC_PAGES_CARRY_UPC):
            continue
        if not norm_upc(extra.get("upc")):
            continue
        prev = extra.get("marveldc")
        if isinstance(prev, dict) and (prev.get("matched") or _recent(prev)):
            continue
        title = fandom_covers.run_title(dict(r), extra)
        if not title:
            continue
        from_run = title != (r["title"] or "")
        out.append({"sid": r["sid"], "number": r["number"], "title": title, "upc": extra["upc"],
                    "year": None if from_run else r["year_began"], "site": site})
        if len(out) >= limit:
            break
    return out


# --- images: hash against what the issue holds, keep only what's bigger ----------------
def _pixels(data: bytes) -> tuple[int, int] | None:
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def held_covers(series_id: int, number: float, path=None) -> list[dict]:
    """The issue's stored covers from every OTHER source: {key, dhash, width, height}."""
    from kometa import coververify, images
    from PIL import Image
    path = path or DB_PATH
    images.ensure_tables(path)
    prefix = f"issue/{int(series_id)}/{number:g}/"
    with db._connect(path) as conn:
        rows = conn.execute("SELECT key, path FROM image_record WHERE key LIKE ? AND path IS NOT NULL",
                            (prefix + "%",)).fetchall()
    out = []
    for r in rows:
        k = r["key"]
        if k.startswith(prefix + "marveldc") or not os.path.exists(r["path"]):
            continue
        try:
            with open(r["path"], "rb") as f:
                data = f.read()
            with Image.open(io.BytesIO(data)) as im:
                w, h = im.size
        except Exception:
            continue
        dh = coververify.dhash(data)
        if dh is not None:
            out.append({"key": k, "dhash": dh, "fine": coververify.fine(data), "width": w, "height": h})
    return out


def judge(series_id: int, number: float, data: bytes, path=None) -> dict:
    """What to do with one downloaded image: {'keep': bool, 'width', 'height',
    'upgrade_of': key|None, 'old_size': [w, h]|None, 'why'}. A close dHash match
    is the same cover — kept only when it has more pixels than the best copy held."""
    from kometa import coververify
    size = _pixels(data)
    h = coververify.dhash(data) if size else None
    if not size or h is None:
        return {"keep": False, "why": "not an image"}
    w, ht = size
    f = coververify.fine(data)
    # the SAME image only (coververify.same_image): a textless virgin or another
    # printing of this art is a different cover — kept as new, never as "not bigger"
    same = [c for c in held_covers(series_id, number, path) if coververify.same_image(f, c["fine"])]
    if not same:
        return {"keep": True, "width": w, "height": ht, "upgrade_of": None, "old_size": None, "why": "new cover"}
    best = max(same, key=lambda c: c["width"] * c["height"])
    old = [best["width"], best["height"]]
    if w * ht > best["width"] * best["height"]:
        return {"keep": True, "width": w, "height": ht, "upgrade_of": best["key"], "old_size": old, "why": "bigger"}
    return {"keep": False, "width": w, "height": ht, "upgrade_of": None, "same_as": best["key"], "old_size": old,
            "why": "not bigger than the copy we hold"}


REJUDGE_FLAG = "marveldc_rejudged_fine"


def rejudge_once(path=None) -> int:
    """Once: images skipped as 'not bigger' under the old loose 8×8 test (before
    coververify.same_image) go back in the download line — a textless virgin was
    being called the same cover as its logo printing. Returns how many."""
    path = path or DB_PATH
    if db.get_config(path).get(REJUDGE_FLAG) == "1":
        return 0
    n = 0
    from kometa import record
    with db._connect(path) as conn:
        rows = conn.execute("""SELECT tracked_series_id AS sid, number, extra_json FROM issue_record
            WHERE extra_json LIKE '%"marveldc": {%' AND extra_json LIKE '%"status": "smaller"%'""").fetchall()
    for r in rows:
        entry = json.loads(r["extra_json"] or "{}").get("marveldc") or {}
        again = [i for i in entry.get("images") or [] if i.get("status") == "smaller"]
        for img in again:
            img.update(status="pending", why="re-judged with the strict same-image test")
        if again:
            record.merge_extra(r["sid"], r["number"], {"marveldc": entry}, path)
            n += len(again)
    db.set_config({REJUDGE_FLAG: "1"}, path)
    if n:
        logger.info(f"Marvel.com: {n} skipped images back in line for the strict same-image test")
    return n


def _pending_rows(path) -> list:
    with db._connect(path) as conn:
        return conn.execute("""SELECT r.tracked_series_id AS sid, r.number, r.extra_json FROM issue_record r
            JOIN tracked_series s ON s.id = r.tracked_series_id
            WHERE r.extra_json LIKE '%"marveldc": {%' AND r.extra_json LIKE '%"status": "pending"%'
            ORDER BY s.on_pull_list DESC, r.tracked_series_id, r.number""").fetchall()


def download_pending(path=None, night: str | None = None, limit: int = DOWNLOADS_PER_TICK, get=None,
                     sleep=None) -> int:
    """Up to `limit` of the images matched pages listed, through the CDN's limiter.
    Returns covers kept. Stops quietly when the CDN rests or the day is spent."""
    path = path or DB_PATH
    get = get or cdn.get
    from kometa import images, record
    from kometa.activity import yield_to_reader
    kept = tried = 0
    for r in _pending_rows(path):
        extra = json.loads(r["extra_json"] or "{}")
        entry = extra.get("marveldc") or {}
        for img in entry.get("images") or []:
            if img.get("status") != "pending":
                continue
            if tried >= limit:
                return kept
            if sleep:
                yield_to_reader(sleep)
            else:
                yield_to_reader()
            try:
                resp = get(img["url"])
            except SourceUnavailable as e:
                logger.info(f"Marvel CDN stopped this tick: {e}")
                return kept
            tried += 1
            data = resp.content if resp.status_code == 200 else None
            stamp = {"at": _stamp(), "night": night}
            if not data or len(data) > MAX_BYTES:
                img.update(status="failed", why=f"answered {resp.status_code}" if not data else "too big", **stamp)
            else:
                v = judge(r["sid"], r["number"], data, path)
                img.update(width=v.get("width"), height=v.get("height"), bytes=len(data),
                           upgrade_of=v.get("upgrade_of"), old_size=v.get("old_size"), why=v["why"], **stamp)
                if v.get("same_as"):
                    img["same_as"] = v["same_as"]
                if v["keep"]:
                    images.store_bytes(img["key"], data, img["url"], "marveldc", path)
                    try:                                    # shown in place of the smaller same cover
                        from kometa import coverupgrade
                        coverupgrade.link(r["sid"], r["number"], img["key"], data, path)
                    except Exception as e:
                        logger.info(f"Cover upgrade link for {img['key']} skipped: {e}")
                    img["status"] = "kept"
                    kept += 1
                else:
                    img["status"] = "smaller" if v.get("width") else "failed"
                for var in entry.get("variants") or []:
                    if var.get("url") == img["url"]:
                        var.update(width=v.get("width"), height=v.get("height"))
            record.merge_extra(r["sid"], r["number"], {"marveldc": entry}, path)
    return kept


# --- the tick ---------------------------------------------------------------------------
def tick(path=None, now: datetime | None = None, budget: int = PAGES_PER_TICK,
         downloads: int = DOWNLOADS_PER_TICK, get=None, cdn_get=None) -> int:
    """Scheduler tick (every 10 min): outside the night window, switched off, or
    with Marvel resting/spent, nothing. Otherwise a few issues asked (at most
    `budget` page requests) and a few matched images downloaded. Returns issues
    asked + covers kept."""
    path = path or DB_PATH
    if not enabled("marveldc_enabled", path):
        return 0
    now = now or _now_local()
    start, end = _hours(path)
    if not in_window(now.hour, start, end):
        return 0
    night = night_of(now, start, end)
    from kometa import record
    from kometa.activity import STOPPING
    left = [budget]

    def spend() -> bool:
        if left[0] <= 0:
            return False
        left[0] -= 1
        return True

    asked = 0
    if not marvel.spent():
        try:
            for c in candidates(path):
                if left[0] < 2 or STOPPING.is_set():
                    break                            # a search with no page after it is a wasted request
                entry = ask(c, spend, night, get=get)
                record.merge_extra(c["sid"], c["number"], {"marveldc": entry}, path)
                asked += 1
        except _OutOfRequests:
            pass
        except SourceUnavailable as e:
            logger.info(f"Marvel.com stopped this tick: {e}")
    kept = 0
    try:
        rejudge_once(path)
    except Exception as e:
        logger.info(f"Marvel.com re-judge skipped: {e}")
    if not cdn.spent() and not STOPPING.is_set():
        kept = download_pending(path, night, downloads, get=cdn_get)
    if asked or kept:
        logger.info(f"Marvel.com: {asked} issues asked, {kept} covers kept")
    return asked + kept


# --- what it did ------------------------------------------------------------------------
def _entries(path):
    from kometa import record
    record.ensure_tables(path)
    with db._connect(path) as conn:
        rows = conn.execute("""SELECT r.tracked_series_id AS sid, r.number, r.extra_json, s.title
            FROM issue_record r JOIN tracked_series s ON s.id = r.tracked_series_id
            WHERE r.extra_json LIKE '%"marveldc": {%' ORDER BY s.title, r.number""").fetchall()
    for r in rows:
        try:
            e = (json.loads(r["extra_json"] or "{}") or {}).get("marveldc")
        except (TypeError, ValueError):
            continue
        if isinstance(e, dict):
            yield r, e


def status(path=None) -> dict:
    """The harvest card's row: issues asked, matched, covers kept, upgrades."""
    path = path or DB_PATH
    asked = matched = kept = upgrades = pending = 0
    for _, e in _entries(path):
        asked += 1
        matched += bool(e.get("matched"))
        for img in e.get("images") or []:
            if img.get("status") == "kept":
                kept += 1
                upgrades += bool(img.get("upgrade_of"))
            elif img.get("status") == "pending":
                pending += 1
    start, end = _hours(path)
    return {"asked": asked, "matched": matched, "kept": kept, "upgrades": upgrades, "pending": pending,
            "budget": marvel.budget(), "cdn_budget": cdn.budget(), "enabled": enabled("marveldc_enabled", path),
            "hours": f"{start}-{end}"}


def report(path=None) -> dict:
    """The last night's run: issues asked, matched, unmatched with the reason,
    and every cover kept (key, size, upgrade_of + the old size)."""
    path = path or DB_PATH
    rows = list(_entries(path))
    nights = {e.get("night") for _, e in rows} | {i.get("night") for _, e in rows for i in e.get("images") or []}
    nights.discard(None)
    night = max(nights) if nights else None
    asked, unmatched, kept, passed, pending = [], [], [], [], 0
    for r, e in rows:
        who = {"series_id": r["sid"], "series": r["title"], "number": r["number"]}
        if e.get("night") == night:
            asked.append(who)
            if not e.get("matched"):
                unmatched.append({**who, "page": e.get("page"), "reason": e.get("reason")})
        for img in e.get("images") or []:
            if img.get("status") == "pending":
                pending += 1
            if img.get("night") != night:
                continue
            row = {**who, "key": img.get("key"), "kind": img.get("kind"), "size": [img.get("width"), img.get("height")],
                   "bytes": img.get("bytes")}
            if img.get("status") == "kept":
                kept.append({**row, "upgrade_of": img.get("upgrade_of"), "old_size": img.get("old_size")})
            elif img.get("status") in ("smaller", "failed"):
                passed.append({**row, "status": img["status"], "why": img.get("why"), "same_as": img.get("same_as"),
                               "old_size": img.get("old_size")})
    return {"night": night, "asked": len(asked), "matched": len(asked) - len(unmatched), "unmatched": unmatched,
            "kept": kept, "not_kept": passed, "pending_downloads": pending,
            "requests_today": {"marvel": marvel.budget(), "cdn": cdn.budget()},
            "dc": "not asked: DC.com issue pages carry no UPC (probed 2026-10-12)"}


def tonight(path=None) -> dict:
    """The Settings card's lines: requests used today, and the last night's matches and keeps."""
    r = report(path)
    m, c = marvel.budget(), cdn.budget()
    return {"night": r["night"], "asked": r["asked"], "matched": r["matched"], "kept": len(r["kept"]),
            "requests": m["used"] + c["used"], "cap": m["cap"] + c["cap"], "paused": m["paused"] or c["paused"]}


@router.get("/api/harvest/marveldc-report")
def get_report():
    return report()
