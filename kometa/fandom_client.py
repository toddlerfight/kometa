"""Collected editions, from the publisher wikis' MediaWiki API.

What a trade COLLECTS is the one fact the filing step needs, and Metron's
reprints field is patchy for trades. The DC and Marvel wikis (dc.fandom.com,
marvel.fandom.com) have it on nearly every collected edition, structured —
and while their pages sit behind Cloudflare, api.php answers plainly.

DC marks a collection '(Collected)' and lists issues as {{c|Series Vol 1 N}}.
Marvel titles them like issues ('X Omnibus Vol 1 1'), Mode = TPB/HC, and lists
ReprintOf1..N = 'Series Vol N num'. Both carry Year and ISBN.
"""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from kometa.naming import norm_key

logger = logging.getLogger(__name__)

USER_AGENT = "Kometa/1.0 (personal pull-list tool)"
MIN_INTERVAL_S = 1.0
WIKIS = {"dc": "dc.fandom.com", "marvel": "marvel.fandom.com"}
_COLLECTED_MODES = {"tpb", "hc", "hardcover", "trade paperback", "omnibus", "graphic novel"}
_lock = threading.Lock()
_last = {"t": 0.0}


class FandomUnavailable(RuntimeError):
    pass


def wiki_for(publisher: str | None) -> str | None:
    p = (publisher or "").lower()
    if "dc" in p.split() or p.startswith("dc") or "vertigo" in p or "black label" in p or "wildstorm" in p:
        return "dc"
    if "marvel" in p:
        return "marvel"
    return None


def _get(host: str, **params) -> dict:
    url = f"https://{host}/api.php?" + urllib.parse.urlencode({**params, "format": "json"})
    with _lock:
        wait = _last["t"] + MIN_INTERVAL_S - time.time()
        if wait > 0:
            time.sleep(wait)
        _last["t"] = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=25) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise FandomUnavailable(f"{host}: {e}") from e


def search(wiki: str, query: str, limit: int = 8) -> list[str]:
    d = _get(WIKIS[wiki], action="query", list="search", srsearch=query, srnamespace=0, srlimit=limit)
    return [h["title"] for h in d.get("query", {}).get("search", [])]


def wikitext(wiki: str, page: str) -> str | None:
    d = _get(WIKIS[wiki], action="parse", page=page, prop="wikitext")
    return d.get("parse", {}).get("wikitext", {}).get("*") if "parse" in d else None


# --- parsing ------------------------------------------------------------------------
_ENTRY_RE = re.compile(r"^(?P<series>.+?)\s+Vol\s+(?P<vol>\d+)\s+(?P<num>[\d.]+(?:\.NOW)?)\s*$", re.I)
_HASH_RE = re.compile(r"^(?P<series>.+?)\s+#\s*(?P<num>[\d.]+)\s*$")


def parse_entry(text: str) -> dict | None:
    """'Batman: Year 100 Vol 1 1' → {series, volume, number}; 'Batman Chronicles #11' too."""
    t = text.strip().split("|")[0].strip()
    m = _ENTRY_RE.match(t) or _HASH_RE.match(t)
    if not m:
        # a bare title is a one-shot — 'Batman: The Killing Joke' IS issue #1 of itself
        t = re.sub(r"\s*\(.*?\)\s*$", "", t).strip()
        return {"series": t, "volume": 1, "number": 1.0} if t and not t.lower().startswith(("category:", "file:")) else None
    num = m.group("num").upper().replace(".NOW", "")
    try:
        number = float(num)
    except ValueError:
        return None
    return {"series": m.group("series").strip(), "volume": int(m.groupdict().get("vol") or 1), "number": number}


def _field(w: str, key: str) -> str | None:
    m = re.search(r"^\|\s*" + re.escape(key) + r"\s*=\s*([^\n]*)", w, re.M)
    return m.group(1).strip() or None if m else None


def parse_collection(w: str, wiki: str) -> dict | None:
    """{year, isbn, collects:[{series, volume, number}], mode} or None if the
    page isn't a collected edition."""
    year = _field(w, "Year")
    isbn = _field(w, "ISBN")
    collects = []
    if wiki == "dc":
        # Only the IssueList block — Notes mention other books ('Countdown #31')
        # that the collection does NOT collect. Two list shapes in the wild:
        # {{c|Batman: Year 100 Vol 1 1}} and [[Batman Vol 1 404|Batman #404]].
        m = re.search(r"^\|\s*IssueList\s*=(.*?)(?=^\|\s*\w+\s*=|\Z)", w, re.M | re.S)
        block = m.group(1) if m else w
        for m in re.finditer(r"\{\{c\|([^}]+)\}\}|\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", block):   # in page order
            p = parse_entry(m.group(1) or m.group(2))
            if p:
                collects.append(p)
        mode = "collected"
    else:
        mode = (_field(w, "Mode") or "").lower()
        if mode not in _COLLECTED_MODES:
            return None
        for m in re.finditer(r"^\|\s*ReprintOf\d+\s*=\s*([^\n]+)", w, re.M):
            p = parse_entry(m.group(1))
            if p:
                collects.append(p)
    if not collects:
        return None
    return {"year": int(year) if year and year.isdigit() else None, "isbn": isbn, "collects": collects, "mode": mode}


# --- lookup ---------------------------------------------------------------------------
_EDITION_WORDS = re.compile(r"\b(the|deluxe|edition|omnibus|tpb|hc|hardcover|collected|collection|vol|volume|book|new|anniversary|\d+th)\b", re.I)


_FORMAT_WORDS = re.compile(r"\b(deluxe|omnibus|absolute|compendium|compact)\b", re.I)
MIN_SCORE = 0.6


def _core(s: str) -> set[str]:
    return set(norm_key(_EDITION_WORDS.sub(" ", re.sub(r"\(Collected\)", "", s))).split())


def _score(q: str, page: str) -> float:
    """How much the page title IS the shelf title: Jaccard over the core words
    (edition noise stripped) — both directions, so 'The Joker: 80 Years of the
    Clown Prince of Crime' can't win on the one word 'joker' — plus a nudge
    when the format words match (a 'Deluxe' query prefers a 'Deluxe' page)."""
    a, b = _core(q), _core(page)
    if not a or not b or not a <= b:
        # every word of the shelf title must be on the page: 'Curse of the White
        # Knight Deluxe' is NOT 'White Knight Deluxe' however many words they share
        return 0.0
    j = len(a & b) / len(a | b)
    fq = {w.lower() for w in _FORMAT_WORDS.findall(q)}
    fp = {w.lower() for w in _FORMAT_WORDS.findall(page)}
    return j + (0.05 if fq and fq <= fp else 0.0)


def lookup_collection(title: str, publisher: str | None) -> dict | None:
    """Find the collected edition page for a folder title. Returns
    {wiki, page, year, isbn, collects, url} or None. Raises FandomUnavailable
    only when the wiki didn't answer."""
    wiki = wiki_for(publisher)
    if not wiki:
        return None
    q = re.sub(r"\s*-\s+", ": ", re.sub(r"\(\d{4}\)", "", title)).strip()
    core_q = " ".join(sorted(_core(q), key=q.lower().find))
    hits, seen = [], set()
    for sq in (q, core_q):                       # full title, then the title minus edition words
        if not sq or sq.lower() in seen:
            continue
        seen.add(sq.lower())
        for h in search(wiki, f"{sq} Collected" if wiki == "dc" else sq):
            if h not in hits and (wiki != "dc" or h.endswith("(Collected)")):
                hits.append(h)
    ranked = sorted(((_score(q, h), h) for h in hits), key=lambda x: (-x[0], len(x[1])))
    best = ranked[0][0] if ranked else 0.0
    for score, page in ranked[:3]:
        # a page that won't parse must not hand the win to a clearly worse title
        # ('Batman: Year One' unparsed → 'The Joker: Year One', 2026-10-08)
        if score < MIN_SCORE or score < best - 0.1:
            break
        w = wikitext(wiki, page)
        if not w:
            continue
        parsed = parse_collection(w, wiki)
        if parsed:
            return {"wiki": wiki, "page": page, "match": round(score, 2),
                    "url": f"https://{WIKIS[wiki]}/wiki/{urllib.parse.quote(page.replace(' ', '_'))}", **parsed}
    return None
