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
import requests

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


class ProwlarrClient:
    def __init__(self, base_url: str, apikey: str):
        self.base_url = base_url.rstrip("/")
        self.apikey = apikey
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "kometa/1.0"

    def test(self) -> tuple[bool, str]:
        """Verify the server answers and the API key is valid. /api/v1/indexer is
        the cheapest authenticated call. Returns (ok, detail) — detail is the
        configured-indexer count on success, else the reason."""
        try:
            r = self.session.get(
                f"{self.base_url}/api/v1/indexer",
                params={"apikey": self.apikey}, timeout=15,
            )
            if r.status_code == 401:
                return False, "Unauthorized — check the API key"
            r.raise_for_status()
            data = r.json()
            n = len(data) if isinstance(data, list) else 0
            return True, f"{n} indexer{'s' if n != 1 else ''} configured"
        except Exception as e:
            return False, str(e)

    def search(self, query: str, protocol: str | None = None, limit: int = 100) -> list[dict]:
        """Aggregate search. Returns normalized dicts:
        {title, protocol, magnet, url, seeders, grabs, size, age, indexer}.
        protocol filter: 'torrent' | 'usenet' | None (both)."""
        try:
            r = self.session.get(
                f"{self.base_url}/api/v1/search",
                params={"query": query, "type": "search", "limit": limit, "apikey": self.apikey},
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
            })
        logger.info(f"Prowlarr: {len(out)} {protocol or 'all'}-results for {query!r}")
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
                f"seeders={best['seeders']} from {best['indexer']}")
    return best


def _drop_year_mismatches(results: list[dict], title: str, series_year) -> list[dict]:
    kept = [r for r in results if not year_mismatch(r.get("title", ""), series_year)]
    if len(kept) < len(results):
        logger.info(f"Prowlarr: dropped {len(results) - len(kept)} year-mismatched result(s) "
                    f"for {title!r} (series began {series_year})")
    return kept


def search_torrent_pack(prowlarr: ProwlarrClient, title: str, series_year=None) -> dict | None:
    """Best torrent pack/collection for a series. Returns the result dict (magnet,
    seeders, title, size) or None. Twin of usenet_client.search_usenet_pack."""
    results = []
    for q in (f"{title} complete", title):
        results = prowlarr.search(q, protocol="torrent")
        if results:
            break
    results = _drop_year_mismatches(results, title, series_year)
    if not results:
        return None
    return _best_downloadable_torrent(
        results, lambda r: _pack_score(r["title"], title, r["size"]) + _seed_bonus(r["seeders"]))


def search_torrent(prowlarr: ProwlarrClient, title: str, issue_number: float, series_year=None,
                   store_date: str | None = None) -> dict | None:
    """Best torrent for a single issue. Returns the result dict or None. Twin of
    usenet_client.search_usenet.

    A single-issue result must carry BOTH the series name (+10) AND issue-number
    evidence (+5) BEFORE seeders count for anything — name-substring alone used
    to hit the old bar of 10 exactly (and seeders could have vaulted it over any
    combined threshold), which is how a well-seeded music album containing the
    word 'Ripcord' got queued for issue #0.

    store_date (ISO 'YYYY-MM-DD') demotes releases posted well before the issue
    existed — see _is_stale."""
    num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
    results = prowlarr.search(f"{title} {num_int}", protocol="torrent")
    results = _drop_year_mismatches(results, title, series_year)
    if not results:
        return None

    def _score(r):
        base = _nzb_score(r["title"], title, issue_number)
        if base < 15:            # name+number or nothing — seeders can't buy evidence
            return 0
        return base + _seed_bonus(r["seeders"])

    return _best_downloadable_torrent(results, _score, min_score=15, store_date=store_date)


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
                f"grabs={best['grabs']} from {best['indexer']}")
    return best


def search_usenet(prowlarr: ProwlarrClient, title: str, issue_number: float, series_year=None,
                  store_date: str | None = None) -> str | None:
    """Best usenet NZB for a single issue. Returns the NZB download URL or None —
    a drop-in for usenet_client.search_usenet, but sourced through Prowlarr so it
    sees every usenet indexer Prowlarr aggregates. Same evidence bar as the
    torrent twin: series name (+10) AND issue-number evidence (+5) before we act.
    store_date demotes releases that predate the issue — see _is_stale."""
    num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
    results = prowlarr.search(f"{title} {num_int}", protocol="usenet")
    results = _drop_year_mismatches(results, title, series_year)
    if not results:
        return None
    def _score(r):
        base = _nzb_score(r["title"], title, issue_number)
        return base if base >= 15 else 0     # name+number or nothing
    best = _best_usenet(results, _score, min_score=15, store_date=store_date)
    return best["url"] if best else None


def search_usenet_pack(prowlarr: ProwlarrClient, title: str, series_year=None) -> str | None:
    """Best usenet pack/collection for a series. Returns the NZB url or None.
    Drop-in for usenet_client.search_usenet_pack, sourced through Prowlarr."""
    results = []
    for q in (f"{title} complete", title):
        results = prowlarr.search(q, protocol="usenet")
        if results:
            break
    results = _drop_year_mismatches(results, title, series_year)
    if not results:
        return None
    best = _best_usenet(results, lambda r: _pack_score(r["title"], title, r["size"]), min_score=10)
    return best["url"] if best else None
