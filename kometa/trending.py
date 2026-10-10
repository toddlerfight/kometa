"""Trending: what comic shops actually sold, from ICv2's monthly charts.

Two charts, keyless and current: Top 50 Comics and Top 20 Graphic Novels by
units (ComicHub data, published by ICv2). The one recommendation row that owes
nothing to your own reading — which is also what makes it the cold-start row.

Each chart row is matched to the shelf by series name; the rest are looked up
on Metron (one issue call each, cached) for a cover and a series id, so the
card can Track the run in one tap. Fetched weekly — the chart is monthly.
"""
import json
import logging
import re
import threading
import urllib.request
from datetime import datetime, timedelta
from html import unescape

from fastapi import APIRouter

import kometa.db as db
from kometa.naming import norm_key

logger = logging.getLogger(__name__)
router = APIRouter()
DB_PATH = db.DB_PATH
INDEX_URL = "https://icv2.com/articles/markets"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TTL_DAYS = 7
ENRICH_COMICS = 24
ENRICH_GNS = 12

_ISSUE_RE = re.compile(r"^(?P<series>.+?)\s+#\s*(?P<num>\d+(?:\.\d+)?)\b")
_VOL_RE = re.compile(r"^(?P<series>.+?)\s+(?:Vol\.?|Volume|Book)\s*(?P<vol>\d+)\b", re.I)
_TAIL_RE = re.compile(r"\s*\((?:one-shot|regular edition cover|[^)]*edition[^)]*|[^)]*cover[^)]*)\)\s*$", re.I)


def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS trending_cache (
            source TEXT PRIMARY KEY, data_json TEXT NOT NULL, fetched_at TEXT DEFAULT (datetime('now')))""")


def _http(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "ignore")


def parse_title(title: str) -> dict:
    """'Absolute Batman #24' → series + number; 'Absolute Batman Vol. 3 Devil's
    Workshop' → series + vol + subtitle; else a one-shot/OGN by its whole name."""
    t = _TAIL_RE.sub("", title.strip())
    t = t.replace("#!", "#1")                       # ICv2's own typo, seen live
    m = _ISSUE_RE.match(t)
    if m:
        return {"series": m.group("series").strip(" -–:"), "number": float(m.group("num")), "vol": None}
    m = _VOL_RE.match(t)
    if m:
        return {"series": m.group("series").strip(" -–:"), "number": None, "vol": int(m.group("vol")),
                "subtitle": t[m.end():].strip(" -–:") or None}
    return {"series": t, "number": None, "vol": None}


def parse_chart(page: str) -> list[dict]:
    """The first table (by units): [{rank, title, publisher, price}]."""
    tables = re.findall(r"<table.*?</table>", page, re.S)
    if not tables:
        return []
    out = []
    for row in re.findall(r"<tr.*?</tr>", tables[0], re.S):
        cells = [re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        cells = [c for c in cells if c]
        if len(cells) >= 3 and cells[0].isdigit():
            out.append({"rank": int(cells[0]), "title": cells[1], "publisher": cells[2],
                        "price": cells[3] if len(cells) > 3 else None, **parse_title(cells[1])})
    return out


def _latest_links(index_html: str) -> dict:
    out = {}
    for href, text in re.findall(r'href="(https://icv2\.com/articles/markets/view/\d+/[^"]*)"[^>]*>([^<]*)', index_html):
        if "comics" not in out and re.search(r"/top-\d+-comics-", href):
            out["comics"] = (href, text.strip())
        if "graphic_novels" not in out and re.search(r"/top-\d+-graphic-novels-", href):
            out["graphic_novels"] = (href, text.strip())
    return out


def fetch_icv2(http=None) -> dict:
    http = http or _http
    links = _latest_links(http(INDEX_URL))
    data = {"source": "ICv2 (ComicHub data)", "fetched_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}
    for key in ("comics", "graphic_novels"):
        href, text = links.get(key, (None, ""))
        data[key] = parse_chart(http(href)) if href else []
        data[key + "_title"] = text
        data[key + "_url"] = href
    m = re.search(r"-([a-z]+)-(\d{4})/?$", data.get("comics_url") or "")       # the slug names the month
    data["month"] = f"{m.group(1).title()} {m.group(2)}" if m else None
    return data


def _load(path) -> dict | None:
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT data_json, fetched_at FROM trending_cache WHERE source = 'icv2'").fetchone()
    if not r:
        return None
    d = json.loads(r["data_json"])
    try:
        d["_stale"] = datetime.strptime(r["fetched_at"], "%Y-%m-%d %H:%M:%S") < datetime.utcnow() - timedelta(days=TTL_DAYS)
    except ValueError:
        d["_stale"] = True
    return d


def _save(data: dict, path):
    ensure_tables(path)
    with db._connect(path) as conn:
        conn.execute("INSERT OR REPLACE INTO trending_cache (source, data_json, fetched_at) VALUES ('icv2', ?, datetime('now'))",
                     (json.dumps({k: v for k, v in data.items() if not k.startswith("_")}),))


# --- matching the shelf and the catalogue ---------------------------------------------
def _key(title: str) -> str:
    """Chart name vs folder name: ICv2 drops the apostrophe ('Shadows Hand')
    and the leading 'The' ('Amazing Spider-Man'); the shelf keeps both."""
    t = re.sub(r"[\'’]", "", re.sub(r"\s*\(\d{4}\)\s*$", "", title or ""))
    return re.sub(r"^the ", "", norm_key(t))


def _shelf_index(path) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = {}
    for s in db.get_all_series(path):
        if s.get("kind") == "arc":
            continue
        idx.setdefault(_key(s["title"]), []).append(s)
        if s.get("metron_series_id"):
            idx.setdefault(f"m{s['metron_series_id']}", []).append(s)
    return idx


def _pick_run(cands: list[dict], number, path) -> tuple[dict | None, dict | None]:
    """Same-named runs (Batman 1940 / 2016 / 2025): the one that has the issue,
    else the newest — a chart row is always the current run."""
    if not cands:
        return None, None
    if number is not None:
        for s in cands:
            i = next((x for x in db.get_issues_for_series(s["id"], path) if x["number"] == number), None)
            if i:
                return s, i
    return max(cands, key=lambda s: s.get("year_began") or 0), None


def match_shelf(entries: list[dict], path) -> None:
    idx = _shelf_index(path)
    for e in entries:
        cands = idx.get(_key(e["series"])) or (idx.get(f"m{e['metron_series_id']}") if e.get("metron_series_id") else None) or []
        s, i = _pick_run(cands, e.get("number"), path)
        e["series_id"] = s["id"] if s else None
        e["owned"] = bool(s)
        if s and e.get("number") is not None:
            e["have_issue"] = bool(i and i.get("owned"))
            e["cover"] = e.get("cover") or (f"/api/series/{s['id']}/issues/{e['number']:g}/thumbnail" if i else f"/api/series/{s['id']}/thumbnail")
        elif s:
            e["cover"] = e.get("cover") or f"/api/series/{s['id']}/thumbnail"


def _same_name(catalogue: str, chart: str) -> bool:
    return _key(catalogue) == _key(chart)


_enriching = {"on": False}


def enrich_catalogue(data: dict, path, lookup=None, limit_comics=ENRICH_COMICS, limit_gns=ENRICH_GNS) -> int:
    """Unowned rows: one Metron issue call each → series id + cover, saved back.
    Returns how many were filled."""
    from kometa import metron_client
    lookup = lookup or (lambda series, number: metron_client._get("issue/", series_name=series, number=f"{(number or 1):g}"))
    n = 0
    for key, limit in (("comics", limit_comics), ("graphic_novels", limit_gns)):
        for e in [x for x in data.get(key, []) if not x.get("owned") and not x.get("metron_series_id") and not x.get("catalogue_miss")][:limit]:
            try:
                r = lookup(e["series"], e.get("number") or 1)
            except metron_client.MetronUnavailable as ex:
                logger.info(f"Trending enrich paused: {ex}")
                _save(data, path)
                return n
            except Exception as ex:
                logger.info(f"Trending enrich skipped {e['series']!r}: {ex}")
                continue
            # Metron's name filter is a contains-match ('X-Men' answers All-New X-Men):
            # keep the same-named runs only, and of those the newest — the chart means this year's
            same = [x for x in r.get("results") or []
                    if _same_name((x.get("series") or {}).get("name") or "", e["series"])]
            hit = max(same, key=lambda x: (x.get("series") or {}).get("year_began") or 0, default=None)
            if hit:
                ser = hit.get("series") or {}
                e["metron_series_id"] = ser.get("id")
                e["metron_title"] = re.sub(r"\s*\(\d{4}\)\s*$", "", ser.get("name") or "")
                e["year"] = ser.get("year_began")
                e["cover"] = hit.get("image")
            else:
                e["catalogue_miss"] = True
            n += 1
    _save(data, path)
    return n


def _enrich_in_background(data: dict, path):
    if _enriching["on"]:
        return
    _enriching["on"] = True

    def run():
        try:
            enrich_catalogue(data, path)
        except Exception as e:
            logger.warning(f"Trending enrich failed: {e}", exc_info=True)
        finally:
            _enriching["on"] = False
    threading.Thread(target=run, daemon=True).start()


def trending(path=None, refresh: bool = False) -> dict:
    path = path or DB_PATH
    data = _load(path)
    if data is None or refresh or data.get("_stale"):
        try:
            fresh = fetch_icv2()
            if fresh.get("comics"):
                data = fresh
                _save(data, path)
        except Exception as e:
            logger.warning(f"Trending: ICv2 fetch failed: {e}")
            if data is None:
                return {"comics": [], "graphic_novels": [], "month": None, "pending": False, "error": str(e)}
    match_shelf(data.get("comics", []), path)
    match_shelf(data.get("graphic_novels", []), path)
    needs = any(not e.get("owned") and not e.get("metron_series_id") and not e.get("catalogue_miss")
                for key in ("comics", "graphic_novels") for e in data.get(key, [])[:ENRICH_COMICS])
    if needs:
        _enrich_in_background(data, path)
    data["pending"] = needs
    return {k: v for k, v in data.items() if not k.startswith("_")}


@router.get("/api/trending")
def api_trending(refresh: int = 0):
    return trending(refresh=bool(refresh))
