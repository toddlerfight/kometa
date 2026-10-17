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


@db.once_per_db
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


def enrich_catalogue(data: dict, path, lookup=None, limit_comics=ENRICH_COMICS, limit_gns=ENRICH_GNS, locg_search=None) -> int:
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
                n += _locg_fallback(data, path, locg_search)      # Metron resting doesn't stop LOCG's look
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
    n += _locg_fallback(data, path, locg_search)
    _save(data, path)
    return n


def _locg_pick(rows: list[dict], e: dict) -> dict | None:
    """LOCG's series rows (not its issue rows) with the chart's name — the same
    publisher if one is, else the newest run. The chart is this month's."""
    same = [r for r in rows if not r.get("comic") and _same_name(r.get("title") or "", e["series"])]
    pub = (e.get("publisher") or "").lower()
    by_pub = [r for r in same if pub and (r.get("publisher") or "").lower() == pub]
    return max(by_pub or same, key=lambda r: r.get("year") or 0, default=None)


def _locg_fallback(data: dict, path, search=None) -> int:
    """Metron missed it (Dungeon Crawler Carl, Vault): ask LOCG once, so the card gets
    a cover and a Track button instead of a dead 'Find'. A LOCG that didn't answer
    (paused, walled) is not a miss — the row is tried again next time."""
    from kometa import locg_client
    search = search or locg_client.search_series_strict
    n = 0
    for key in ("comics", "graphic_novels"):
        for e in [x for x in data.get(key, []) if x.get("catalogue_miss") and not x.get("owned")
                  and not x.get("locg_series_id") and not x.get("locg_tried")]:
            try:
                rows = search(e["series"])
            except Exception as ex:
                logger.info(f"Trending LOCG fallback paused at {e['series']!r}: {ex}")
                return n
            hit = _locg_pick(rows, e)
            e["locg_tried"] = True
            if hit:
                e["locg_series_id"] = hit["id"]
                e["locg_title"] = re.sub(r"\s*\(\d{4}\)\s*$", "", hit.get("title") or "")
                e["year"] = hit.get("year")
                e["cover"] = e.get("cover") or hit.get("cover")
                n += 1
    return n


# --- collected editions: that volume's own cover, or not shown -------------------------
# 2026-10-17: Absolute Batman Vol 1, 2 and 3 all wore the run's cover, and Dungeon
# Crawler Carl Vol 1 and 2 both wore LOCG's series art — three books that looked
# like one. A volume's cover comes from the shelf's trades list, Metron's TPB/HC
# sibling series, or LOCG's edition list; the run's own cover is never used.
_HC_RE = re.compile(r"\bHC\b|hardcover", re.I)


def _edition_format(name: str) -> str:
    return "HC" if _HC_RE.search(name or "") else "Omnibus" if re.search(r"omnibus", name or "", re.I) else "TPB"


def _pick_edition(eds: list[dict], e: dict) -> dict | None:
    """That volume with a cover, in the chart's format if the catalogue has it."""
    same = [t for t in eds if t.get("vol") == e["vol"] and t.get("cover")]
    want = _edition_format(e.get("title") or "")
    return next((t for t in same if t.get("format") == want), None) or (same[0] if same else None)


def _shelf_editions(entries: list[dict], path) -> dict:
    sids = {e["series_id"] for e in entries if e.get("owned") and e.get("series_id") and e.get("vol") is not None}
    return {sid: (db.get_trades(sid, path) or {}).get("trades") or [] for sid in sids}


def _vol_cover(e: dict, shelf: dict) -> str:
    hit = _pick_edition(shelf.get(e.get("series_id")) or [], e) if e.get("owned") else None
    return (hit or {}).get("cover") or e.get("vol_cover") or ""


def _metron_edition(results: list[dict], e: dict) -> dict | None:
    """Metron files a run's collected editions as sibling series ('Absolute Batman
    TPB (2025)') whose issue #3 is Vol 3. The run itself answers the same query
    with its single issue #3 — same name, no format on the end — and is skipped."""
    from kometa.record import _base_name
    eds = []
    for x in results:
        name = re.sub(r"\s*\(\d{4}\)\s*$", "", (x.get("series") or {}).get("name") or "")
        if _key(name) == _key(e["series"]) or _key(_base_name(name)) != _key(e["series"]):
            continue
        eds.append({"vol": e["vol"], "format": _edition_format(name), "cover": x.get("image")})
    return _pick_edition(eds, e)


def _locg_editions(locg_series_id) -> list[dict]:
    from kometa import locg_client
    if locg_client.locg_paused():
        raise RuntimeError("LOCG paused")
    return locg_client.select_editions(locg_client.get_trades_anon(locg_series_id))


def _wants_vol_cover(e: dict, shelf: dict) -> bool:
    return e.get("vol") is not None and not e.get("vol_tried") and not _vol_cover(e, shelf)


def fill_collected_covers(data: dict, path, lookup=None, locg_trades=None) -> int:
    """Each charted volume without a cover of its own: Metron's collected sibling,
    then (a run only LOCG knows) LOCG's edition list. A source that didn't answer
    is asked again next time. Returns how many covers were found; saves."""
    from kometa import metron_client
    lookup = lookup or (lambda series, vol: metron_client._get("issue/", series_name=series, number=str(vol)))
    locg_trades = locg_trades or _locg_editions
    entries = data.get("graphic_novels", [])
    shelf = _shelf_editions(entries, path)
    n = 0
    for e in [x for x in entries if _wants_vol_cover(x, shelf)]:
        if not e.get("vol_metron"):
            try:
                r = lookup(e["series"], e["vol"])
            except metron_client.MetronUnavailable as ex:
                logger.info(f"Trending volume covers paused: {ex}")
                break
            except Exception as ex:
                logger.info(f"Trending volume cover skipped {e['title']!r}: {ex}")
                continue
            e["vol_metron"] = True
            hit = _metron_edition(r.get("results") or [], e)
            if hit:
                e["vol_cover"], e["vol_tried"] = hit["cover"], True
                n += 1
                continue
        if e.get("owned"):
            e["vol_tried"] = True                     # the shelf's own trades list catches up on its own
            continue
        if not (e.get("metron_series_id") or e.get("locg_tried")):
            continue                                  # the run hasn't been looked up everywhere yet
        if e.get("locg_series_id"):
            try:
                hit = _pick_edition(locg_trades(e["locg_series_id"]), e)
            except Exception as ex:
                logger.info(f"Trending volume cover via LOCG paused at {e['title']!r}: {ex}")
                continue
            if hit:
                e["vol_cover"] = hit["cover"]
                n += 1
        e["vol_tried"] = True
    _save(data, path)
    return n


def shown(data: dict, path) -> dict:
    """What the rows show: every card has a cover, a volume charted twice (Vol 3 and
    Vol 3 HC) is one card at its better rank, and no cover appears twice."""
    shelf = _shelf_editions(data.get("graphic_novels", []), path)
    out = {}
    for key in ("comics", "graphic_novels"):
        rows, covers, vols = [], set(), set()
        for e in data.get(key, []):
            vol = (_key(e["series"]), e["vol"]) if e.get("vol") is not None else None
            cover = _vol_cover(e, shelf) if vol else e.get("cover")
            if not cover or cover in covers or vol in vols:
                continue
            covers.add(cover)
            if vol:
                vols.add(vol)
            rows.append(dict(e, cover=cover))
        out[key] = rows
    return out


def _enrich_in_background(data: dict, path):
    if _enriching["on"]:
        return
    _enriching["on"] = True

    def run():
        try:
            enrich_catalogue(data, path)
            fill_collected_covers(data, path)
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
    # misses from before the LOCG fallback (or a LOCG that was paused): one more look, quietly
    retry = any(e.get("catalogue_miss") and not e.get("owned") and not e.get("locg_tried")
                for key in ("comics", "graphic_novels") for e in data.get(key, []))
    shelf = _shelf_editions(data.get("graphic_novels", []), path)
    vols = any(_wants_vol_cover(e, shelf) for e in data.get("graphic_novels", []))
    if needs or retry or vols:
        _enrich_in_background(data, path)
    data["pending"] = needs or vols
    return {**{k: v for k, v in data.items() if not k.startswith("_")}, **shown(data, path)}


@router.get("/api/trending")
def api_trending(refresh: int = 0):
    return trending(refresh=bool(refresh))
