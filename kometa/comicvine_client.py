"""ComicVine search client — the gap-filler for LOCG. LOCG misses chunks of
vintage event/collection content (e.g. Batman: Knightquest); ComicVine, the most
complete comics DB, has it. Read-only: volume search for Add Series, plus a
volume's issue list for populating a tracked series on add.

CV "volumes" are series AND collected editions, so a TPB like
"Batman: Knightquest: The Crusade" is a searchable volume.

CV 403s requests without a descriptive User-Agent, and volume detail endpoints
take a '4050-' (volume resource-type) prefix on the id.
"""
import re
import logging
import requests

logger = logging.getLogger(__name__)

_CV_BASE = "https://comicvine.gamespot.com/api"
_UA = "Kometa/1.0 (comic library manager)"
_VOLUME_PREFIX = "4050-"  # CV resource-type id for volumes
_ARC_PREFIX = "4045-"     # CV resource-type id for story arcs
# .../batman-491-the-freedom-of-madness/4000-37038/  → grabs the title slug
_SLUG_RE = re.compile(r'/([a-z0-9-]+)/\d+-\d+/?$')


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

    # --- story arcs (the cross-title event layer) ---

    def search_arcs(self, query: str, limit: int = 10) -> list[dict]:
        """Search story arcs by name. Returns [{name, cv_arc_id, publisher}]. CV
        splits some events into multiple arcs ('Knightquest: The Crusade' + '…The
        Search') — caller groups by name prefix if it wants one logical event."""
        try:
            d = self._get("story_arcs/", filter=f"name:{query}", limit=limit,
                          field_list="name,id,publisher")
        except Exception as e:
            logger.warning(f"ComicVine arc search failed for {query!r}: {e}")
            return []
        out = []
        for r in (d.get("results") or [])[:limit]:
            p = r.get("publisher")
            out.append({
                "name": r.get("name", ""),
                "cv_arc_id": r.get("id"),
                "publisher": p.get("name") if isinstance(p, dict) else p,
            })
        logger.info(f"ComicVine: {len(out)} arc results for {query!r}")
        return out

    def get_arc_issues(self, cv_arc_id) -> list[dict]:
        """Ordered cross-title issue list for an arc — the arc's `issues` array IS
        the reading order. Resolves series+number from each issue's URL slug in the
        SAME call (no 23 per-issue lookups). Returns
        [{order, series, number, title, cv_issue_id}].

        Caveat: anthology slugs that embed a year ("Showcase '93") can mis-read the
        number; `number_uncertain` flags those for a per-issue-lookup refinement in
        the populate path."""
        try:
            d = self._get(f"story_arc/{_ARC_PREFIX}{cv_arc_id}/", field_list="issues")
        except Exception as e:
            logger.warning(f"ComicVine arc {cv_arc_id} issues failed: {e}")
            return []
        out = []
        for n, i in enumerate((d.get("results") or {}).get("issues") or [], 1):
            series, num = self._parse_slug(i.get("site_detail_url"))
            # a "number" ≥ 1900 is almost certainly a year baked into an anthology
            # slug, not the issue number — flag for accurate per-issue resolution.
            uncertain = num.isdigit() and int(num) >= 1900
            out.append({
                "order": n, "series": series, "number": num, "title": i.get("name", ""),
                "cv_issue_id": i.get("id"), "number_uncertain": uncertain,
            })
        return out

    @staticmethod
    def _parse_slug(url: str | None) -> tuple[str, str]:
        """'.../batman-491-the-freedom-of-madness/4000-37038/' → ('Batman', '491').
        First all-digit slug token is the issue number; tokens before it = series."""
        m = _SLUG_RE.search(url or "")
        if not m:
            return ("", "?")
        parts = m.group(1).split("-")
        for idx, p in enumerate(parts):
            if p.isdigit():
                return (" ".join(parts[:idx]).title(), p)
        return (" ".join(parts).title(), "?")
