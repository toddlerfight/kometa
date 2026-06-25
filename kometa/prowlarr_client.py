"""Prowlarr aggregate search — one query across every configured indexer, both
protocols. This is what lets Kometa SEE torrents: the newznab path
(usenet_client) only ever queried the usenet feeds, so torrent indexers were
invisible. Prowlarr's /api/v1/search returns usenet + torrent results in one
normalized shot, with seeders/grabs/age so the brain can choose.

Reuses the usenet scoring (_norm/_nzb_score/_pack_score) for title relevance and
layers a seeder weight on top — a healthy torrent completes, a 0-seeder one is a
corpse.
"""
import logging
import requests

from kometa.usenet_client import _norm, _nzb_score, _pack_score

logger = logging.getLogger(__name__)


def _seed_bonus(seeders: int) -> int:
    """+1 per 10 seeders, capped at +10. Tilts ties toward what will actually finish."""
    return min(int(seeders or 0), 100) // 10


class ProwlarrClient:
    def __init__(self, base_url: str, apikey: str):
        self.base_url = base_url.rstrip("/")
        self.apikey = apikey
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "kometa/1.0"

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


def _best_downloadable_torrent(results: list[dict], score_fn) -> dict | None:
    """Pick the highest-scoring torrent we can act on — has a download handle
    (a magnet OR a .torrent url; many indexers give the latter, no magnet) and at
    least one live seeder. Returns the result dict or None."""
    viable = [r for r in results if (r.get("magnet") or r.get("url")) and (r.get("seeders") or 0) >= 1]
    if not viable:
        return None
    scored = sorted(
        [(r, score_fn(r)) for r in viable],
        key=lambda x: (-x[1], -(x[0].get("seeders") or 0), -(x[0].get("size") or 0)),
    )
    best, score = scored[0]
    if score < 10:
        logger.info(f"Prowlarr torrent: best score {score} too low — skipping")
        return None
    logger.info(f"Prowlarr torrent: {best['title']!r} score={score} "
                f"seeders={best['seeders']} from {best['indexer']}")
    return best


def search_torrent_pack(prowlarr: ProwlarrClient, title: str) -> dict | None:
    """Best torrent pack/collection for a series. Returns the result dict (magnet,
    seeders, title, size) or None. Twin of usenet_client.search_usenet_pack."""
    results = []
    for q in (f"{title} complete", title):
        results = prowlarr.search(q, protocol="torrent")
        if results:
            break
    if not results:
        return None
    return _best_downloadable_torrent(
        results, lambda r: _pack_score(r["title"], title, r["size"]) + _seed_bonus(r["seeders"]))


def search_torrent(prowlarr: ProwlarrClient, title: str, issue_number: float) -> dict | None:
    """Best torrent for a single issue. Returns the result dict or None. Twin of
    usenet_client.search_usenet."""
    num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
    results = prowlarr.search(f"{title} {num_int}", protocol="torrent")
    if not results:
        return None
    return _best_downloadable_torrent(
        results, lambda r: _nzb_score(r["title"], title, issue_number) + _seed_bonus(r["seeders"]))
