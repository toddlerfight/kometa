"""The gaps Metron leaves on old issues: dates and descriptions (owner's call, 2026-10-18).

Two dates, never mixed:

- store_date — the day the issue went on sale. Only a source that knows that day gives
  one: Comic Vine's store_date, GCD's on_sale_date. Written to issue_status AND
  issue_record (every screen reads issue_status).
- cover_date (issue_status) — the month printed on the cover ('1994-08'), or just the
  year ('1994') when that's all anyone knows. Shown as 'Cover date August 1994', never
  as 'Released'; used for order and age where no on-sale date exists. From what's
  already held — the record's cover date (Metron, Comic Vine), the file's own tags,
  GCD's publication date — then Comic Vine and GCD as they're asked.

Old comics were dated a month or two ahead of the day they reached shops, so a cover
date is good for order and useless as a release date. Before this, GCD's cover-date
sort key stood in for its missing on-sale date, and a file tag of '2019' was stored as
1 January 2019; both are cover dates now.

Descriptions: Metron → Comic Vine → GCD synopsis (docs/consistent-comic-data-plan-2026-10-17.md).
Filled only where empty; nothing a catalogue gave is replaced.

Switch: config gap_fill on/off. dry_run() says what a full pass would do, writing nothing.
"""
import html
import json
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

from kometa import db
from kometa.db import DB_PATH

logger = logging.getLogger(__name__)

GAP_MINUTES = 10
CV_CALLS_PER_TICK = 3          # 100 issues a call; shares Comic Vine's hour with the ladder (cvname.spend)
GCD_PER_TICK = 6               # one call an issue; ~860 a day of GCD's 1,800 at most
RETRY_DAYS = 30                # a source that had nothing is asked again after this
DESC_MAX = 1500

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})


def on() -> bool:
    return db.get_config(DB_PATH).get("gap_fill") == "on"


def _on(path) -> bool:
    return db.get_config(path).get("gap_fill") == "on"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _age_days(ts: str | None) -> float:
    try:
        t = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 1e9
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400


# --- dates ----------------------------------------------------------------------------------

def cover_month(raw: str | None) -> str | None:
    """A cover date at the precision it really has: '1994-08-01' → '1994-08',
    '1994-08-00' → '1994-08', '1990-00-00' → '1990', '1994' → '1994'. None for nothing."""
    m = re.match(r"^\s*(\d{4})(?:-(\d{1,2}))?", str(raw or ""))
    if not m or m.group(1) == "0000":
        return None
    mm = int(m.group(2) or 0)
    return f"{m.group(1)}-{mm:02d}" if 1 <= mm <= 12 else m.group(1)


def from_publication(text: str | None) -> str | None:
    """GCD's printed date: 'September 1994' → '1994-09'; 'Fall 1994', '1990' → the year."""
    t = (text or "").lower()
    y = re.search(r"\b(1[89]\d\d|20\d\d)\b", t)
    if not y:
        return None
    for word in re.findall(r"[a-z]+", t):
        if word in _MONTHS:
            return f"{y.group(1)}-{_MONTHS[word]:02d}"
    return y.group(1)


def as_date(cover: str | None) -> str | None:
    """A cover date as a comparable ISO day: '1994-08' → '1994-08-01', '1994' → '1994-01-01'."""
    if not cover:
        return None
    return cover + ("-01" if len(cover) == 7 else "-01-01" if len(cover) == 4 else "")


def _held_cover(rec: dict) -> str | None:
    """The cover date from what the record already holds — no network."""
    got = cover_month(rec.get("cover_date"))
    if got:
        return got
    sd = rec.get("store_date") or ""
    if rec.get("source") == "comicinfo" and re.match(r"^\d{4}-\d{2}-\d{2}", sd):
        # the file's own Year/Month: '2019-01-01' is a bare year saved as a day
        return sd[:4] if sd[5:10] == "01-01" else sd[:7]
    try:
        extra = json.loads(rec.get("extra_json") or "null") or {}
    except (TypeError, ValueError):
        extra = {}
    return from_publication(extra.get("publication_date"))


STAMP_SPAN = 12          # one file-tag year on issues further apart than this is a pack's stamp


def _stamped(conn) -> set:
    """(series, year) where the files' bare Year tag is a pack's stamp, not a date: one year
    on issues more than STAMP_SPAN numbers apart. Uncanny X-Men Annual #4–#25 all said 1992."""
    span = {}
    for r in conn.execute("""SELECT tracked_series_id sid, number, store_date FROM issue_record
            WHERE source = 'comicinfo' AND store_date LIKE '____-01-01'"""):
        lo, hi = span.get((r["sid"], r["store_date"][:4]), (r["number"], r["number"]))
        span[(r["sid"], r["store_date"][:4])] = (min(lo, r["number"]), max(hi, r["number"]))
    return {k for k, (lo, hi) in span.items() if hi - lo > STAMP_SPAN}


def local_covers(path=None, dry: bool = False) -> int:
    """Copy the cover dates already held into issue_status.cover_date. → rows set."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        stamped = _stamped(conn)
        rows = [dict(r) for r in conn.execute("""
            SELECT i.id, i.tracked_series_id sid, i.cover_date status_cover, r.cover_date, r.store_date, r.source, r.extra_json
            FROM issue_status i JOIN issue_record r
              ON r.tracked_series_id = i.tracked_series_id AND r.number = i.number
            WHERE i.cover_date IS NULL OR (r.source = 'comicinfo' AND LENGTH(i.cover_date) = 4)""")]
        sets, clears = [], []
        for r in rows:
            c = _held_cover(r)
            if c and len(c) == 4 and r["source"] == "comicinfo" and (r["sid"], c) in stamped:
                c = None                             # a pack's stamp, not this issue's year
            if r["status_cover"] is None and c:
                sets.append((c, r["id"]))
            elif r["status_cover"] is not None and c is None and (r["sid"], r["status_cover"]) in stamped:
                clears.append((r["id"],))
        if not dry:
            if sets:
                conn.executemany("UPDATE issue_status SET cover_date = ? WHERE id = ? AND cover_date IS NULL", sets)
            if clears:
                conn.executemany("UPDATE issue_status SET cover_date = NULL WHERE id = ?", clears)
    return len(sets)


# --- descriptions ----------------------------------------------------------------------------

class _Prose(HTMLParser):
    """Comic Vine's description HTML → its paragraphs. Tables, figures and images are
    dropped, and everything from a 'List of covers' / 'Notes'-style heading on."""
    _SKIP = {"table", "figure", "script", "style", "img"}
    _STOP = re.compile(r"cover|creator|note|trivia|reprint|collected|credit|appearance", re.I)

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.cur, self.skip, self.head, self.stopped = [], [], 0, None, False

    def _flush(self):
        t = re.sub(r"\s+", " ", "".join(self.cur)).strip()
        if t:
            self.blocks.append(t)
        self.cur = []

    def handle_starttag(self, tag, attrs):
        if self.stopped:
            return
        if tag in self._SKIP:
            self.skip += tag != "img"
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            self.head = []
        elif tag in ("p", "div", "li", "br"):
            self._flush()

    def handle_endtag(self, tag):
        if self.stopped:
            return
        if tag in self._SKIP and tag != "img":
            self.skip = max(0, self.skip - 1)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self.head is not None:
            if self._STOP.search("".join(self.head)):
                self.stopped = True
            self.head = None
        elif tag in ("p", "div", "li"):
            self._flush()

    def handle_data(self, data):
        if self.stopped or self.skip:
            return
        if self.head is not None:
            self.head.append(data)
        else:
            self.cur.append(data)


def clean_description(raw_html: str | None, deck: str | None = None) -> str:
    """Comic Vine's description as plain paragraphs, cut to DESC_MAX at a sentence; the
    deck (its one-line summary) when the description has nothing left."""
    p = _Prose()
    try:
        p.feed(raw_html or "")
        p.close()
    except Exception:
        pass
    p._flush()
    text = "\n\n".join(b for b in p.blocks if len(b) > 1)
    if len(text) < 3:
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", deck or ""))).strip()
    if len(text) > DESC_MAX:
        cut = text[:DESC_MAX]
        end = max(cut.rfind(". "), cut.rfind(".\n"))
        text = cut[:end + 1] if end > DESC_MAX // 2 else cut.rstrip() + "…"
    return text


# --- the fills ------------------------------------------------------------------------------

def _rows(conn, where: str, args=()) -> list[dict]:
    return [dict(r) for r in conn.execute(f"""
        SELECT i.tracked_series_id sid, i.number, i.owned, i.store_date status_date, i.cover_date status_cover,
               r.desc, r.store_date, r.cover_date, r.source, r.cv_issue_id, r.extra_json, r.field_sources_json
        FROM issue_status i JOIN issue_record r ON r.tracked_series_id = i.tracked_series_id AND r.number = i.number
        JOIN tracked_series s ON s.id = i.tracked_series_id
        WHERE (s.kind IS NULL OR s.kind != 'arc') AND (i.ignored IS NULL OR i.ignored = 0) AND ({where})
        ORDER BY s.on_pull_list DESC, i.owned DESC, i.tracked_series_id, i.number""", args)]


def _ids(row: dict) -> tuple[int | None, int | None]:
    try:
        e = json.loads(row.get("extra_json") or "null") or {}
    except (TypeError, ValueError):
        e = {}
    cv = e.get("cv_id") or row.get("cv_issue_id")
    try:
        cv = int(cv) if cv else None
    except (TypeError, ValueError):
        cv = None
    return cv, e.get("gcd_id")


def _fs(row: dict) -> dict:
    try:
        return json.loads(row.get("field_sources_json") or "{}") or {}
    except (TypeError, ValueError):
        return {}


def _needs(row: dict) -> tuple[bool, bool]:
    """(needs an on-sale date, needs a description)."""
    return (not row.get("status_date"), not (row.get("desc") or "").strip())


def _tried(row: dict, source: str) -> bool:
    return _age_days((_fs(row).get("gap_tried") or {}).get(source)) < RETRY_DAYS


def _suspect_gcd_date(row: dict) -> bool:
    """A GCD record's date from before 2026-10-18: it may be GCD's cover-date key
    standing in for an on-sale date nobody recorded. Asked once more."""
    fs = _fs(row)
    return row.get("source") == "gcd" and bool(row.get("store_date")) and "store_date" not in fs \
        and not _tried(row, "gcd")


def cv_wanted(path=None, limit: int = 100) -> list[dict]:
    path = path or DB_PATH
    with db._connect(path) as conn:
        rows = _rows(conn, "i.store_date IS NULL OR r.desc IS NULL OR r.desc = ''")
    return [r for r in rows if _ids(r)[0] and any(_needs(r)) and not _tried(r, "comicvine")][:limit]


def gcd_wanted(path=None, limit: int = GCD_PER_TICK) -> list[dict]:
    """Rows Comic Vine couldn't finish (asked already, or no Comic Vine id) that GCD can try,
    and GCD records whose date may be its cover-date key."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        rows = _rows(conn, "i.store_date IS NULL OR r.desc IS NULL OR r.desc = '' OR r.source = 'gcd'")
    out = []
    for r in rows:
        cv, gcd = _ids(r)
        if not gcd:
            continue
        if _suspect_gcd_date(r) or (any(_needs(r)) and (not cv or _tried(r, "comicvine")) and not _tried(r, "gcd")):
            out.append(r)
        if len(out) >= limit:
            break
    return out


def _write(conn, row: dict, source: str, store_date: str | None = None, desc: str | None = None,
           cover: str | None = None, clear_date: bool = False) -> dict:
    """Fill what's empty on one issue and stamp the ask. → what changed."""
    sid, n = row["sid"], row["number"]
    fs = _fs(row)
    fs.setdefault("gap_tried", {})[source] = _now()
    changed = {}
    if clear_date:
        # GCD's cover-date key had stood in for an on-sale date: it moves to cover_date
        conn.execute("UPDATE issue_record SET store_date = NULL WHERE tracked_series_id = ? AND number = ?", (sid, n))
        conn.execute("UPDATE issue_status SET store_date = NULL WHERE tracked_series_id = ? AND number = ? AND store_date = ?",
                     (sid, n, row["store_date"]))
        changed["date_cleared"] = row["store_date"]
        row = {**row, "status_date": None if row.get("status_date") == row["store_date"] else row.get("status_date")}
    if store_date and not row.get("status_date"):
        conn.execute("UPDATE issue_status SET store_date = ? WHERE tracked_series_id = ? AND number = ? AND store_date IS NULL",
                     (store_date, sid, n))
        conn.execute("UPDATE issue_record SET store_date = ? WHERE tracked_series_id = ? AND number = ?", (store_date, sid, n))
        fs["store_date"] = {"source": source, "at": _now()}
        changed["store_date"] = store_date
    if desc and not (row.get("desc") or "").strip():
        conn.execute("UPDATE issue_record SET desc = ? WHERE tracked_series_id = ? AND number = ?", (desc, sid, n))
        fs["desc"] = {"source": source, "at": _now()}
        changed["desc"] = len(desc)
    if cover and not row.get("status_cover"):
        conn.execute("UPDATE issue_status SET cover_date = ? WHERE tracked_series_id = ? AND number = ? AND cover_date IS NULL",
                     (cover, sid, n))
        changed["cover_date"] = cover
    conn.execute("UPDATE issue_record SET field_sources_json = ? WHERE tracked_series_id = ? AND number = ?",
                 (json.dumps(fs), sid, n))
    return changed


def _retag(changed: list[tuple[int, float]], path) -> None:
    """New dates and descriptions reach the files' ComicInfo now, as the credits ladder's do."""
    try:
        from kometa import retag
        for sid, n in changed:
            retag.requeue_issue_now(sid, n, path)
    except Exception as e:
        logger.info(f"Gap fill: retag requeue skipped: {e}")


def _cv_client(path):
    from kometa.comicvine_client import ComicVineClient
    cfg = db.get_config(path)
    key = (cfg.get("cv_api_key") or "").strip()
    if not key or cfg.get("comicvine_enabled", "1") == "0":
        return None
    return ComicVineClient(key)


def cv_fill(path=None, calls: int = CV_CALLS_PER_TICK, dry: bool = False, client=None) -> dict:
    """Comic Vine, 100 issues a call: on-sale dates, descriptions, cover dates. → counts."""
    path = path or DB_PATH
    from kometa import cvname
    cv = client or _cv_client(path)
    out = {"asked": 0, "store_date": 0, "desc": 0, "cover_date": 0}
    if not cv:
        return out
    touched = []
    for _ in range(calls):
        batch = cv_wanted(path, limit=100)
        if not batch or (client is None and not cvname.spend()):
            break
        ids = {}
        for r in batch:
            ids.setdefault(_ids(r)[0], []).append(r)
        d = cv._get("issues/", filter="id:" + "|".join(str(i) for i in ids),
                    field_list="id,store_date,cover_date,deck,description", limit=100)
        if d.get("status_code") not in (None, 1):
            logger.info(f"Gap fill: Comic Vine said {d.get('error')}")
            break
        got = {int(x["id"]): x for x in d.get("results") or [] if x.get("id")}
        out["asked"] += len(batch)
        with db._connect(path) as conn:
            for cvid, rows in ids.items():
                x = got.get(cvid) or {}
                for r in rows:
                    sd = (x.get("store_date") or "")[:10] or None
                    desc = clean_description(x.get("description"), x.get("deck")) or None
                    cover = cover_month(x.get("cover_date"))
                    if dry:
                        out["store_date"] += bool(sd and not r["status_date"])
                        out["desc"] += bool(desc and not (r["desc"] or "").strip())
                        out["cover_date"] += bool(cover and not r["status_cover"])
                        continue
                    ch = _write(conn, r, "comicvine", store_date=sd, desc=desc, cover=cover)
                    for k in ("store_date", "desc", "cover_date"):
                        out[k] += k in ch
                    if r["owned"] and ("store_date" in ch or "desc" in ch):
                        touched.append((r["sid"], r["number"]))
        if dry:
            break                   # nothing was stamped: the next call would ask the same 100
    if touched:
        _retag(touched, path)
    return out


def gcd_fill(path=None, limit: int = GCD_PER_TICK, dry: bool = False, fetch=None) -> dict:
    """GCD, one issue a call: its on-sale date (never its cover-date key), its synopsis,
    its cover month. → counts."""
    path = path or DB_PATH
    from kometa import gcd_client
    if fetch is None:
        if not gcd_client.configured():
            return {"asked": 0}
        fetch = lambda g: gcd_client._get(f"issue/{int(g)}/")
    out = {"asked": 0, "store_date": 0, "desc": 0, "cover_date": 0, "date_cleared": 0}
    touched = []
    for r in gcd_wanted(path, limit=limit):
        try:
            raw = fetch(_ids(r)[1])
        except gcd_client.GCDUnavailable as e:
            logger.info(f"Gap fill: {e}")
            break
        out["asked"] += 1
        raw = raw or {}
        sd = gcd_client._date(raw.get("on_sale_date")) if re.match(r"^\d{4}-\d{2}-\d{2}", raw.get("on_sale_date") or "") \
            and not (raw.get("on_sale_date") or "").endswith("-00") else None
        stories = [s for s in raw.get("story_set") or [] if (s.get("type") or "").lower() != "cover"]
        desc = "\n\n".join(s["synopsis"].strip() for s in stories if (s.get("synopsis") or "").strip()) or None
        cover = cover_month(raw.get("key_date")) or from_publication(raw.get("publication_date"))
        # only a date that IS GCD's cover key: a record also copies the issue's own date
        # (Hellboy #3, 4 February 2004 from the release list) and that one stays
        clear = (_suspect_gcd_date(r) and r["store_date"] != sd
                 and r["store_date"] == gcd_client._date(raw.get("key_date")))
        if dry:
            out["store_date"] += bool(sd and (not r["status_date"] or clear))
            out["desc"] += bool(desc and not (r["desc"] or "").strip())
            out["cover_date"] += bool(cover and not r["status_cover"])
            out["date_cleared"] += bool(clear)
            continue
        with db._connect(path) as conn:
            ch = _write(conn, r, "gcd", store_date=sd, desc=desc, cover=cover, clear_date=clear)
        for k in ("store_date", "desc", "cover_date", "date_cleared"):
            out[k] += k in ch
        if r["owned"] and ch:
            touched.append((r["sid"], r["number"]))
    if touched:
        _retag(touched, path)
    return out


def tick(path=None) -> dict:
    """Scheduler share: held cover dates, then Comic Vine, then GCD. Off unless gap_fill is on."""
    path = path or DB_PATH
    if not _on(path):
        return {}
    got = {"cover_local": local_covers(path)}
    try:
        got["comicvine"] = cv_fill(path)
    except Exception as e:
        logger.info(f"Gap fill: Comic Vine skipped: {e}")
    try:
        got["gcd"] = gcd_fill(path)
    except Exception as e:
        logger.info(f"Gap fill: GCD skipped: {e}")
    if got["cover_local"] or any((got.get(s) or {}).get(k) for s in ("comicvine", "gcd")
                                 for k in ("store_date", "desc", "cover_date", "date_cleared")):
        logger.info(f"Gap fill: {got}")
    return got


def dry_run(path=None, gcd_sample: int = 12) -> dict:
    """What a full pass would do, writing nothing: everything held locally, one Comic Vine
    call (100 issues), a small GCD sample. Plus how many are waiting for each."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        no_date = conn.execute("""SELECT COUNT(*) FROM issue_status i JOIN tracked_series s ON s.id = i.tracked_series_id
            WHERE (s.kind IS NULL OR s.kind != 'arc') AND i.store_date IS NULL""").fetchone()[0]
    return {
        "issues_without_on_sale_date": no_date,
        "cover_dates_from_what_we_hold": local_covers(path, dry=True),
        "comicvine_waiting": len(cv_wanted(path, limit=100000)),
        "comicvine_one_call": cv_fill(path, calls=1, dry=True),
        "gcd_waiting": len(gcd_wanted(path, limit=100000)),
        "gcd_sample": gcd_fill(path, limit=gcd_sample, dry=True),
    }
