"""A clean ComicInfo.xml from Kometa's record (docs/library-enrichment-spec.md, 3a).

The uploader's ComicInfo is patchy and often wrong (a 'Vol. 1' series name, a
scan group in the notes, a 2009 date on a 1963 comic). The user's call
(2026-10-12): whichever sanitises — so this writes a whole new document from the
record and drops theirs. Only ComicInfo's own fields (v2.0 schema order); a
field the record doesn't know is left out, never guessed.

This module only MAKES the XML. Putting it in a file is step 3's rebuild job.
"""
import hashlib
import re
import xml.etree.ElementTree as ET

import kometa.db as db

DB_PATH = db.DB_PATH

# Role → ComicInfo field. Lower-cased; combined roles ('Penciller, Inker') are
# split first. 'Artist' / 'Illustrator' drew the whole page: penciller AND inker.
_ROLE_FIELDS = {
    "writer": ("Writer",), "script": ("Writer",), "scripter": ("Writer",), "story": ("Writer",),
    "plot": ("Writer",), "dialogue": ("Writer",), "adapter": ("Writer",),
    "penciller": ("Penciller",), "pencils": ("Penciller",), "breakdowns": ("Penciller",), "layouts": ("Penciller",),
    "artist": ("Penciller", "Inker"), "illustrator": ("Penciller", "Inker"),
    "inker": ("Inker",), "inks": ("Inker",), "finishes": ("Inker",),
    "colorist": ("Colorist",), "colors": ("Colorist",), "colourist": ("Colorist",),
    "color assists": ("Colorist",), "color separations": ("Colorist",), "color flats": ("Colorist",),
    "letterer": ("Letterer",), "letters": ("Letterer",),
    "cover": ("CoverArtist",), "cover artist": ("CoverArtist",), "cover penciller": ("CoverArtist",),
    "cover inker": ("CoverArtist",), "cover colorist": ("CoverArtist",),
    "editor": ("Editor",), "associate editor": ("Editor",), "assistant editor": ("Editor",),
    "group editor": ("Editor",), "senior editor": ("Editor",),
    "translator": ("Translator",),
}
# Mastheads and production credits aren't who made the comic — left out on purpose:
# editor-in-chief, executive editor/producer, designer, logo design, production…

# ComicInfo's AgeRating is a fixed list; Metron's names map onto it
_AGE = {"everyone": "Everyone", "all ages": "Everyone", "teen": "Teen", "teen plus": "Teen",
        "mature": "Mature 17+", "explicit": "Adults Only 18+", "adult": "Adults Only 18+"}

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(s) -> str:
    """No control characters (invalid in XML 1.0), no runs of whitespace."""
    return re.sub(r"\s+", " ", _CTRL.sub("", str(s or ""))).strip()


def _number(n) -> str:
    """605.1 stays 605.1; 12.0 is '12'; 0.5 is '1/2' the way every reader writes it."""
    if n is None:
        return ""
    f = float(n)
    if f == 0.5:
        return "1/2"
    return str(int(f)) if f == int(f) else f"{f:g}"


def _people(credits: list[dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for c in credits or []:
        name = _clean(c.get("name"))
        if not name:
            continue
        for role in re.split(r"\s*,\s*", (c.get("role") or "").lower()):
            for field in _ROLE_FIELDS.get(role.strip(), ()):
                if name not in out.setdefault(field, []):
                    out[field].append(name)
    return out


def _genres(series_rec: dict) -> list[str]:
    import json
    g = series_rec.get("genres")
    if g is None and series_rec.get("genres_json"):
        try:
            g = json.loads(series_rec["genres_json"])
        except (TypeError, ValueError):
            g = None
    return [str(x) for x in g] if isinstance(g, list) else []


def _date(row: dict) -> tuple[str, str, str]:
    """ComicInfo's date is the cover date by tradition; the store date stands in."""
    d = (row.get("cover_date") or row.get("store_date") or "")[:10]
    m = re.match(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", d)
    if not m:
        return "", "", ""
    y, mo, dd = m.group(1), (m.group(2) or ""), (m.group(3) or "")
    return y, str(int(mo)) if mo and mo != "00" else "", str(int(dd)) if dd and dd != "00" else ""


# v2.0 schema order — readers don't care, people diffing files do
_ORDER = ("Title", "Series", "Number", "Count", "Volume", "AlternateSeries", "AlternateNumber", "StoryArc",
          "SeriesGroup", "Summary", "Notes", "Year", "Month", "Day", "Writer", "Penciller", "Inker", "Colorist",
          "Letterer", "CoverArtist", "Editor", "Translator", "Publisher", "Imprint", "Genre", "Web", "PageCount",
          "LanguageISO", "Format", "Characters", "Teams", "Locations", "GTIN", "AgeRating", "CommunityRating")


def fields(series: dict, row: dict, page_count: int | None = None, series_rec: dict | None = None) -> dict:
    """The ComicInfo fields for one issue, as strings, only the ones known."""
    extra = row.get("extra") or {}
    series_rec = series_rec or {}
    people = _people(row.get("credits") or [])
    y, mo, d = _date(row)
    title = extra.get("title") or row.get("title") or "; ".join(extra.get("story_titles") or [])
    stype = extra.get("series_type") or series_rec.get("series_type") or ""
    community = row.get("community") or {}
    rating = community.get("rating")
    ids = [f"{k} {v}" for k, v in (("Metron issue", row.get("metron_issue_id")), ("ComicVine issue", row.get("cv_issue_id")),
                                   ("GCD issue", extra.get("gcd_id")), ("LOCG issue", row.get("locg_issue_id"))) if v]
    sources = ", ".join(dict.fromkeys(s for s in (row.get("source"),) if s))
    f = {
        "Title": title,
        "Series": series.get("title"),
        "Number": _number(row.get("number")),
        "Volume": series.get("year_began") or series_rec.get("year_began") or "",
        "StoryArc": ", ".join(a for a in (row.get("arcs") or []) if a),
        "Summary": row.get("desc"),
        "Notes": (f"Tagged by Kometa from {sources}. " if sources else "Tagged by Kometa. ") + "; ".join(ids),
        "Year": y, "Month": mo, "Day": d,
        **{k: ", ".join(v) for k, v in people.items()},
        "Publisher": extra.get("publisher") or series.get("publisher") or series_rec.get("publisher"),
        "Imprint": extra.get("imprint"),
        "Genre": ", ".join(_genres(series_rec)),
        "PageCount": page_count or "",
        # Metron, LOCG and ComicVine catalogue English-language editions; GCD says per series
        "LanguageISO": "en" if row.get("source") in ("metron", "locg") else (extra.get("language") or ""),
        "Format": {"single issue": "Series"}.get(stype.lower(), stype),
        "Characters": ", ".join(extra.get("characters") or []),
        "Teams": ", ".join(extra.get("teams") or []),
        "GTIN": extra.get("upc") or row.get("isbn") or "",
        "AgeRating": _AGE.get((extra.get("age_rating") or "").lower(), ""),
        "CommunityRating": f"{float(rating):.1f}" if isinstance(rating, (int, float)) and 0 <= rating <= 5 else "",
    }
    return {k: _clean(f[k]) for k in _ORDER if k in f and _clean(f[k])}


def build(series: dict, row: dict, page_count: int | None = None, series_rec: dict | None = None) -> bytes:
    root = ET.Element("ComicInfo", {"xmlns:xsd": "http://www.w3.org/2001/XMLSchema",
                                    "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance"})
    for k, v in fields(series, row, page_count, series_rec).items():
        ET.SubElement(root, k).text = v
    ET.indent(root, "  ")
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def digest(xml: bytes) -> str:
    """What the rebuild job compares: same digest = the file already says this."""
    return hashlib.sha256(xml).hexdigest()[:16]


def for_issue(series_id: int, number: float, path=None, page_count: int | None = None) -> bytes | None:
    """The XML for one issue from the live record, or None when the record has
    nothing to say (no row, or only a miss)."""
    from kometa import record
    path = path or DB_PATH
    series = db.get_series_by_id(series_id, path)
    row = record.get_issue(series_id, float(number), path)
    if not series or not row or row.get("fill_state") == "miss":
        return None
    row = dict(row, number=float(number))
    return build(series, row, page_count, record.get_series(series_id, path))
