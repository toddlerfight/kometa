"""GCD off the disk — the website's answers out of GCD's own download.

comics.org/download/ hands out the whole database as one SQLite file (~6.7 GB; private use
is unrestricted). gcd_client asks here first and only knocks on the website for what the
file can't answer: an issue or series newer than the snapshot, or a series search the file
finds nothing for. Answers come back in the website's JSON shape (gcd-django
apps/api/serializers.py, credits.py, story.py), so nothing above gcd_client can tell which
one answered. No covers: GCD doesn't put them in the download — 'cover' is always ''.

The file lives next to Kometa's own database as gcd.db (config 'gcd_db_path' overrides).
Refresh = download a newer one and move it over the old: the mtime changes and every
thread reopens. Opened immutable — nothing here ever writes to it.
"""
import os
import re
import sqlite3
import threading
import time
import urllib.parse

MISS = object()                 # the file can't answer this one: go and ask the website
API = "https://www.comics.org/api/"
_CHECK_S = 60                   # how long a 'there / not there' verdict on the file holds
_SEARCH_CAP = 100               # the website pages 50 at a time and gcd_client reads two pages
_CREDIT_TYPES = (("script", 1), ("pencils", 2), ("inks", 3), ("colors", 4), ("letters", 5), ("editing", 6))
_IDENTITY = 2                   # gcd_character_relation_type: 'alias of' / 'secret identity of'

_local = threading.local()
_seen = {"db": None, "path": None, "at": 0.0, "ok": False}   # the last check, per Kometa database
_seen_lock = threading.Lock()


def db_path(path=None) -> str:
    from kometa import db
    path = path or db.DB_PATH
    try:
        override = (db.get_config(path).get("gcd_db_path") or "").strip()
    except Exception:
        override = ""
    return override or os.path.join(os.path.dirname(os.path.abspath(path)), "gcd.db")


class _Conn(sqlite3.Connection):
    top: dict                   # the highest issue / series id in the snapshot: past these the website may know more


def _open(p: str) -> _Conn:
    """This thread's handle on the file, reopened when the file is swapped for a newer one."""
    mtime = os.path.getmtime(p)
    cur = getattr(_local, "conn", None)
    if cur and cur[0] == (p, mtime):
        return cur[1]
    if cur:
        cur[1].close()
    conn = sqlite3.connect(f"file:{urllib.parse.quote(p)}?mode=ro&immutable=1", uri=True,
                           check_same_thread=False, factory=_Conn)
    conn.row_factory = sqlite3.Row
    top = conn.execute("SELECT (SELECT MAX(id) FROM gcd_issue), (SELECT MAX(id) FROM gcd_series)").fetchone()
    conn.top = {"issue": top[0] or 0, "series": top[1] or 0}
    _local.conn = ((p, mtime), conn)
    return conn


def _check(path=None) -> tuple[str, bool]:
    """(the file's path, is it a usable GCD dump) — re-checked every minute, not every call."""
    from kometa import db
    key = path or db.DB_PATH
    with _seen_lock:
        if _seen["db"] == key and time.time() - _seen["at"] < _CHECK_S:
            return _seen["path"], _seen["ok"]
    p = db_path(key)
    try:
        ok = os.path.isfile(p) and bool(_open(p).execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'gcd_issue'").fetchone())
    except (OSError, sqlite3.Error):
        ok = False
    with _seen_lock:
        _seen.update(db=key, path=p, at=time.time(), ok=ok)
    return p, ok


def available(path=None) -> bool:
    """GCD's download is next to this Kometa database (or where 'gcd_db_path' says)."""
    return _check(path)[1]


def info(path=None) -> dict:
    """For the status line: which snapshot, and how far it reaches."""
    p, ok = _check(path)
    if not ok:
        return {"file": False}
    conn = _open(p)
    return {"file": True, "path": p, "snapshot": time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(p))),
            "issues_to_id": conn.top["issue"], "series_to_id": conn.top["series"]}


# The dump ships without ANALYZE statistics, so SQLite reaches for the 'deleted' flag's index
# (millions of rows) over issue_id / story_id. Every '+x.deleted' below keeps it off that index.

# --- routing: the website's paths -------------------------------------------------------
_ISSUE = re.compile(r"^issue/(\d+)/?$")
_SERIES = re.compile(r"^series/(\d+)/?$")
_SEARCH = re.compile(r"^series/name/([^/]+)/(?:year/(\d+)/)?$")
_PUBLISHER = re.compile(r"^publisher/(\d+)/?$")


def answer(path: str, params: dict | None = None, db=None):
    """The website's JSON for this API path, None where the website would say 404, or MISS
    when only the website can say (newer than the snapshot, or a path we don't serve)."""
    p, ok = _check(db)
    if not ok:
        return MISS
    try:
        conn = _open(p)
    except (OSError, sqlite3.Error):
        return MISS
    path = path.lstrip("/")
    page = int((params or {}).get("page") or 1)
    if m := _ISSUE.match(path):
        iid = int(m.group(1))
        return MISS if iid > conn.top["issue"] else _issue(conn, iid)
    if m := _SERIES.match(path):
        sid = int(m.group(1))
        return MISS if sid > conn.top["series"] else _series(conn, sid)
    if m := _SEARCH.match(path):
        if page > 1:                                        # page 1 carried every hit
            return {"count": 0, "next": None, "results": []}
        hits = _search(conn, urllib.parse.unquote(m.group(1)), int(m.group(2)) if m.group(2) else None)
        return {"count": len(hits), "next": None, "results": hits} if hits else MISS
    if m := _PUBLISHER.match(path):
        r = conn.execute("SELECT name FROM gcd_publisher WHERE id = ? AND +deleted = 0", (int(m.group(1)),)).fetchone()
        return {"name": r["name"]} if r else MISS
    return MISS


def _url(kind: str, i) -> str | None:
    return f"{API}{kind}/{int(i)}/?format=json" if i else None


def _dec(v) -> str | None:
    """36 → '36.000', as the website's DecimalField prints it."""
    if v is None or v == "":
        return None
    try:
        return f"{float(v):.3f}"
    except (TypeError, ValueError):
        return str(v)


# --- series ------------------------------------------------------------------------------
def _descriptor(i) -> str:
    """Issue.full_descriptor: '368 - King Ottokar's Sceptre', 'v2#5', '12 [Newsstand]'."""
    number = i["number"] or ""
    if number == "[nn]" and i["is_singleton"]:
        base = ""
    else:
        title = f" - {i['title']}" if i["title"] and i["has_issue_title"] else ""
        if i["display_volume_with_number"]:
            vol = f"[v{i['volume']}]" if i["volume_not_printed"] else f"v{i['volume']}"
            base = f"{vol}#{number}{title}"
        else:
            base = number + title
    addon = f"[{i['variant_name']}]" if i["variant_name"] else "[unnamed variant]" if i["variant_of_id"] else ""
    return f"{base} {addon}" if base and addon else base + addon


def _series_rows(conn, where: str, args: tuple, limit: int = 1) -> list[dict]:
    rows = conn.execute(f"""
        SELECT s.*, c.code AS country_code, l.code AS language_code FROM gcd_series s
        LEFT JOIN stddata_country c ON c.id = s.country_id LEFT JOIN stddata_language l ON l.id = s.language_id
        WHERE +s.deleted = 0 AND {where} ORDER BY s.sort_name, s.year_began LIMIT {int(limit)}""", args).fetchall()
    out = []
    for s in rows:
        issues = conn.execute("""
            SELECT i.id, i.number, i.title, i.volume, i.display_volume_with_number, i.volume_not_printed,
                   i.variant_name, i.variant_of_id, ? AS has_issue_title, ? AS is_singleton
            FROM gcd_issue i WHERE i.series_id = ? AND +i.deleted = 0 ORDER BY i.sort_code""",
                              (s["has_issue_title"], s["is_singleton"], s["id"])).fetchall()
        out.append({
            "api_url": _url("series", s["id"]), "name": s["name"],
            "country": s["country_code"], "language": s["language_code"],
            "active_issues": [_url("issue", i["id"]) for i in issues],
            "issue_descriptors": [_descriptor(i) for i in issues],
            "color": s["color"], "dimensions": s["dimensions"], "paper_stock": s["paper_stock"],
            "binding": s["binding"], "publishing_format": s["publishing_format"], "notes": s["notes"],
            "year_began": s["year_began"], "year_ended": s["year_ended"],
            "publisher": _url("publisher", s["publisher_id"]),
        })
    return out


def _series(conn, sid: int) -> dict | None:
    rows = _series_rows(conn, "s.id = ?", (sid,))
    return rows[0] if rows else None


def _search(conn, name: str, year: int | None) -> list[dict]:
    """The website's series/name/ search: name contains, year_began exact when given."""
    like = "%" + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    if year:
        return _series_rows(conn, "s.name LIKE ? ESCAPE '\\' AND s.year_began = ?", (like, year), _SEARCH_CAP)
    return _series_rows(conn, "s.name LIKE ? ESCAPE '\\'", (like,), _SEARCH_CAP)


# --- one issue ---------------------------------------------------------------------------
def _credit_text(c) -> str:
    """CreatorNameDetail.display_credit(url=False), less the rare ghost/house/studio forms:
    'Gregory Wright (colors)', 'Hergé (credited as Herge)', 'Jack Kirby ?'."""
    name = c["name"] if c["is_official_name"] else (c["official"] or c["name"])
    if c["uncertain"]:
        name += " ?"
    text = name
    if not c["is_official_name"] and c["is_credited"] and not c["credited_as"]:
        text += f" (credited as {c['name']})"
    signed_as = c["signed_as"] if "signed_as" in c.keys() else None
    if c["credited_as"] and not (signed_as and c["credited_as"] == signed_as):
        text += f" (credited as {c['credited_as']})"
    is_signed = bool(c["is_signed"]) if "is_signed" in c.keys() else False
    if is_signed and not signed_as and c["is_credited"] and not c["credited_as"] and c["is_official_name"]:
        text += " (credited, signed)"
    elif is_signed and not signed_as:
        text += " (signed)"
    elif c["is_credited"] and not c["credited_as"] and c["is_official_name"]:
        text += " (credited)"
    if c["credited_as"] and signed_as and c["credited_as"] == signed_as:
        text += f" (credited, signed as {c['credited_as']})"
    elif signed_as:
        text += f" (signed as {signed_as})"
    if c["is_sourced"]:
        text += " (sourced)"
    if c["credit_name"]:
        text += f" ({c['credit_name']})"
    return text


def _credit_field(rows, type_id: int, old_text) -> str:
    """credits.__credit_value: the linked credits, then the old free-text field."""
    value = "; ".join(_credit_text(c) for c in rows if c["credit_type_id"] == type_id)
    if old_text:
        value = f"{value}; {old_text}" if value else old_text
    return value


def _characters(conn, story) -> str:
    """story.show_characters(url=False), less groups and universes: the linked cast, a secret
    identity folded in as 'Batman [Bruce Wayne]', then the old free-text field."""
    cast = conn.execute("""
        SELECT sc.character_id AS name_id, cnd.name, cnd.character_id AS who, sc.notes FROM gcd_story_character sc
        JOIN gcd_character_name_detail cnd ON cnd.id = sc.character_id
        WHERE sc.story_id = ? AND +sc.deleted = 0 ORDER BY sc.id""", (story["id"],)).fetchall()
    text = ""
    if cast:
        here = {c["who"]: c["name"] for c in cast}
        rel = conn.execute(f"""
            SELECT from_character_id, to_character_id FROM gcd_character_relation
            WHERE +relation_type_id = {_IDENTITY} AND from_character_id IN ({','.join('?' * len(here))})""",
                           tuple(here)).fetchall()
        identity = {}
        for f, t in rel:
            if t in here:
                identity.setdefault(f, []).append(t)
        folded = {t for ts in identity.values() for t in ts}
        parts = []
        for c in cast:
            if c["who"] in folded:
                continue
            part = c["name"]
            if identity.get(c["who"]):
                part += " [" + "; ".join(here[t] for t in identity[c["who"]]) + "]"
            if c["notes"]:
                part += f" ({c['notes']})"
            parts.append(part)
        text = "; ".join(parts)
    if story["characters"]:
        text = f"{text}; {story['characters']}" if text else story["characters"]
    return text


_CREDIT_COLS = """sc.credit_type_id, sc.credit_name, sc.credited_as, sc.is_credited, sc.uncertain,
                  sc.is_sourced, cnd.name, cnd.is_official_name, cr.gcd_official_name AS official"""


def _issue(conn, iid: int) -> dict | None:
    i = conn.execute("""
        SELECT i.*, s.name AS s_name, s.year_began AS s_year, s.has_issue_title, s.is_singleton,
               ip.name AS ip_name
        FROM gcd_issue i JOIN gcd_series s ON s.id = i.series_id
        LEFT JOIN gcd_indicia_publisher ip ON ip.id = i.indicia_publisher_id
        WHERE i.id = ? AND +i.deleted = 0""", (iid,)).fetchone()
    if not i:
        return None
    brands = [r[0] for r in conn.execute("""
        SELECT b.name FROM gcd_issue_brand_emblem e JOIN gcd_brand b ON b.id = e.brand_id
        WHERE e.issue_id = ? ORDER BY b.name""", (iid,))]
    issue_credits = conn.execute(f"""
        SELECT {_CREDIT_COLS}, 0 AS is_signed, NULL AS signed_as FROM gcd_issue_credit sc
        JOIN gcd_creator_name_detail cnd ON cnd.id = sc.creator_id JOIN gcd_creator cr ON cr.id = cnd.creator_id
        WHERE sc.issue_id = ? AND +sc.deleted = 0 ORDER BY sc.id""", (iid,)).fetchall()
    stories = conn.execute("""
        SELECT st.*, t.name AS type_name FROM gcd_story st LEFT JOIN gcd_story_type t ON t.id = st.type_id
        WHERE st.issue_id = ? AND +st.deleted = 0 ORDER BY st.sequence_number""", (iid,)).fetchall()
    story_set = []
    for st in stories:
        credits = conn.execute(f"""
            SELECT {_CREDIT_COLS}, sc.is_signed, sc.signed_as FROM gcd_story_credit sc
            JOIN gcd_creator_name_detail cnd ON cnd.id = sc.creator_id JOIN gcd_creator cr ON cr.id = cnd.creator_id
            WHERE sc.story_id = ? AND +sc.deleted = 0 ORDER BY sc.id""", (st["id"],)).fetchall()
        row = {"type": st["type_name"], "title": st["title"], "feature": st["feature"],
               "sequence_number": st["sequence_number"], "page_count": _dec(st["page_count"])}
        for field, type_id in _CREDIT_TYPES:
            row[field] = "None" if st[f"no_{field}"] else _credit_field(credits, type_id, st[field])
        row.update(job_number=st["job_number"], genre=st["genre"], first_line=st["first_line"],
                   characters=_characters(conn, st), synopsis=st["synopsis"], notes=st["notes"], keywords="")
        story_set.append(row)
    return {
        "api_url": _url("issue", iid), "series_name": f"{i['s_name']} ({i['s_year']} series)",
        "descriptor": _descriptor(i), "number": i["number"], "volume": i["volume"],
        "variant_name": i["variant_name"], "title": i["title"], "publication_date": i["publication_date"],
        "key_date": i["key_date"], "price": i["price"], "page_count": _dec(i["page_count"]),
        "editing": _credit_field(issue_credits, 6, i["editing"]),
        "indicia_publisher": i["ip_name"], "brand_emblem": "; ".join(brands),
        "isbn": i["isbn"], "barcode": i["barcode"], "rating": i["rating"], "on_sale_date": i["on_sale_date"],
        "indicia_frequency": i["indicia_frequency"], "notes": i["notes"],
        "variant_of": _url("issue", i["variant_of_id"]), "series": _url("series", i["series_id"]),
        "indicia_printer": "", "keywords": "", "story_set": story_set, "cover": "",
    }
