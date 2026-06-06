import re
import logging
import requests

logger = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]', ' ', s.lower()).strip()


def _nzb_score(nzb_title: str, series: str, issue_number: float) -> int:
    """Score an NZB title for relevance. Higher is better."""
    t = _norm(nzb_title)
    s = _norm(series)
    score = 0
    if s in t:
        score += 10
    num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
    # Try #48, #048, " 48 ", " 048 "
    candidates = {f'#{num_int}', f'#{int(num_int):03d}', f' {num_int} ', f' {int(num_int):03d} '}
    padded = f' {t} '
    if any(c.lower() in padded for c in candidates):
        score += 5
    return score


class NewznabClient:
    def __init__(self, name: str, host: str, apikey: str, ssl: bool = True):
        self.name = name
        self.base = f"{'https' if ssl else 'http'}://{host}/api"
        self.apikey = apikey
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "kometa/1.0"

    def search(self, query: str, categories: str = "7030") -> list[dict]:
        try:
            r = self.session.get(
                self.base,
                params={
                    "t": "search",
                    "apikey": self.apikey,
                    "q": query,
                    "cat": categories,
                    "o": "json",
                },
                timeout=15,
            )
            r.raise_for_status()
            text = r.text.strip()
            if not text:
                return []
            # Try JSON first; fall back to XML (Atom/RSS)
            if text.startswith("{"):
                results = self._parse_json(r.json())
            else:
                results = self._parse_xml(text)
            logger.info(f"Newznab {self.name}: {len(results)} results for {query!r}")
            return results
        except Exception as e:
            logger.warning(f"Newznab {self.name} search failed: {e}")
            return []

    def _parse_json(self, data: dict) -> list[dict]:
        items = data.get("channel", {}).get("item", [])
        if not isinstance(items, list):
            items = [items] if items else []
        results = []
        for item in items:
            enc = item.get("enclosure") or {}
            attrs = enc.get("@attributes", enc) if isinstance(enc, dict) else {}
            url = attrs.get("url") or item.get("link", "")
            if not url:
                continue
            try:
                size = int(attrs.get("length", 0) or item.get("size", 0) or 0)
            except (ValueError, TypeError):
                size = 0
            results.append({"title": item.get("title", ""), "url": url, "size": size, "indexer": self.name})
        return results

    def _parse_xml(self, text: str) -> list[dict]:
        import xml.etree.ElementTree as ET
        results = []
        try:
            root = ET.fromstring(text)
            # RSS: <rss><channel><item>...</item></channel></rss>
            for item in root.iter("item"):
                title_el = item.find("title")
                title = title_el.text if title_el is not None else ""
                enc = item.find("enclosure")
                if enc is not None:
                    url = enc.get("url", "")
                    try:
                        size = int(enc.get("length", 0) or 0)
                    except (ValueError, TypeError):
                        size = 0
                else:
                    link_el = item.find("link")
                    url = link_el.text if link_el is not None else ""
                    size = 0
                if url:
                    results.append({"title": title, "url": url, "size": size, "indexer": self.name})
        except ET.ParseError as e:
            logger.warning(f"Newznab {self.name} XML parse failed: {e}")
        return results


_PACK_KEYWORDS_RE = re.compile(r'\b(complete|collection|pack|omnibus|complet)\b', re.I)
_RANGE_RE = re.compile(r'#\s*\d+\s*[-–]\s*#?\s*\d+')
PACK_THRESHOLD = 5


def _pack_score(nzb_title: str, series: str, size: int) -> int:
    t = _norm(nzb_title)
    s = _norm(series)
    if s not in t:
        return 0
    score = 10
    if _PACK_KEYWORDS_RE.search(nzb_title):
        score += 8
    if _RANGE_RE.search(nzb_title):
        score += 6
    if size > 50_000_000:
        score += 3
    if size > 200_000_000:
        score += 3
    return score


def search_usenet_pack(indexers: list[dict], title: str) -> str | None:
    """Search for a series pack/collection NZB. Returns best NZB URL or None."""
    queries = [f"{title} complete", f"{title} pack", title]
    clients = [
        NewznabClient(name=idx["name"], host=idx["host"], apikey=idx["apikey"], ssl=bool(idx.get("ssl", True)))
        for idx in indexers
    ]
    if not clients:
        return None

    all_results: list[dict] = []
    for query in queries:
        for client in clients:
            all_results.extend(client.search(query))
        if all_results:
            break

    if not all_results:
        logger.info(f"Usenet pack: no results for {title!r}")
        return None

    scored = sorted(
        [(r, _pack_score(r["title"], title, r.get("size", 0))) for r in all_results],
        key=lambda x: (-x[1], -x[0].get("size", 0)),
    )
    best, score = scored[0]

    if score < 10:
        logger.info(f"Usenet pack: best score {score} too low for {title!r} — skipping")
        return None

    logger.info(f"Usenet pack: {best['title']!r} score={score} size={best.get('size', 0):,} from {best['indexer']}")
    return best["url"]


def search_usenet(indexers: list[dict], title: str, issue_number: float) -> str | None:
    """
    Search configured Newznab indexers for a comic issue.
    Returns the best NZB URL, or None if nothing useful found.
    """
    num_int = int(issue_number) if issue_number == int(issue_number) else issue_number
    queries = [
        f"{title} #{num_int}",
        f"{title} {int(num_int):03d}",
    ]

    clients = [
        NewznabClient(
            name=idx["name"],
            host=idx["host"],
            apikey=idx["apikey"],
            ssl=bool(idx.get("ssl", True)),
        )
        for idx in indexers
    ]
    if not clients:
        return None

    all_results: list[dict] = []
    for query in queries:
        for client in clients:
            all_results.extend(client.search(query))
        if all_results:
            break

    if not all_results:
        logger.info(f"Usenet: no results for {title!r} #{num_int}")
        return None

    scored = sorted(
        [(r, _nzb_score(r["title"], title, issue_number)) for r in all_results],
        key=lambda x: (-x[1], -x[0]["size"]),
    )
    best, score = scored[0]

    # Require series name to appear in NZB title
    if score < 10:
        logger.info(f"Usenet: best score {score} too low for {title!r} #{num_int} — skipping")
        return None

    logger.info(f"Usenet: {best['title']!r} score={score} from {best['indexer']}")
    return best["url"]
