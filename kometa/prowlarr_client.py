"""Prowlarr aggregate search — one query across every configured indexer, both
protocols. This is what lets Kometa SEE torrents: the newznab path
(usenet_client) only ever queried the usenet feeds, so torrent indexers were
invisible. Prowlarr's /api/v1/search returns usenet + torrent results in one
normalized shot, with seeders/grabs/age so the brain can choose.

Reuses the usenet scoring (_norm/_nzb_score/_pack_score) for title relevance and
layers a seeder weight on top — a healthy torrent completes, a 0-seeder one is a
corpse.
"""
import datetime
import logging
import re
import time
import requests

from kometa.naming import _season_from_title
from kometa.usenet_client import _nzb_score, _pack_score, year_mismatch

logger = logging.getLogger(__name__)


def _seed_bonus(seeders: int) -> int:
    """+1 per 10 seeders, capped at +10. Tilts ties toward what will actually finish."""
    return min(int(seeders or 0), 100) // 10


# How many days BEFORE an issue's store date a release can plausibly be that
# issue. Same idea as the legacy newznab _drop_stale (45d on pubDate), but the
# Prowlarr migration silently LOST that guard — nothing here ever read the
# `age` field, which is how a 312-day-old [digital-mobile] webtoon chapter got
# ranked as equal to yesterday's print issue. Slightly more generous than the
# legacy 45 because `age` is indexer-side and fuzzier than a pubDate.
_STALE_GRACE_DAYS = 60


def _is_stale(result: dict, store_date: str | None) -> bool:
    """True when Prowlarr's `age` (days since the release was posted) says it
    existed well before the issue's store date — an older printing/webtoon
    wearing the same number. Missing age or store date never flags: this
    DEMOTES in the sort, it does not reject, and unknown data must not demote."""
    if not store_date:
        return False
    age = result.get("age")
    if age is None:
        return False
    try:
        sd = datetime.date.fromisoformat(str(store_date)[:10])
        posted = datetime.date.today() - datetime.timedelta(days=float(age))
    except (TypeError, ValueError):
        return False
    return posted < sd - datetime.timedelta(days=_STALE_GRACE_DAYS)


COMICS_TAG = "comics"            # the Prowlarr tag the owner put on the comic indexers (2026-10-18)
COMICS_CATEGORY = 7030          # Books/Comics
_COMIC_IDX_TTL_S = 1800
_comic_idx: dict = {}           # base_url → (fetched_at, {protocol: [indexer ids]} or None)


class ProwlarrClient:
    def __init__(self, base_url: str, apikey: str):
        self.base_url = base_url.rstrip("/")
        self.apikey = apikey
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "kometa/1.0"
        # the key as a header, not ?apikey=: a failed request's error repeats its URL
        self.session.headers["X-Api-Key"] = apikey

    def test(self) -> tuple[bool, str]:
        """Verify the server answers and the API key is valid. /api/v1/indexer is
        the cheapest authenticated call. Returns (ok, detail) — detail is the
        configured-indexer count on success, else the reason."""
        try:
            r = self.session.get(
                f"{self.base_url}/api/v1/indexer",
                timeout=15,
            )
            if r.status_code == 401:
                return False, "Unauthorized — check the API key"
            r.raise_for_status()
            data = r.json()
            n = len(data) if isinstance(data, list) else 0
            return True, f"{n} indexer{'s' if n != 1 else ''} configured"
        except Exception as e:
            return False, str(e)

    def comic_indexers(self) -> dict | None:
        """{protocol: [indexer ids]} of the enabled indexers tagged 'comics', or None when
        there's no such tag (then every indexer is asked, as before). Looked up every half hour.
        Before the tag, a Buffy search also went to YTS, showRSS and SubsPlease — sites that
        only hold films and TV — and the TV show's releases came back with the comics."""
        now = time.time()
        hit = _comic_idx.get(self.base_url)
        if hit and now - hit[0] < _COMIC_IDX_TTL_S:
            return hit[1]
        try:
            tags = self.session.get(f"{self.base_url}/api/v1/tag", timeout=15).json()
            tag = next((t["id"] for t in tags if (t.get("label") or "").lower() == COMICS_TAG), None)
            found = None
            if tag is not None:
                found = {}
                for ix in self.session.get(f"{self.base_url}/api/v1/indexer", timeout=15).json():
                    if ix.get("enable") and tag in (ix.get("tags") or []):
                        found.setdefault(ix.get("protocol"), []).append(ix["id"])
                found = found or None
        except Exception as e:
            logger.info(f"Prowlarr: comic indexers not looked up ({e}) — asking them all")
            return hit[1] if hit else None
        _comic_idx[self.base_url] = (now, found)
        return found

    def search(self, query: str, protocol: str | None = None, limit: int = 100, categories=None) -> list[dict]:
        """Aggregate search. Returns normalized dicts:
        {title, protocol, magnet, url, seeders, grabs, size, age, indexer}.
        protocol filter: 'torrent' | 'usenet' | None (both).
        With the 'comics' tag in Prowlarr: only those indexers (of that protocol), in Books/Comics."""
        params = {"query": query, "type": "search", "limit": limit}
        comic = self.comic_indexers()
        ids = [i for p, l in (comic or {}).items() if not protocol or p == protocol for i in l]
        if comic is not None and not ids:
            logger.info(f"Prowlarr: no {protocol} indexer is tagged '{COMICS_TAG}' — not searched")
            return []
        if ids:
            params["indexerIds"] = ids
            params["categories"] = list(categories) if categories else [COMICS_CATEGORY]
        elif categories:
            params["categories"] = list(categories)
        try:
            r = self.session.get(
                f"{self.base_url}/api/v1/search",
                params=params,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning(f"Prowlarr search failed for {query!r}: {e}")
            return []
        out = []
        for it in data:
            proto = it.get("protocol")
            if protocol and proto != protocol:
                continue
            dl = it.get("downloadUrl") or ""
            guid = str(it.get("guid") or "")
            magnet = guid if guid.startswith("magnet:") else (dl if dl.startswith("magnet:") else "")
            out.append({
                "title": it.get("title", ""),
                "protocol": proto,
                "magnet": magnet,
                "url": dl,                      # NZB url, .torrent url, or magnet
                "seeders": it.get("seeders") or 0,
                "grabs": it.get("grabs") or 0,
                "size": it.get("size") or 0,
                "age": it.get("age"),
                "indexer": it.get("indexer", ""),
                # Newznab ids: 7000s = books/comics; 2000s films, 3000s audio, 5000s TV
                "categories": [c.get("id") for c in (it.get("categories") or []) if isinstance(c, dict) and c.get("id")],
            })
        logger.info(f"Prowlarr: {len(out)} {protocol or 'all'}-results for {query!r}")
        return out


# Not a comic, whatever the title says. 2026-10-11: 'The Amazing Spider-Man 2'
# (an R.G. Mechanics game repack, 7.5 GB) was taken for ASM #2, and a 20 GB
# Five Nights at Freddy's game for an issue #4 — scored on the name alone.
_NOT_A_COMIC = re.compile(
    r"\b(r\.?g\.?\s*mechanics|fitgirl|dodi|elamigos|codex|skidrow|repack|"
    r"ps[2345]|xbox|nsp|iso|v\d+\.\d{2,}(?:\.\d+)?|"
    r"2160p|1080p|720p|480p|4k|x26[45]|h26[45]|hevc|xvid|bluray|blu-ray|web-?dl|webrip|hdtv|dvd-?rip|bd-?rip|hd-?rip|dvd\d?|"
    r"mkv|avi|mp4|wmv|"
    r"film collection|trilogy collection|s\d{1,2}e\d{1,2}|\d{1,2}x\d{2}|season \d+|tv series|"   # not 'seasons N' (The Seasons 012) or 'complete series' (comic packs)
    r"mp3|flac|m4b|audiobook|soundtrack|discography|album|lossless|24 ?bit|tr24)\b", re.I)
SINGLE_ISSUE_MAX_BYTES = 1_500_000_000      # a fat one-shot is a few hundred MB


def _media_only(cats) -> bool:
    ids = [int(c) for c in (cats or []) if str(c).isdigit()]
    return bool(ids) and all(1000 <= i < 7000 for i in ids)


def drop_not_comics(results: list[dict], single_issue: bool = True) -> list[dict]:
    """Results that could be a comic at all: not filed only as film/audio/TV/
    games, not named like one, and (for a single issue) not gigabytes."""
    out = []
    for r in results:
        t = re.sub(r"[._]+", " ", r.get("title") or "")
        if _media_only(r.get("categories")) or _NOT_A_COMIC.search(t):
            continue
        if single_issue and (r.get("size") or 0) > SINGLE_ISSUE_MAX_BYTES:
            continue
        out.append(r)
    if len(out) != len(results):
        logger.info(f"Prowlarr: dropped {len(results) - len(out)} result(s) that aren't comics")
    return out


def _best_downloadable_torrent(results: list[dict], score_fn, min_score: int = 10,
                               store_date: str | None = None) -> dict | None:
    """Pick the highest-scoring torrent we can act on — has a download handle
    (a magnet OR a .torrent url; many indexers give the latter, no magnet) and at
    least one live seeder. Returns the result dict or None.

    Stale releases (posted long before store_date — see _is_stale) sort BELOW
    every fresh one regardless of score, but stay selectable when they're all
    there is: a demotion, not a rejection. The score bar is applied BEFORE the
    stale sort so a fresh-but-worthless hit can't shadow a stale-but-real one."""
    viable = [r for r in results if (r.get("magnet") or r.get("url")) and (r.get("seeders") or 0) >= 1]
    if not viable:
        return None
    scored = [(r, score_fn(r)) for r in viable]
    passing = [(r, s) for r, s in scored if s >= min_score]
    if not passing:
        logger.info(f"Prowlarr torrent: best score {max(s for _, s in scored)} "
                    f"below {min_score} — skipping")
        return None
    passing.sort(key=lambda x: (_is_stale(x[0], store_date), -x[1],
                                -(x[0].get("seeders") or 0), -(x[0].get("size") or 0)))
    best, score = passing[0]
    if _is_stale(best, store_date):
        logger.info(f"Prowlarr torrent: every candidate is stale for store_date {store_date} "
                    f"— taking the best of them anyway")
    logger.info(f"Prowlarr torrent: {best['title']!r} score={score} "
                f"seeders={best.get('seeders', 0)} from {best.get('indexer', '')}")
    return best


def _drop_year_mismatches(results: list[dict], title: str, series_year,
                          store_date: str | None = None) -> list[dict]:
    """store_date is what closes the UPWARD door — without it year_mismatch can
    only reject releases older than the series, and a same-named later volume
    (New Avengers 2013 vs the 2025 relaunch) walks straight in. Pack searches
    have no single issue date and pass None, keeping the lower-bound-only rule."""
    if not series_year:
        # 'Rumble (2017)' with no stored year: the name tag IS the year. The scorer
        # now ignores that tag, so this is the only thing keeping 2014 out.
        m = re.search(r"\(((?:19|20)\d{2})\D*\)\s*$", title or "")
        series_year = int(m.group(1)) if m else series_year
    kept = [r for r in results
            if not year_mismatch(r.get("title", ""), series_year, store_date=store_date)]
    if len(kept) < len(results):
        logger.info(f"Prowlarr: dropped {len(results) - len(kept)} year-mismatched result(s) "
                    f"for {title!r} (series began {series_year}, issue shipped {store_date})")
    return kept


def _drop_season_mismatches(results: list[dict], title: str) -> list[dict]:
    """Drop releases that belong to a DIFFERENT season of this title.

    'Batman: The Adventures Continue' is three runs sharing a base title and
    sharing issue numbers 1..8, so a query for issue 1 of Season One scores
    'Season Two 001' just as highly — the name matches, the number matches. The
    download guard rejects those on arrival, correctly, but by then we've spent
    the fetch and the issue just fails over and over. Cheaper and truer to never
    pick them.

    Same conservative rule the download guard uses: a release that names no
    season is innocent (plenty don't), and a series that names no season is
    season 1."""
    want = _season_from_title(title) or 1
    kept = [r for r in results
            if (_season_from_title(r.get("title", "")) or want) == want]
    if len(kept) < len(results):
        logger.info(f"Prowlarr: dropped {len(results) - len(kept)} wrong-season result(s) "
                    f"for {title!r} (want season {want})")
    return kept


def search_torrent_pack(prowlarr: ProwlarrClient, title: str, series_year=None) -> dict | None:
    """Best torrent pack/collection for a series. Returns the result dict (magnet,
    seeders, title, size) or None. Twin of search_usenet_pack below."""
    for q in (f"{title} complete", title):
        results = drop_not_comics(prowlarr.search(q, protocol="torrent"), single_issue=False)
        results = _drop_year_mismatches(results, title, series_year)
        results = _drop_season_mismatches(results, title)
        if not results:
            continue
        best = _best_downloadable_torrent(
            results, lambda r: _pack_score(r["title"], title, r["size"]) + _seed_bonus(r["seeders"]))
        if best:
            return best
    return None


def _issue_queries(title: str, issue_number: float) -> list[str]:
    """The queries a single-issue search walks, in order, stopping at the first one
    that produces a winner. Indexers match the query LITERALLY and releases pad
    their numbers — 'Re-Evolution 002' — so the old lone '{title} 2' query asked
    for a token no release carries and got ZERO hits while three good NZBs sat
    right there (Last Ronin II #2-4, 2026-10-03). Padded first, then the bare
    title as a net: the scorer still demands issue-number evidence before acting,
    so casting wide can't grab the wrong issue."""
    num = f"{int(issue_number):03d}" if issue_number == int(issue_number) else str(issue_number)
    # Indexers match literally and releases spell '&' out — 'Batman and the
    # Joker 003' exists, 'Batman & the Joker 003' gets nothing. Ask both ways.
    titles = [title]
    if "&" in title:
        titles.append(re.sub(r"\s*&\s*", " and ", title))
    return [f"{t} {num}" for t in titles] + titles


def release_key(url: str | None) -> str | None:
    """What makes a Prowlarr download URL the SAME release across searches.
    Prowlarr mints a fresh `link=` token every search (and the apikey is noise),
    so two grabs of one NZB never share a URL — Deadly Class #47 was bought
    three times through a blacklist that compared whole URLs. The indexer path
    plus the `file=` name is what's stable. Anything else (magnets, GetComics
    links) is its own key."""
    if not url:
        return url
    try:
        from urllib.parse import urlsplit, parse_qs
        u = urlsplit(url)
        q = parse_qs(u.query)
        if u.path.endswith("/download") and q.get("file"):
            return f"{u.netloc}{u.path}?file={q['file'][0]}"
    except ValueError:
        pass
    return url


def _drop_failed_sources(results: list[dict], exclude_urls) -> list[dict]:
    """Drop releases whose delivery already failed for this issue (rotted
    NZBs, dead magnets, no-pages posts). The whole point of a retry is to NOT
    buy the same corpse twice — the next-best release gets its shot instead."""
    if not exclude_urls:
        return results
    bad = {release_key(u) for u in exclude_urls}
    return [r for r in results
            if release_key(r.get("url")) not in bad and release_key(r.get("magnet")) not in bad]


def search_torrent(prowlarr: ProwlarrClient, title: str, issue_number: float, series_year=None,
                   store_date: str | None = None, exclude_urls=None) -> dict | None:
    """Best torrent for a single issue. Returns the result dict or None. Twin of
    search_usenet below.

    A single-issue result must carry BOTH the series name (+10) AND issue-number
    evidence (+5) BEFORE seeders count for anything — name-substring alone used
    to hit the old bar of 10 exactly (and seeders could have vaulted it over any
    combined threshold), which is how a well-seeded music album containing the
    word 'Ripcord' got queued for issue #0.

    store_date (ISO 'YYYY-MM-DD') demotes releases posted well before the issue
    existed — see _is_stale."""
    def _score(r):
        base = _nzb_score(r["title"], title, issue_number)
        if base < 15:            # name+number or nothing — seeders can't buy evidence
            return 0
        return base + _seed_bonus(r["seeders"])

    for query in _issue_queries(title, issue_number):
        results = drop_not_comics(prowlarr.search(query, protocol="torrent"))
        results = _drop_year_mismatches(results, title, series_year, store_date=store_date)
        results = _drop_season_mismatches(results, title)
        results = _drop_failed_sources(results, exclude_urls)
        if not results:
            continue
        best = _best_downloadable_torrent(results, _score, min_score=15, store_date=store_date)
        if best:
            return best
    return None


def _best_usenet(results: list[dict], score_fn, min_score: int,
                 store_date: str | None = None) -> dict | None:
    """Pick the highest-scoring usenet result we can act on. Usenet has no
    seeders — a post is either retained or it isn't, and we can't know until SAB
    tries — so viability is just "has an NZB url", and grabs/size break ties.

    Same stale demotion as the torrent picker: a release posted long before
    store_date sorts below every fresh one but survives as a last resort. This
    is THE fix for the Absolute Superman #21 grab — the 312-day-old webtoon tied
    the day-old print rip on score and won the grabs tiebreak; now it can only
    win an empty room."""
    viable = [r for r in results if r.get("url")]
    if not viable:
        return None
    scored = [(r, score_fn(r)) for r in viable]
    passing = [(r, s) for r, s in scored if s >= min_score]
    if not passing:
        logger.info(f"Prowlarr usenet: best score {max(s for _, s in scored)} "
                    f"below {min_score} — skipping")
        return None
    passing.sort(key=lambda x: (_is_stale(x[0], store_date), -x[1],
                                -(x[0].get("grabs") or 0), -(x[0].get("size") or 0)))
    best, score = passing[0]
    if _is_stale(best, store_date):
        logger.info(f"Prowlarr usenet: every candidate is stale for store_date {store_date} "
                    f"— taking the best of them anyway")
    logger.info(f"Prowlarr usenet: {best['title']!r} score={score} "
                f"grabs={best.get('grabs', 0)} from {best.get('indexer', '')}")
    return best


def search_usenet(prowlarr: ProwlarrClient, title: str, issue_number: float, series_year=None,
                  store_date: str | None = None, exclude_urls=None) -> str | None:
    """Best usenet NZB for a single issue. Returns the NZB download URL or None —
    sourced through Prowlarr so it
    sees every usenet indexer Prowlarr aggregates. Same evidence bar as the
    torrent twin: series name (+10) AND issue-number evidence (+5) before we act.
    store_date demotes releases that predate the issue — see _is_stale."""
    def _score(r):
        base = _nzb_score(r["title"], title, issue_number)
        return base if base >= 15 else 0     # name+number or nothing

    for query in _issue_queries(title, issue_number):
        results = drop_not_comics(prowlarr.search(query, protocol="usenet"))
        results = _drop_year_mismatches(results, title, series_year, store_date=store_date)
        results = _drop_season_mismatches(results, title)
        results = _drop_failed_sources(results, exclude_urls)
        if not results:
            continue
        best = _best_usenet(results, _score, min_score=15, store_date=store_date)
        if best:
            return best["url"]
    return None


def search_usenet_pack(prowlarr: ProwlarrClient, title: str, series_year=None, missing: int | None = None) -> str | None:
    """Best usenet pack/collection for a series. Returns the NZB url or None.
    Sourced through Prowlarr.
    missing: how many issues the pack is for — caps the believable size."""
    for q in (f"{title} complete", title):
        results = drop_not_comics(prowlarr.search(q, protocol="usenet"), single_issue=False)
        results = _drop_year_mismatches(results, title, series_year)
        results = _drop_season_mismatches(results, title)
        if not results:
            continue
        best = _best_usenet(results, lambda r: _pack_score(r["title"], title, r["size"], missing=missing), min_score=10)
        if best:
            return best["url"]
    return None
