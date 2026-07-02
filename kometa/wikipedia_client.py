"""Wikipedia (MediaWiki API) as an arc DISCOVERY source.

Answers the one question ComicVine can't — "what story arcs is this series in?" —
because CV's story_arc_credits field is empty. Keyless; just a descriptive
User-Agent. Reading ORDER stays ComicVine's job (precise, cross-title, real issue
numbers). Here we only surface arc names + rough issue ranges parsed from a series'
collected-editions / story-arcs wikitable.
"""
import re
import logging

import requests

from kometa.naming import norm_key as _norm

logger = logging.getLogger(__name__)

_API = "https://en.wikipedia.org/w/api.php"
_UA = "Kometa/1.0 (comic library manager; arc discovery; +https://github.com/)"

# A trailing edition phrase marks a re-release, not the series ("The Walking Dead
# Deluxe" -> "The Walking Dead"). Resolve the base series' article instead.
_EDITION_RE = re.compile(
    r"\s*[-:]?\s*\b(deluxe|omnibus|compendium|the complete|collected|library|"
    r"edition|hardcover|tpb)\b.*$", re.I)
_ARC_SECTION_RE = re.compile(
    r"\b(collected|story arc|trade paperback|graphic novel|reading order)\b", re.I)
_RANGE_RE = re.compile(r"#?\s*(\d+)\s*[–\-—]\s*#?\s*(\d+)")
_SINGLE_RE = re.compile(r"#\s*(\d+)\b")
_COMIC_SUFFIXES = ("(comic book)", "(comics)", "(comic)", "(comic strip)",
                   "(comic series)")


def _arc_name(name):
    """'The Walking Dead Vol. 1: Days Gone Bye' -> 'Days Gone Bye'. Keeps names that
    carry no arc subtitle (e.g. 'Saga Vol. 1') as-is."""
    m = re.search(r"\bvol(?:ume|\.)?\s*\d+\s*[:\-–]\s*(.+)$", name, re.I)
    return m.group(1).strip() if m else name


def _valid_range(a, b):
    """A plausible issue range — not an ISBN (978/979…), year, or mush."""
    return 1 <= a <= b <= 600 and (b - a) <= 60 and a < 900


def _clean(cell):
    """Strip wiki markup from a table cell down to readable text."""
    c = re.sub(r"<ref.*?</ref>", "", cell, flags=re.S)
    c = re.sub(r"<ref[^>]*/>", "", c)
    c = re.sub(r"\{\{[^{}]*\}\}", "", c)            # templates
    c = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", c)  # [[A|B]] -> B
    c = re.sub(r"\[\[([^\]]*)\]\]", r"\1", c)        # [[A]] -> A
    c = re.sub(r"'{2,}", "", c)                      # bold/italic
    c = re.sub(r"<[^>]+>", "", c)                    # stray html
    return c.strip(" |:-\t")


class WikipediaClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = _UA

    def _get(self, **params):
        params.update({"format": "json", "formatversion": "2"})
        r = self.session.get(_API, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _base_title(title):
        return _EDITION_RE.sub("", title or "").strip(" -:") or (title or "")

    def resolve_page(self, title, year=None):
        """Best-guess Wikipedia article title for a comic series, or None. Prefers a
        comic-disambiguated page ('X (comic book)') over the bare title."""
        base = self._base_title(title)
        try:
            d = self._get(action="query", list="search",
                          srsearch=f"{base} comic", srlimit=10)
        except Exception as e:
            logger.warning(f"Wikipedia search failed for {title!r}: {e}")
            return None
        hits = [h["title"] for h in d.get("query", {}).get("search", [])]
        if not hits:
            return None
        nb = _norm(base)
        for suf in _COMIC_SUFFIXES:                 # disambiguated comic page first
            want = f"{nb} {_norm(suf)}"
            for h in hits:
                if _norm(h) == want:
                    return h
        for h in hits:                              # then an exact title match
            if _norm(h) == nb:
                return h
        return hits[0]                              # else the top hit

    def discover_arcs(self, title, year=None):
        """[{name, first_issue, last_issue, page}] from the series' collected-editions
        wikitable, or []. Discovery only — precision is ComicVine's."""
        page = self.resolve_page(title, year)
        if not page:
            return []
        try:
            d = self._get(action="parse", page=page, prop="wikitext", redirects=1)
        except Exception as e:
            logger.warning(f"Wikipedia parse failed for {page!r}: {e}")
            return []
        wikitext = (d.get("parse") or {}).get("wikitext") or ""
        arcs = self._parse_arcs(wikitext)
        for a in arcs:
            a["page"] = page
        logger.info(f"Wikipedia: {len(arcs)} arcs for {title!r} (page {page!r})")
        return arcs

    @staticmethod
    def _cells(row):
        cells = []
        for line in row.split("\n"):
            line = line.strip()
            if not line or line.startswith(("|+", "|-", "{|", "|}")):
                continue
            if line[0] in "|!":
                line = line[1:]
                for part in re.split(r"\|\||!!", line):
                    # drop a leading cell-attribute segment ("style=... | text")
                    if "|" in part and "[[" not in part.split("|")[0]:
                        part = part.split("|", 1)[1]
                    cells.append(part)
        return cells

    @staticmethod
    def _row_name(cells):
        # Prefer a wikilinked/italic title cell (the volume name); else first text cell.
        for prefer in (True, False):
            for c in cells:
                if _RANGE_RE.search(c):
                    continue
                if prefer and "[[" not in c and "''" not in c:
                    continue
                t = _arc_name(_clean(c))
                if t and not t.isdigit() and len(t) > 2:
                    return t
        return None

    @staticmethod
    def _arc_sections(wikitext):
        """Bodies of sections whose heading names collected editions / story arcs —
        the section is a precise filter, so even a 1-2 row table (new series) counts."""
        parts = re.split(r"\n(==+[^=\n]+==+)\n", wikitext)
        out = []
        for i in range(1, len(parts), 2):
            if _ARC_SECTION_RE.search(parts[i]) and i + 1 < len(parts):
                out.append(parts[i + 1])
        return out

    @classmethod
    def _table_candidates(cls, table):
        cand = []
        for row in re.split(r"\n\|-", table):
            cells = cls._cells(row)
            rng = None
            for c in cells:
                m = _RANGE_RE.search(c)
                if m and _valid_range(int(m.group(1)), int(m.group(2))):
                    rng = (int(m.group(1)), int(m.group(2)))
                    break
            if not rng:
                continue
            name = cls._row_name(cells)
            if name:
                cand.append((name, rng))
        return cand

    @classmethod
    def _parse_arcs(cls, wikitext):
        # Two passes, unioned: (1) tables inside collected-editions/story-arc sections
        # accept even 1-2 rows (short/new series like Absolute Batman); (2) ANY table
        # with >=3 valid rows — the broad net that also filters stray creator/novel
        # tables. (1) rescues small tables (2) misses; (2) rescues big tables whose
        # heading (1) doesn't match.
        passes = [(b, 1) for b in cls._arc_sections(wikitext)] + [(wikitext, 3)]
        out, seen = [], set()
        for block, require_min in passes:
            for table in re.findall(r"\{\|.*?\n\|\}", block, re.S):
                cand = cls._table_candidates(table)
                if len(cand) < require_min:
                    continue
                for name, (a, b) in cand:
                    key = _norm(name)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"name": name, "first_issue": a, "last_issue": b})
        return out


if __name__ == "__main__":
    import sys
    c = WikipediaClient()
    for t in sys.argv[1:]:
        arcs = c.discover_arcs(t)
        print(f"\n=== {t} -> page {c.resolve_page(t)!r} -> {len(arcs)} arcs ===")
        for a in arcs[:40]:
            print(f"   {a['name']!r}  #{a['first_issue']}-{a['last_issue']}")
