"""ComicVine search client — the gap-filler for LOCG. LOCG misses chunks of
vintage event/collection content (e.g. Batman: Knightquest); ComicVine, the most
complete comics DB, has it. Read-only: volume search for Add Series, plus a
volume's issue list for populating a tracked series on add.

CV "volumes" are series AND collected editions, so a TPB like
"Batman: Knightquest: The Crusade" is a searchable volume.

CV 403s requests without a descriptive User-Agent, and volume detail endpoints
take a '4050-' (volume resource-type) prefix on the id.
"""
import logging
import requests

logger = logging.getLogger(__name__)

_CV_BASE = "https://comicvine.gamespot.com/api"
_UA = "Kometa/1.0 (comic library manager)"
_VOLUME_PREFIX = "4050-"  # CV resource-type id for volumes


class ComicVineClient:
    def __init__(self, apikey: str):
        self.apikey = apikey
        self.session = requests.Session()
        self.session.headers["User-Agent"] = _UA

    def _get(self, path: str, **params) -> dict:
        params.update({"api_key": self.apikey, "format": "json"})
        r = self.session.get(f"{_CV_BASE}/{path}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def test(self) -> bool:
        """True if the key is valid and CV answers."""
        try:
            d = self._get("search/", query="batman", resources="volume", limit=1)
            return d.get("status_code") == 1
        except Exception as e:
            logger.warning(f"ComicVine test failed: {e}")
            return False

    def search_volumes(self, query: str, limit: int = 12) -> list[dict]:
        """Search volumes (series + collected editions). Returns normalized dicts:
        {name, year, publisher, cv_volume_id, issue_count}. CV ranks by relevance,
        so the on-topic editions come first."""
        try:
            d = self._get(
                "search/", query=query, resources="volume", limit=limit,
                field_list="name,start_year,publisher,id,count_of_issues",
            )
        except Exception as e:
            logger.warning(f"ComicVine search failed for {query!r}: {e}")
            return []
        out = []
        for r in (d.get("results") or [])[:limit]:
            p = r.get("publisher")
            out.append({
                "name": r.get("name", ""),
                "year": r.get("start_year"),
                "publisher": p.get("name") if isinstance(p, dict) else p,
                "cv_volume_id": r.get("id"),
                "issue_count": r.get("count_of_issues"),
            })
        logger.info(f"ComicVine: {len(out)} volume results for {query!r}")
        return out

    def get_volume_issues(self, cv_volume_id) -> list[dict]:
        """The issue list for a volume — used to populate a tracked series on add.
        Returns CV issue stubs ({id, issue_number, name, ...})."""
        try:
            d = self._get(f"volume/{_VOLUME_PREFIX}{cv_volume_id}/", field_list="issues")
        except Exception as e:
            logger.warning(f"ComicVine volume {cv_volume_id} issues failed: {e}")
            return []
        return (d.get("results") or {}).get("issues") or []
