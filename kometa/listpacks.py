"""Packs for a reading list: one download that fills many of its gaps.

Get missing used to walk a 196-gap Secret Wars list entry by entry — forty-odd
runs, forty-odd searches. The indexers already hold the event as one pack
('Secret Wars (Story Arc) (2015-2016)', 10 GB, seeded). This finds those packs
and, before a byte of comic is downloaded, reads the pack's FILE LIST (the
.torrent's info dict, the .nzb's <file subject>s — both tiny) and counts how
many of the list's gaps it actually holds. A pack is chosen on what's inside
it, never on its name: a lying pack title is how the Transmetro trades landed
wrong.

Collected editions (omnibus, TPB, 'Hybrid Comic eBook') are not issue packs —
they fill Trades, not gaps — and are skipped here.
"""
import logging
import os
import re
import time
import xml.etree.ElementTree as ET

import kometa.db as db
from kometa import readlists
from kometa.naming import norm_key, parse_issue_number

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
COMICS_CATEGORIES = (7030,)                 # Newznab 'Books/Comics'; Prowlarr maps every indexer onto it
MIN_COVER = 5                               # a pack must fill this many gaps…
MIN_SHARE = 0.30                            # …or this share of them
MAX_INSPECT = 8                             # file lists fetched per hunt (each is one indexer grab of metadata)
MAX_MAGNETS = 2                             # magnet-only candidates resolved through qBit, paused, per hunt
MAGNET_WAIT_S = 60
_COLLECTED = re.compile(r"\b(omnibus|tpb|trade paperback|hc|hardcover|deluxe|compendium|vol(?:ume)?\.?\s*\d+|"
                        r"hybrid\.?comic|ebook|collection|complete collection)\b", re.I)
_NOT_COMIC = re.compile(r"\b(2160p|1080p|720p|x26[45]|hevc|bluray|web-?dl|webrip|m4b|mp3|flac|repack|dodi|fitgirl)\b", re.I)
COMIC_EXTS = (".cbz", ".cbr", ".zip", ".rar", ".cb7", ".pdf")


# --- matching file names to gaps ---------------------------------------------------
def _key(title: str) -> str:
    t = re.sub(r"\s*\((?:19|20)\d{2}[^)]*\)\s*$", "", title or "")
    t = re.sub(r"['’]", "", t)
    return re.sub(r"^the ", "", norm_key(t))


def gap_index(list_id: int, path=None) -> dict:
    """{series key: {"title", "numbers": {number: item_id}}} for every gap in the list."""
    res = readlists.resolve(list_id, path or DB_PATH)
    out: dict = {}
    for e in res["entries"]:
        if e["status"] == "owned":
            continue
        n = readlists._num(e["number"])
        if n is None:
            continue
        g = out.setdefault(_key(e["series"]), {"title": e["series"], "numbers": {}})
        g["numbers"].setdefault(n, e["item_id"])
    return out


def match_file(name: str, gaps: dict) -> int | None:
    """The gap item a pack file fills, or None. The longest series name that
    leads the file name wins — 'Secret Wars 2099 001' is Secret Wars 2099's, not
    Secret Wars #2099."""
    stem = os.path.splitext(os.path.basename(name))[0]
    k = _key(stem)
    best = None
    for key in gaps:
        # the series name, then straight to the number: 'Secret Wars Journal 001'
        # leads with Secret Wars but is another title
        if key and k.startswith(key + " ") and re.match(r"(?:issue )?\d", k[len(key) + 1:]) \
                and (best is None or len(key) > len(best)):
            best = key
    if best is None:
        return None
    n = parse_issue_number(stem, gaps[best]["title"])
    return gaps[best]["numbers"].get(n) if n is not None else None


def coverage(files: list[str], gaps: dict) -> dict[int, str]:
    """{item_id: file name} for the gaps a pack's files fill (first file wins)."""
    out: dict[int, str] = {}
    for f in files:
        if not f.lower().endswith(COMIC_EXTS):
            continue
        it = match_file(f, gaps)
        if it is not None and it not in out:
            out[it] = f
    return out


# --- reading a pack's file list without downloading it ----------------------------
def _bdecode(data: bytes, i: int = 0):
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        i, out = i + 1, []
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i, out = i + 1, {}
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            out[k] = v
        return out, i + 1
    j = data.index(b":", i)
    n = int(data[i:j])
    return data[j + 1:j + 1 + n], j + 1 + n


def torrent_files(data: bytes) -> list[str]:
    """File paths in a .torrent (its info dict)."""
    meta, _ = _bdecode(data)
    info = meta.get(b"info") or {}
    if b"files" in info:
        return ["/".join(p.decode("utf-8", "replace") for p in f.get(b"path", [])) for f in info[b"files"]]
    return [info.get(b"name", b"").decode("utf-8", "replace")]


def nzb_files(xml_text: str) -> list[str]:
    """File names an .nzb carries — the quoted name inside each <file subject>."""
    root = ET.fromstring(xml_text)
    out = []
    for f in root.iter():
        if f.tag.endswith("file"):
            subj = f.attrib.get("subject", "")
            m = re.search(r'"([^"]+)"', subj)
            out.append(m.group(1) if m else subj)
    return out


def _http_get(url: str) -> tuple[bytes, str]:
    """Fetch a Prowlarr download link without following a redirect to a magnet."""
    import requests
    r = requests.get(url, timeout=30, allow_redirects=False, headers={"User-Agent": "kometa/1.0"})
    if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get("Location", "")
        if loc.startswith("magnet:"):
            return b"", loc
        r = requests.get(loc, timeout=30, headers={"User-Agent": "kometa/1.0"})
    r.raise_for_status()
    return r.content, ""


def _magnet_files(magnet: str, qbit=None) -> list[str] | None:
    """A magnet-only pack: hand it to qBit with 'stop once the metadata arrives'
    (a PAUSED torrent never fetches metadata — the first cut read 0 files off
    every magnet), read the file list, delete it at once. No content is fetched.
    None when the swarm didn't answer in time."""
    from kometa import sources
    from kometa.qbittorrent_client import infohash_from_magnet
    qb = qbit or sources.qbittorrent()
    if not qb:
        return None
    ih = infohash_from_magnet(magnet)
    if not ih:
        return None
    r = qb._req("POST", "/api/v2/torrents/add",
                data={"urls": magnet, "category": "kometa-probe", "stopCondition": "MetadataReceived"})
    if r is None:
        return None
    try:
        deadline = time.time() + MAGNET_WAIT_S
        while time.time() < deadline:
            r = qb._req("GET", "/api/v2/torrents/files", params={"hash": ih})
            files = r.json() if r is not None else []
            if files:
                return [f.get("name", "") for f in files]
            time.sleep(2)
        return None
    finally:
        qb.delete_torrent(ih, delete_files=True)


def file_list(cand: dict, fetch=None, magnet_files=None) -> list[str] | None:
    """The pack's file names, or None if we couldn't look."""
    fetch = fetch or _http_get
    try:
        if cand.get("protocol") == "usenet":
            data, _ = fetch(cand["url"])
            return nzb_files(data.decode("utf-8", "replace"))
        magnet = cand.get("magnet") or ""
        if cand.get("url") and not cand["url"].startswith("magnet:"):
            data, redirect = fetch(cand["url"])
            if data:
                return torrent_files(data)
            magnet = magnet or redirect
        if magnet:
            return (magnet_files or _magnet_files)(magnet)
    except Exception as e:
        logger.info(f"List packs: couldn't read {cand.get('title')!r}: {e}")
    return None


# --- the hunt ------------------------------------------------------------------------
def queries(list_name: str, gaps: dict) -> list[str]:
    """The list's own name, its runs (with years), and the event shapes packs are
    posted under. Deduped, list name first."""
    base = re.sub(r"\s*[:(].*$", "", list_name).strip()
    years = sorted({y for g in gaps.values() for y in re.findall(r"\b(?:19|20)\d{2}\b", g["title"])})
    qs = [list_name, base, f"{base} Story Arc", f"{base} Event", f"{base} Complete"]
    qs += [f"{base} {y}" for y in years]
    qs += [g["title"] for g in sorted(gaps.values(), key=lambda g: -len(g["numbers"]))[:6]]
    out, seen = [], set()
    for q in qs:
        k = norm_key(q)
        if k and k not in seen:
            seen.add(k)
            out.append(q)
    return out


def find_packs(list_id: int, path=None, prowlarr=None, fetch=None, magnet_files=None, inspect: int = MAX_INSPECT) -> dict:
    """Dry run: candidate packs with what each one would fill. Nothing is queued.
    → {gaps, queries, candidates: [{title, protocol, size, seeders, url, magnet, covers, covered, files}], chosen: [...]}"""
    path = path or DB_PATH
    from kometa import sources
    gaps = gap_index(list_id, path)
    n_gaps = sum(len(g["numbers"]) for g in gaps.values())
    with db._connect(path) as conn:
        r = conn.execute("SELECT name FROM reading_lists WHERE id = ?", (list_id,)).fetchone()
    if not r:
        raise KeyError(list_id)
    out = {"list_id": list_id, "gaps": n_gaps, "queries": [], "candidates": [], "chosen": []}
    if not n_gaps:
        return out
    pr = prowlarr or sources.prowlarr()
    if not pr:
        out["error"] = "Prowlarr isn't configured"
        return out
    qs = queries(r["name"], gaps)
    out["queries"] = qs
    import html
    base = _key(re.sub(r"\s*[:(].*$", "", r["name"]))
    run_keys = [k for k in gaps if len(k) >= 4]
    def relevance(t: str) -> int:
        # a pack's title must name the event or one of the list's runs — an
        # 'X-Force (v1-v3 + extras)' torrent is big, seeded, and nothing to do with it
        k = _key(t)
        return 2 if base and base in k else (1 if any(rk in k for rk in run_keys) else 0)
    seen, cands = set(), []
    for q in qs:
        for c in pr.search(q, categories=COMICS_CATEGORIES):
            c = dict(c, title=html.unescape(c.get("title") or ""))
            t = c["title"]
            key = (c.get("protocol"), norm_key(t))
            if key in seen or _COLLECTED.search(t) or _NOT_COMIC.search(t) or not relevance(t):
                continue
            seen.add(key)
            c["_rel"] = relevance(t)
            cands.append(c)
    # the event's own name first, then the best seeded, then the biggest
    cands.sort(key=lambda c: (-c["_rel"], -(c.get("seeders") or 0), -(c.get("size") or 0)))
    magnets = 0
    for c in cands[:inspect]:
        magnet_only = c.get("protocol") == "torrent" and (not c.get("url") or c["url"].startswith("magnet:"))
        if magnet_only:
            if magnets >= MAX_MAGNETS:
                continue
            magnets += 1
        files = file_list(c, fetch, magnet_files)
        if files is None:
            continue
        cov = coverage(files, gaps)
        out["candidates"].append({"title": c.get("title"), "protocol": c.get("protocol"), "size": c.get("size"),
                                  "seeders": c.get("seeders"), "indexer": c.get("indexer"), "url": c.get("url"),
                                  "magnet": c.get("magnet"), "files": len(files),
                                  "covered": len(cov), "covers": sorted(cov)})
    out["chosen"] = choose(out["candidates"], n_gaps)
    return out


def choose(candidates: list[dict], n_gaps: int) -> list[dict]:
    """Best coverage first; a pack only counts for gaps nothing chosen already
    fills, and must clear the bar on those alone."""
    bar = min(MIN_COVER, max(1, int(n_gaps * MIN_SHARE + 0.999))) if n_gaps else MIN_COVER
    chosen, taken = [], set()
    pool = list(candidates)
    while pool:
        pool.sort(key=lambda c: (-len(set(c["covers"]) - taken), -(c.get("seeders") or 0)))
        best = pool.pop(0)
        new = set(best["covers"]) - taken
        if len(new) < bar:
            break
        chosen.append(dict(best, fills=sorted(new)))
        taken |= new
    return chosen


# --- queueing a chosen pack ------------------------------------------------------------
LIST_PACK = "list_pack"


def pack_issue_number(list_id: int, n: int) -> float:
    """The queue's (series, issue_number) key for a list pack: well clear of any real issue."""
    return float(-1000 - list_id * 10 - n)


def queue_pack(list_id: int, pack: dict, owner_series_id: int, path=None, sab=None, qbit=None) -> int | None:
    """Submit a chosen pack and record it as a list_pack row (owned by one of the
    runs it fills — the queue keys on a series). Returns the queue id."""
    import json
    from kometa import sources
    path = path or DB_PATH
    with db._connect(path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM download_queue WHERE kind = ? AND tracked_series_id = ?",
                         (LIST_PACK, owner_series_id)).fetchone()[0]
    number = pack_issue_number(list_id, n)
    meta = {"list_id": list_id, "pack_title": pack["title"], "fills": pack.get("fills") or pack["covers"]}
    if pack["protocol"] == "usenet":
        client = sab or sources.sabnzbd()
        handle = client.add_nzb_url(pack["url"], pack["title"]) if client else None
        state, col = "pending_usenet", "sab_nzo_id"
    else:
        client = qbit or sources.qbittorrent()
        handle = client.add_torrent(pack.get("magnet") or pack["url"]) if client else None
        state, col = "pending_torrent", "torrent_hash"
    if not handle:
        logger.info(f"List packs: couldn't submit {pack['title']!r}")
        return None
    with db._connect(path) as conn:
        cur = conn.execute(f"""INSERT INTO download_queue (tracked_series_id, issue_number, kind, state, {col}, source_url, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)""", (owner_series_id, number, LIST_PACK, state, handle, pack.get("url"), json.dumps(meta)))
        qid = cur.lastrowid
    logger.info(f"List packs: queued {pack['title']!r} for list {list_id} (fills {len(meta['fills'])}) → qid {qid}")
    return qid


# --- placing what a finished pack holds --------------------------------------------------
def place(item: dict, qid: int, comics: list[str], place_fn, label: str, path=None, ensure_run=None) -> dict:
    """Each pack file that fills a gap of the list goes into its run's folder
    under the house name; everything else in the pack is left behind. A run the
    list names but nobody tracks yet is tracked here, pull list off — the same
    way Get missing tracks one."""
    import json
    from kometa.downloader import _safe, ensure_cbz, force_readable_tree
    from kometa.naming import counts_as_owned
    path = path or DB_PATH
    meta = json.loads(item.get("meta_json") or "{}")
    list_id = meta.get("list_id")
    gaps = gap_index(list_id, path)
    want = set(meta.get("fills") or [])
    entries = {e["item_id"]: e for e in readlists.resolve(list_id, path)["entries"]}
    ensure_run = ensure_run or _ensure_run
    placed, skipped, touched = 0, 0, {}
    for src in comics:
        it = match_file(src, gaps)
        if it is None or (want and it not in want):
            skipped += 1
            continue
        e = entries.get(it)
        if not e:
            continue
        series = ensure_run(list_id, it, path)
        if not series:
            skipped += 1
            continue
        n = readlists._num(e["number"])
        dest_dir = series["folder_path"]
        os.makedirs(dest_dir, exist_ok=True)
        ext = os.path.splitext(src)[1].lower()
        num = f"{int(n):03d}" if n == int(n) else f"{n:g}"
        dst = os.path.join(dest_dir, f"{_safe(series['title'])} #{num}{ext}")
        if os.path.exists(dst) and counts_as_owned(dst, series["title"]):
            skipped += 1
            continue
        landed = ensure_cbz(place_fn(src, dst))
        placed += 1
        touched[series["id"]] = landed
        force_readable_tree(dest_dir)
    logger.info(f"{label} list pack: placed {placed}, left {skipped} of {len(comics)} for list {list_id}")
    db.update_queue_state(qid, "done", error=None if placed else "Nothing in the pack filled a gap", path=path)
    return {"placed": placed, "skipped": skipped, "series": touched}


def _ensure_run(list_id: int, item_id: int, path) -> dict | None:
    """The tracked run for a list entry: already linked, or found and tracked
    now (Metron, then LOCG — Get missing's own rules). None if nobody can say."""
    from kometa import listget, sources
    res = readlists.resolve(list_id, path)
    e = next((x for x in res["entries"] if x["item_id"] == item_id), None)
    if not e:
        return None
    if e.get("series_id"):
        return db.get_series_by_id(e["series_id"], path)
    item = next(i for i in readlists.get_items(list_id, path) if i["id"] == item_id)
    run = listget._find_run(e, item)
    if not run or run.get("retry"):
        return None
    s = listget._existing_for(run, path)
    if not s:
        s = listget._track(run, list_id, path, sources.comics_root())
        try:
            listget._sync(s)
        except Exception as ex:
            logger.info(f"List packs: first sync for {s['title']!r} failed: {ex}")
    return db.get_series_by_id(s["id"], path)
