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
    # a record read back out of our own file says where it came from first time round —
    # so a retag of it writes the same Notes, not 'from comicinfo-kometa'
    src = extra.get("kometa_from") if row.get("source") == OURS_SOURCE else row.get("source")
    sources = ", ".join(dict.fromkeys(s for s in (src,) if s))
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
    srec = record.get_series(series_id, path)
    if merged_genres_on(path):
        srec = dict(srec or {}, genres=genre_line(series_id, path))
    return build(series, row, page_count, srec)


def merged_genres_on(path=None) -> bool:
    """Recommendations step 2c, behind config comicinfo_genres = 'merged'. Off, <Genre>
    is the files' own old line (genres_json) and nothing gets re-queued for it. On, it
    is the merged genres — and every tagged file whose line changes goes back through
    retag's recheck (requeue_changed), paced at its tick budget. That's most of the
    library, so the owner decides when (2026-10-16)."""
    return db.get_config(path or DB_PATH).get("comicinfo_genres") == "merged"


def genre_line(series_id: int, path=None) -> list[str]:
    """<Genre> from record.effective_tags: Metron's genres first, then GCD's and the
    files' themes; formats and ratings never. 'post-apocalyptic' → 'Post-Apocalyptic'."""
    from kometa import record
    def title(key: str) -> str:
        return " ".join("-".join(p[:1].upper() + p[1:] for p in w.split("-")) for w in key.split())
    return [title(t["key"]) for t in record.effective_tags(series_id, path or DB_PATH)]


# --- collected editions --------------------------------------------------------------
def trade_fields(series: dict, t: dict, page_count: int | None = None) -> dict:
    """A trade's ComicInfo: the series it collects, its VOLUME (never a Number —
    readers group by series + number, so 'Number 1' filed DIE HC as issue #1 beside
    Die #001; 2026-10-14), its format, ISBN, and what it collects in the summary.
    Trade records carry no credits — none are invented."""
    vol = t.get("vol")
    y, mo, d = _date({"store_date": t.get("store_date")})
    collects = [f"{c.get('series') or ''} #{c.get('number')}".strip() for c in (t.get("collects") or [])
                if isinstance(c, dict) and c.get("number") is not None]
    summary = t.get("desc") or ""
    if collects:
        summary = (summary + "\n\n" if summary else "") + "Collects " + ", ".join(collects)
    ids = [f"{k} {v}" for k, v in (("Metron issue", t.get("metron_issue_id")), ("LOCG", t.get("locg_id"))) if v]
    f = {
        "Title": t.get("subtitle") or t.get("edition_title") or t.get("title"),
        "Series": series.get("title"),
        "Volume": _number(vol) if vol is not None else "",
        "Summary": summary,
        "Notes": "Tagged by Kometa (collected edition). " + "; ".join(ids),
        "Year": y, "Month": mo, "Day": d,
        "Publisher": series.get("publisher"),
        "PageCount": page_count or t.get("page_count") or "",
        "Format": {"tpb": "TPB", "hc": "Hardcover", "gn": "Graphic Novel", "omnibus": "Omnibus"}.get(
            (t.get("format") or "").lower(), t.get("format") or ""),
        "GTIN": t.get("isbn") or "",
    }
    return {k: _clean(f[k]) for k in _ORDER if k in f and _clean(f[k])}


def _owned_trade(series_id: int, filename: str, path) -> dict | None:
    """The series' trade-list entry whose landed file is this one."""
    cached = db.get_trades(series_id, path)
    for t in (cached or {}).get("trades") or []:
        if t.get("file") and t["file"] == filename:
            return t
    return None


def for_book(book: dict, path=None, page_count: int | None = None) -> bytes | None:
    """The XML for a shelf book: its issue's record, or — numberless in a tracked
    series — the trade it is. None when kometa knows nothing about it."""
    import os
    path = path or DB_PATH
    sid = book.get("tracked_series_id")
    if not sid:
        return None
    if book.get("number") is not None:
        return for_issue(sid, book["number"], path, page_count)
    series = db.get_series_by_id(sid, path)
    t = series and _owned_trade(sid, os.path.basename(book.get("path") or ""), path)
    if not t:
        return None
    root = ET.Element("ComicInfo", {"xmlns:xsd": "http://www.w3.org/2001/XMLSchema",
                                    "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance"})
    for k, v in trade_fields(series, t, page_count).items():
        ET.SubElement(root, k).text = v
    ET.indent(root, "  ")
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


# --- reading our own back (enrichment 5a) ----------------------------------------------
# A file Kometa tagged already carries its record: parsed back, a rebuilt DB (or a
# fresh shelf) costs nobody a catalogue request. Recognised by the Notes marker
# fields() writes; an uploader's ComicInfo is not ours and stays a stopgap.
OURS_MARKER = "Tagged by Kometa"
OURS_SOURCE = "comicinfo-kometa"      # the record's source for a row read back out of our own file
_FIELD_ROLE = (("Writer", "Writer"), ("Penciller", "Penciller"), ("Inker", "Inker"), ("Colorist", "Colorist"),
               ("Letterer", "Letterer"), ("CoverArtist", "Cover"), ("Editor", "Editor"), ("Translator", "Translator"))
_AGE_BACK = {"Everyone": "Everyone", "Teen": "Teen", "Mature 17+": "Mature", "Adults Only 18+": "Explicit"}
_NOTE_IDS = (("metron_issue_id", r"Metron issue (\d+)"), ("cv_issue_id", r"ComicVine issue ([\w-]+)"),
             ("gcd_id", r"GCD issue (\d+)"), ("locg_issue_id", r"LOCG issue (\d+)"))


def _tags(xml) -> dict:
    """bytes / str / an already-flat {tag: text} dict (covers.read_comicinfo's) → flat dict."""
    if isinstance(xml, dict):
        return xml
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, TypeError, ValueError):
        return {}
    return {el.tag.split("}")[-1]: el.text.strip() for el in root if el.text and el.text.strip()}


def _split(s: str | None) -> list[str]:
    return [p for p in re.split(r"\s*,\s*", s or "") if p]


def is_ours(xml) -> bool:
    return (_tags(xml).get("Notes") or "").startswith(OURS_MARKER)


def parse_ours(xml) -> dict | None:
    """The inverse of fields() for one ISSUE: ComicInfo Kometa wrote → the
    write_issue shape (desc, credits, arcs, cover_date, page_count, isbn, extra)
    plus the catalogue ids from Notes (metron_issue_id, cv_issue_id,
    locg_issue_id; GCD's into extra.gcd_id). None when the file isn't ours, or is
    a collected edition (a trade isn't an issue record)."""
    t = _tags(xml)
    notes = t.get("Notes") or ""
    if not notes.startswith(OURS_MARKER) or notes.startswith(OURS_MARKER + " (collected edition)"):
        return None
    credits = [{"role": role, "name": name, "metron_creator_id": None}
               for field, role in _FIELD_ROLE for name in _split(t.get(field))]
    y, mo, d = t.get("Year") or "", t.get("Month") or "", t.get("Day") or ""
    cover_date = None
    if y.isdigit():
        cover_date = f"{int(y):04d}"
        if mo.isdigit():
            cover_date += f"-{int(mo):02d}"
            if d.isdigit():
                cover_date += f"-{int(d):02d}"
    extra: dict = {}
    m = re.match(re.escape(OURS_MARKER) + r" from ([^.]+)\.", notes)
    if m:
        extra["kometa_from"] = m.group(1).strip()          # whose record it was when the file was written
    ids: dict = {}
    for key, rx in _NOTE_IDS:
        hit = re.search(rx, notes)
        if hit:
            v = hit.group(1)
            ids[key] = int(v) if key in ("metron_issue_id", "gcd_id") else v
    if "gcd_id" in ids:
        extra["gcd_id"] = ids.pop("gcd_id")
    if "cv_issue_id" in ids:
        extra["cv_id"] = ids["cv_issue_id"]
    for tag, key in (("Title", "title"), ("Publisher", "publisher"), ("Imprint", "imprint"), ("LanguageISO", "language")):
        if t.get(tag):
            extra[key] = t[tag]
    if t.get("Format"):
        extra["series_type"] = {"Series": "Single Issue"}.get(t["Format"], t["Format"])
    for tag, key in (("Characters", "characters"), ("Teams", "teams")):
        if t.get(tag):
            extra[key] = _split(t[tag])
    if t.get("AgeRating") in _AGE_BACK:
        extra["age_rating"] = _AGE_BACK[t["AgeRating"]]
    gtin, isbn = t.get("GTIN") or "", None
    if re.fullmatch(r"97[89]\d{10}", gtin):
        isbn = gtin                                         # an ISBN-13; anything else is the barcode
    elif gtin:
        extra["upc"] = gtin
    pages = t.get("PageCount") or ""
    return {"desc": t.get("Summary") or "", "credits": credits, "arcs": _split(t.get("StoryArc")), "covers": [],
            "cover_date": cover_date, "store_date": None, "page_count": int(pages) if pages.isdigit() else None,
            "price": None, "isbn": isbn, "extra": extra, **ids}
