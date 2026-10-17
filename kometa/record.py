"""The local catalogue record: what Kometa knows about each series and issue,
kept here, filled from the catalogues, read by everything.

Tonight's lesson (2026-10-09): creators lived in one cache, trades in another,
covers in a third, each filled on a different trigger, each empty until
something poked it, all at the mercy of Metron's thirty-a-minute and LOCG's
cookie. One record instead. Metron fills first (ids, dates, pages, ISBNs),
LOCG second (the runs Metron hasn't got — names, no ids), and every row says
where it came from and when. Filled by a background trickle for the backlog,
picked up for new issues as sync writes them, refreshed on interaction once
it's old. Nothing asks a catalogue at request time.
"""
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

import kometa.db as db

logger = logging.getLogger(__name__)
DB_PATH = db.DB_PATH
RECORD_TTL_DAYS = 90          # a full row is refreshed on interaction once this old
RECORD_MISS_TTL_DAYS = 7      # a row with nothing to ask is asked again after this
# Metron's volunteers add a new issue's credits days after it's out: a row asked on release
# day with none is asked again daily for its first CREDITLESS_DAYS (100 Bullets: The US of
# Anger #4 was asked on 2026-10-14 with no credits and would have kept none for 90 days)
CREDITLESS_RETRY_DAYS = 1
CREDITLESS_DAYS = 60
# what the file's own tags said: a stopgap, shown until a catalogue answers, never a
# reason not to ask one — and never thrown away for a miss
STOPGAP_SOURCES = ("comicinfo",)
# what a catalogue said — or our own ComicInfo carrying it back ('comicinfo-kometa',
# step 5a): catalogue-grade, kept fresh on the record TTL like any catalogue row
CATALOGUE_SOURCES = ("metron", "locg", "gcd", "comicinfo-kometa")
TRICKLE_LIMIT = 60            # per tick (10 min) — Metron's 3.5 s gap paces it; the harvest floor caps the day
TRICKLE_MINUTES = 10
# Series facts version. 2 = series_detail keeps genres, desc, associated, imprint and
# alt_names (2026-10-16); every Metron-linked row older than this is re-read, a few a tick.
FACTS_V = 2
FACTS_REFILL_PER_TICK = 4     # ~600 Metron-linked series ≈ a day at one tick per 10 min


_relabelled: dict = {}


@db.once_per_db
def ensure_tables(path=None):
    with db._connect(path or DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS issue_record (
            tracked_series_id INTEGER NOT NULL, number REAL NOT NULL,
            desc TEXT, credits_json TEXT, arcs_json TEXT, covers_json TEXT,
            store_date TEXT, cover_date TEXT, page_count INTEGER, price TEXT, isbn TEXT,
            metron_issue_id INTEGER, locg_issue_id TEXT, cv_issue_id TEXT,
            source TEXT, fetched_at TEXT, fill_state TEXT,
            PRIMARY KEY (tracked_series_id, number))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS series_record (
            tracked_series_id INTEGER PRIMARY KEY,
            desc TEXT, publisher TEXT, year_began INTEGER, year_end INTEGER, series_type TEXT, issue_count INTEGER,
            metron_series_id INTEGER, locg_series_id INTEGER, cv_volume_id TEXT,
            source TEXT, fetched_at TEXT, fill_state TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issue_record_state ON issue_record(fill_state, fetched_at)")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(issue_record)")]
        if "community_json" not in cols:
            # what LOCG's readers say (rating, pulls, have/read/want, histogram) — a
            # dated snapshot, refreshed on the re-enrich schedule; added 2026-10-12
            conn.execute("ALTER TABLE issue_record ADD COLUMN community_json TEXT")
            conn.execute("ALTER TABLE issue_record ADD COLUMN community_at TEXT")
        if "extra_json" not in cols:
            # everything else Metron says (ComicVine/GCD ids, barcode, cast, imprint…)
            # — what a clean ComicInfo.xml is made of; added 2026-10-12, additive only
            conn.execute("ALTER TABLE issue_record ADD COLUMN extra_json TEXT")
        if "variants_at" not in cols:
            # when the cover list was last asked for — separate from the details'
            # clock, since variants keep landing for weeks after release
            conn.execute("ALTER TABLE issue_record ADD COLUMN variants_at TEXT")
        if "field_sources_json" not in cols:
            # which catalogue gave a field when it isn't the row's own source, and which
            # rungs were asked for it (docs/credits-sourcing-design-2026-10-17.md); added 2026-10-17
            conn.execute("ALTER TABLE issue_record ADD COLUMN field_sources_json TEXT")
        if not _relabelled.get(path or DB_PATH):
            # once per process: covers-only placeholders written as 'partial' before
            # 2026-10-10 read as finished details and were never asked again
            conn.execute("""UPDATE issue_record SET fill_state = 'variants'
                WHERE fill_state = 'partial' AND variants_at IS NOT NULL AND COALESCE(desc, '') = ''
                  AND COALESCE(credits_json, '[]') IN ('', '[]')""")
            _relabelled[path or DB_PATH] = True
        scols = [r[1] for r in conn.execute("PRAGMA table_info(series_record)")]
        for col in ("genres_json", "tags_json"):
            if col not in scols:
                # the files' own <Genre> lines, read once. (Metron's series records carry
                # genres too — series_tags, below, holds both since 2026-10-16.)
                conn.execute(f"ALTER TABLE series_record ADD COLUMN {col} TEXT")
        for col, typ in (("extra_json", "TEXT"), ("facts_v", "INTEGER")):
            if col not in scols:
                conn.execute(f"ALTER TABLE series_record ADD COLUMN {col} {typ}")
        # Every genre label a series has, per source. 'comicinfo' = the <Genre> line the comic
        # files carried when they arrived (read once, kept in genres_json); 190 series had
        # one, and a refill through INSERT OR REPLACE would have erased the only copy (found
        # 2026-10-16). They live here now, and fill_series upserts.
        conn.execute("""CREATE TABLE IF NOT EXISTS series_tags (
            tracked_series_id INTEGER NOT NULL, source TEXT NOT NULL, key TEXT NOT NULL,
            label TEXT, kind TEXT, at TEXT,
            PRIMARY KEY (tracked_series_id, source, key))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_series_tags_key ON series_tags(key)")
        # Rows Komga harvested were its read of the files' ComicInfo — the same provenance
        # as 'comicinfo', and treated the same everywhere. Komga is gone; so is its label.
        conn.execute("UPDATE series_record SET source = 'comicinfo' WHERE source = 'komga'")
        conn.execute("UPDATE issue_record SET source = 'comicinfo' WHERE source = 'komga'")
        # Re-derived from genres_json on every start, so a change to the vocabulary
        # re-keys them; genres_json stays the files' own record.
        conn.execute("DELETE FROM series_tags WHERE source = 'comicinfo'")
        for sid, gj in conn.execute("""SELECT tracked_series_id, genres_json FROM series_record
                                       WHERE genres_json IS NOT NULL AND genres_json NOT IN ('', '[]')""").fetchall():
            try:
                labels = json.loads(gj)
            except (TypeError, ValueError):
                continue
            for label in labels if isinstance(labels, list) else []:
                for key, kind in genre_keys(str(label)):
                    conn.execute("INSERT OR IGNORE INTO series_tags VALUES (?, 'comicinfo', ?, ?, ?, ?)",
                                 (sid, key, str(label), kind, _now()))
        conn.execute("""CREATE TABLE IF NOT EXISTS trade_record (
            tracked_series_id INTEGER NOT NULL, key TEXT NOT NULL,
            title TEXT, vol INTEGER, vol_range_json TEXT, format TEXT, edition_title TEXT, subtitle TEXT,
            cover TEXT, store_date TEXT, page_count INTEGER, isbn TEXT, price TEXT, desc TEXT, collects_json TEXT,
            metron_series_id INTEGER, metron_issue_id INTEGER, locg_id TEXT,
            source TEXT, fetched_at TEXT,
            PRIMARY KEY (tracked_series_id, key))""")


# The genre vocabulary (agreed with the design partner, 2026-10-16, from all 47 labels
# the files carried). Keys are lowercase; '/' compounds split ("action/adventure" is
# action AND adventure). 'other' = a format, rating or medium: stored, never a reason
# two series are alike, never written to <Genre>. 'theme' = finer than Metron's coarse
# genres, so it survives beside them. Anything else is a coarse 'genre'.
_NOT_CONTENT = {"movies & tv", "movies and tv", "graphic novels", "graphic novel", "mature", "gore", "anthology",
                "risque", "young adult", "art books", "manga", "fiction", "literature", "adult", "teen",
                "all ages", "kids"}
_THEMES = {"supernatural", "occult", "leading ladies", "suspense", "post-apocalyptic", "zombies", "mythology",
           "thriller", "slice of life", "martial arts", "conspiracy", "political", "dystopia", "urban fantasy",
           "school life", "music", "animals", "japan"}
_GENRE_ALIASES = {"super hero": "superhero", "super-hero": "superhero", "superheroes": "superhero",
                  "post apocalyptic": "post-apocalyptic", "postapocalyptic": "post-apocalyptic",
                  "nonfiction": "non-fiction", "non fiction": "non-fiction", "sci-fi": "science fiction",
                  "sci fi": "science fiction", "scifi": "science fiction", "humour": "humor"}
FILE_THEME_TRUST = 0.6                           # a file's theme label, beside a catalogue's genres
GCD_TRUST = 0.8                                  # GCD's story genres: a catalogue, finer than Metron's


def genre_keys(label: str) -> list[tuple[str, str]]:
    """[(key, kind)] for one label from any source: Metron's "Super-Hero" and the
    files' "superhero" are both ('superhero', 'genre'); "action/adventure" is two."""
    out = []
    for part in (label or "").split("/"):
        k = re.sub(r"[\s_]+", " ", part.strip().lower()).strip()
        if not k:
            continue
        k = _GENRE_ALIASES.get(k, k)
        kind = "other" if k in _NOT_CONTENT else "theme" if k in _THEMES else "genre"
        if (k, kind) not in out:
            out.append((k, kind))
    return out


def genre_key(label: str) -> tuple[str, str]:
    """The first (key, kind) of a label; ('', '') for a blank one."""
    keys = genre_keys(label)
    return keys[0] if keys else ("", "")


def effective_tags(series_id: int, path=None) -> list[dict]:
    """The labels that describe a series, with a trust weight — what Related scores
    and what <Genre> is written from. Once Metron has answered, its coarse genres are
    the series' genres: the other sources' coarse labels are dropped, not merged.
    (Metron only says what a run IS, never what it isn't, so "keep the file labels
    Metron didn't mention" would keep Saga's 'superhero' and 'horror' — the
    partner's trap.) Themes stay: GCD's at GCD_TRUST, the files' at FILE_THEME_TRUST.
    With no Metron answer, GCD's and the files' genres stand in at those weights.
    'other' never appears."""
    return _effective(tags_for(series_id, path))


def effective_tags_all(path=None) -> dict[int, list[dict]]:
    """effective_tags for every series in one read — for Related's index."""
    path = path or DB_PATH
    ensure_tables(path)
    by: dict[int, list[dict]] = {}
    with db._connect(path) as conn:
        for r in conn.execute("""SELECT tracked_series_id, key, label, kind, source FROM series_tags
                ORDER BY tracked_series_id, CASE source WHEN 'metron' THEN 0 WHEN 'gcd' THEN 1 ELSE 2 END, key"""):
            by.setdefault(r["tracked_series_id"], []).append(
                {"key": r["key"], "label": r["label"], "kind": r["kind"], "source": r["source"]})
    return {sid: _effective(rows) for sid, rows in by.items()}


def _effective(rows: list[dict]) -> list[dict]:
    """The merge rule itself, over one series' rows (Metron first, then GCD, then files)."""
    has_metron = any(r["source"] == "metron" for r in rows)
    out, seen = [], set()
    for r in rows:
        if r["kind"] == "other" or r["key"] in seen:
            continue
        trust = GCD_TRUST if r["source"] == "gcd" else FILE_THEME_TRUST
        if r["source"] == "metron":
            w = 1.0
        elif r["kind"] == "theme" or not has_metron:
            w = trust                            # a theme, or no Metron answer yet: stands in at its source's trust
        else:
            continue                             # a coarse genre from elsewhere, superseded by Metron's
        seen.add(r["key"])
        out.append({**r, "weight": w})
    return out


def set_tags(series_id: int, source: str, labels: list[str], path=None) -> None:
    """Replace one source's labels for a series — that source's answer, whole."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        conn.execute("DELETE FROM series_tags WHERE tracked_series_id = ? AND source = ?", (series_id, source))
        for label in labels:
            for key, kind in genre_keys(label):
                conn.execute("INSERT OR IGNORE INTO series_tags VALUES (?, ?, ?, ?, ?, ?)",
                             (series_id, source, key, label, kind, _now()))


def tags_for(series_id: int, path=None) -> list[dict]:
    """Every label a series carries, Metron's first: [{key, label, kind, source}]."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        rows = conn.execute("""SELECT key, label, kind, source FROM series_tags WHERE tracked_series_id = ?
            ORDER BY CASE source WHEN 'metron' THEN 0 WHEN 'gcd' THEN 1 ELSE 2 END, key""", (series_id,)).fetchall()
    return [dict(r) for r in rows]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _age_days(fetched_at: str | None) -> float:
    if not fetched_at:
        return 1e9
    try:
        return (datetime.utcnow() - datetime.strptime(fetched_at[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400
    except ValueError:
        return 1e9


def _row(r) -> dict | None:
    if not r:
        return None
    d = dict(r)
    for k in ("extra", "community"):
        try:
            d[k] = json.loads(d.pop(f"{k}_json", None) or "null")
        except (TypeError, ValueError):
            d[k] = None
    try:
        d["field_sources"] = json.loads(d.pop("field_sources_json", None) or "{}") or {}
    except (TypeError, ValueError):
        d["field_sources"] = {}
    for k in ("credits", "arcs", "covers"):
        try:
            d[k] = json.loads(d.pop(f"{k}_json") or "[]")
        except (TypeError, ValueError):
            d[k] = []
    return d


def get_issue(series_id: int, number: float, path=None) -> dict | None:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        return _row(conn.execute("SELECT * FROM issue_record WHERE tracked_series_id = ? AND number = ?",
                                 (series_id, number)).fetchone())


def issues_for_series(series_id: int, path=None) -> list[dict]:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        return [_row(r) for r in conn.execute(
            "SELECT * FROM issue_record WHERE tracked_series_id = ? ORDER BY number", (series_id,))]


def get_series(series_id: int, path=None) -> dict | None:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT * FROM series_record WHERE tracked_series_id = ?", (series_id,)).fetchone()
        return dict(r) if r else None


# Keys other workers merge into extra (merge_extra) — carried across a Metron refresh.
HARVEST_EXTRA_KEYS = ("prh", "cv_original", "fandom", "marveldc")


def write_issue(series_id: int, number: float, data: dict, source: str | None, state: str, issue: dict | None = None,
                path=None) -> dict:
    """Store what a catalogue said about one issue. data uses the issue_meta /
    metron_client.issue_detail shape: desc, credits, covers, arcs, store_date,
    cover_date, page_count, price, isbn."""
    path = path or DB_PATH
    ensure_tables(path)
    issue = issue or {}
    with db._connect(path) as conn:
        # a variant list already fetched outlives a details write: REPLACE used to
        # wipe it and its stamp, and the LOCG sweep then fetched it all over again
        kept = conn.execute("SELECT covers_json, variants_at, extra_json, community_json, community_at, credits_json, "
                            "field_sources_json, source FROM issue_record WHERE tracked_series_id = ? AND number = ?",
                            (series_id, number)).fetchone()
        credits, sources = _credits_to_keep(kept, data.get("credits") or [], source)
        extra = data.get("extra")
        # a write that brings no extra (LOCG, a stopgap) never wipes what Metron said;
        # Metron's {} — asked, nothing beyond the basics — is stored as {}, not as
        # "never asked", or that issue would be asked again on every tick forever
        if extra is not None and kept and kept["extra_json"]:
            # what other workers wrote into extra (PRH's ask, ComicVine's original,
            # the Fandom gallery) isn't Metron's to drop on a refresh — losing it
            # meant asking those sources all over again
            try:
                prior = json.loads(kept["extra_json"]) or {}
            except (TypeError, ValueError):
                prior = {}
            extra = {**{k: prior[k] for k in HARVEST_EXTRA_KEYS if k in prior}, **extra}
        extra_json = json.dumps(extra) if extra is not None else (kept["extra_json"] if kept else None)
        cv_id = issue.get("cv_issue_id") or ((extra or {}).get("cv_id"))
        conn.execute("""INSERT OR REPLACE INTO issue_record
            (tracked_series_id, number, desc, credits_json, arcs_json, covers_json, store_date, cover_date, page_count,
             price, isbn, metron_issue_id, locg_issue_id, cv_issue_id, source, fetched_at, fill_state, extra_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (series_id, number, data.get("desc") or "", json.dumps(credits),
                      json.dumps(data.get("arcs") or []), json.dumps(data.get("covers") or []),
                      data.get("store_date") or issue.get("store_date"), data.get("cover_date"), data.get("page_count"),
                      data.get("price"), data.get("isbn"), issue.get("metron_issue_id"), issue.get("locg_issue_id"),
                      str(cv_id) if cv_id else None, source, _now(), state, extra_json))
        if sources:
            conn.execute("UPDATE issue_record SET field_sources_json = ? WHERE tracked_series_id = ? AND number = ?",
                         (json.dumps(sources), series_id, number))
        if kept and kept["variants_at"]:
            conn.execute("UPDATE issue_record SET covers_json = ?, variants_at = ? WHERE tracked_series_id = ? AND number = ?",
                         (kept["covers_json"], kept["variants_at"], series_id, number))
        if kept and kept["community_json"]:
            # readers' scores (LOCG, ComicBookRoundup, Open Library) aren't details:
            # a details refresh used to REPLACE them away and the sweep re-asked
            conn.execute("UPDATE issue_record SET community_json = ?, community_at = ? WHERE tracked_series_id = ? AND number = ?",
                         (kept["community_json"], kept["community_at"], series_id, number))
    return get_issue(series_id, number, path)


# --- credits: one catalogue's list, the best one that has any (2026-10-17) -------------------
# docs/credits-sourcing-design-2026-10-17.md (reviewed: credits-sourcing-review-2026-10-17.md).
# A list is never blended across catalogues; a higher rung's list replaces a lower one's,
# a lower rung never replaces a higher one, and an empty answer never clears a list.
CREDIT_RANK = {"metron": 5, "gcd": 4, "comicvine": 3, "locg": 2, "comicinfo": 1}
CREDIT_RUNG_RETRY_DAYS = 7      # a rung that had nothing is asked again after this


def credits_source(row: dict | None) -> str | None:
    """Who gave this row's credits: its field_sources entry, else the row's own source."""
    if not row:
        return None
    fs = row.get("field_sources") or {}
    return (fs.get("credits") or {}).get("source") or row.get("source")


def _credits_to_keep(kept, incoming: list, source: str | None) -> tuple[list, dict]:
    """(credits to store, field_sources to store) for a write over `kept` (a raw row or None)."""
    try:
        sources = json.loads((kept["field_sources_json"] if kept else None) or "{}") or {}
    except (TypeError, ValueError):
        sources = {}
    try:
        held = json.loads((kept["credits_json"] if kept else None) or "[]") or []
    except (TypeError, ValueError):
        held = []
    held_from = (sources.get("credits") or {}).get("source") or (kept["source"] if kept else None)
    if held and (not incoming or CREDIT_RANK.get(held_from, 0) > CREDIT_RANK.get(source, 0)):
        if held_from != (source or held_from) and "credits" not in sources:
            sources["credits"] = {"source": held_from, "at": _now()}
        return held, sources
    if incoming and sources.get("credits"):
        sources["credits"] = {"source": source, "at": _now()}
    return incoming, sources


def _cv_credits(people: list[dict]) -> list[dict]:
    """Comic Vine's person_credits ('artist, colorist, cover') → Metron-shaped credits."""
    names = {"writer": "Writer", "script": "Writer", "plotter": "Plot", "penciler": "Penciller", "penciller": "Penciller",
             "artist": "Artist", "inker": "Inker", "colorist": "Colorist", "letterer": "Letterer", "cover": "Cover"}
    out, seen = [], set()
    for p in people or []:
        for r in re.split(r"\s*,\s*", (p.get("role") or "").lower()):
            role = names.get(r.strip())
            key = (role, (p.get("name") or "").lower())
            if role and p.get("name") and key not in seen:
                seen.add(key)
                out.append({"role": role, "name": p["name"], "metron_creator_id": None})
    return out


def _ask_gcd(row: dict) -> list | None:
    """GCD's story credits for this issue by its gcd_id: [] asked and none, None couldn't ask."""
    from kometa import gcd_client
    gid = (row.get("extra") or {}).get("gcd_id")
    if not gid or not gcd_client.configured() or (gcd_client.budget() or {}).get("left", 0) <= 0:
        return None
    d = gcd_client.issue_detail(int(gid))
    if d is None:
        return []
    return [dict(c, metron_creator_id=None) for c in d.get("credits") or [] if c.get("role") not in ("Editor", None)]


def _ask_comicvine(row: dict) -> list | None:
    """Comic Vine's person_credits by cv_id: [] asked and none, None couldn't ask."""
    from kometa import sources
    cvid = (row.get("extra") or {}).get("cv_id") or row.get("cv_issue_id")
    cv = sources.comicvine()
    if not cvid or cv is None:
        return None
    d = cv._get(f"issue/4000-{int(cvid)}/", field_list="person_credits")
    if d.get("status_code") not in (None, 1):
        return None
    return _cv_credits((d.get("results") or {}).get("person_credits") or [])


CREDIT_RUNGS = (("gcd", _ask_gcd), ("comicvine", _ask_comicvine))


def fill_credits(series_id: int, number: float, path=None, rungs=None, dry_run: bool = False) -> dict:
    """Rungs 2–3 of the credits ladder for one issue whose list is empty or from a rung below.
    Each rung asked is remembered; one that had nothing waits CREDIT_RUNG_RETRY_DAYS.
    Returns {"source": rung or None, "credits": [...], "asked": [rungs]} — written unless dry_run."""
    path = path or DB_PATH
    row = get_issue(series_id, number, path)
    out = {"source": None, "credits": [], "asked": []}
    if not row:
        return out
    have = row.get("credits") or []
    rank = CREDIT_RANK.get(credits_source(row), 0) if have else 0
    tried = dict((row.get("field_sources") or {}).get("credits_tried") or {})
    for name, ask in (rungs or CREDIT_RUNGS):
        if CREDIT_RANK[name] <= rank:
            break
        if tried.get(name) and _age_days(tried[name]) < CREDIT_RUNG_RETRY_DAYS:
            continue
        try:
            got = ask(row)
        except Exception as e:
            logger.info(f"Credits from {name} for {series_id}#{number:g} skipped: {e}")
            continue
        if got is None:
            continue
        out["asked"].append(name)
        tried[name] = _now()
        if got:
            out.update(source=name, credits=got)
            break
    if dry_run:
        return out
    with db._connect(path) as conn:
        r = conn.execute("SELECT field_sources_json FROM issue_record WHERE tracked_series_id = ? AND number = ?",
                         (series_id, number)).fetchone()
        try:
            fs = json.loads((r["field_sources_json"] if r else None) or "{}") or {}
        except (TypeError, ValueError):
            fs = {}
        fs["credits_tried"] = tried
        if out["source"]:
            fs["credits"] = {"source": out["source"], "at": _now()}
            conn.execute("UPDATE issue_record SET credits_json = ?, field_sources_json = ? WHERE tracked_series_id = ? AND number = ?",
                         (json.dumps(out["credits"]), json.dumps(fs), series_id, number))
        else:
            conn.execute("UPDATE issue_record SET field_sources_json = ? WHERE tracked_series_id = ? AND number = ?",
                         (json.dumps(fs), series_id, number))
    if out["source"]:
        logger.info(f"Credits for {series_id}#{number:g} from {out['source']}: {len(out['credits'])}")
    return out


def credits_ladder_on(path=None) -> bool:
    """Owner's switch (config credits_ladder): off until the dry run has been looked at."""
    return db.get_config(path or DB_PATH).get("credits_ladder") == "on"


CREDITS_PER_TICK = 6


def credits_tick(path=None, limit: int = CREDITS_PER_TICK) -> int:
    """Scheduler share: a few issues up the ladder per tick. Returns how many got credits."""
    path = path or DB_PATH
    if not credits_ladder_on(path):
        return 0
    got = 0
    for r in credits_wanted(path, limit=limit):
        got += bool(fill_credits(r["tracked_series_id"], r["number"], path)["source"])
    return got


def credits_wanted(path=None, limit: int = 50) -> list[dict]:
    """Rows the ladder could still improve: no credits, or credits from below GCD, with a
    GCD or Comic Vine id to ask by. Pull-list series and owned issues first."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        rows = [_row(r) for r in conn.execute("""
            SELECT r.* FROM issue_record r JOIN tracked_series s ON s.id = r.tracked_series_id
            JOIN issue_status i ON i.tracked_series_id = r.tracked_series_id AND i.number = r.number
            WHERE (s.kind IS NULL OR s.kind != 'arc') AND (i.ignored IS NULL OR i.ignored = 0)
              AND (r.credits_json IS NULL OR r.credits_json = '[]' OR r.source IN ('locg', 'comicinfo'))
              AND (r.extra_json LIKE '%"gcd_id": %' OR r.extra_json LIKE '%"cv_id": %' OR r.cv_issue_id IS NOT NULL)
            ORDER BY s.on_pull_list DESC, i.owned DESC, i.store_date DESC""")]
    out = []
    for r in rows:
        tried = (r.get("field_sources") or {}).get("credits_tried") or {}
        e = r.get("extra") or {}
        ids = [n for n, k in (("gcd", "gcd_id"), ("comicvine", "cv_id")) if e.get(k) or (n == "comicvine" and r.get("cv_issue_id"))]
        if any(not tried.get(n) or _age_days(tried[n]) >= CREDIT_RUNG_RETRY_DAYS for n in ids):
            out.append(r)
        if len(out) >= limit:
            break
    return out


def _fresh(row: dict | None) -> bool:
    """True when the row answers for now: full/partial within TTL, or a miss
    younger than the miss TTL."""
    if not row:
        return False
    age = _age_days(row.get("fetched_at"))
    if row.get("source") in STOPGAP_SOURCES:
        return False                                            # shown, but a catalogue is still owed an ask
    if row.get("fill_state") == "miss":
        return age < RECORD_MISS_TTL_DAYS
    if row.get("fill_state") == "variants":
        return False                                            # covers only: the details are still owed
    if row.get("source") == "metron" and row.get("extra") is None:
        return False                                            # filled before 'extra' was kept (2026-10-12): ask once more
    if not row.get("credits") and _recently_out(row.get("store_date")) and age >= CREDITLESS_RETRY_DAYS:
        return False                                            # out lately, no credits yet: Metron may have them now
    return age < RECORD_TTL_DAYS


def _recently_out(store_date: str | None) -> bool:
    today = datetime.utcnow().date()
    try:
        d = datetime.strptime((store_date or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return today - timedelta(days=CREDITLESS_DAYS) <= d <= today


def merge_extra(series_id: int, number: float, fields: dict, path=None) -> None:
    """Add keys to an existing row's extra (what Metron said, plus what other
    sources add, e.g. ComicVine's original cover). A row with no extra yet stays
    'never asked' for Metron's purposes — this only writes when extra exists."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT extra_json FROM issue_record WHERE tracked_series_id = ? AND number = ?",
                         (series_id, number)).fetchone()
        if not r or r["extra_json"] is None:
            return
        extra = json.loads(r["extra_json"] or "{}")
        extra.update(fields)
        conn.execute("UPDATE issue_record SET extra_json = ? WHERE tracked_series_id = ? AND number = ?",
                     (json.dumps(extra), series_id, number))


# Other sources' scores ride in community_json under their own key, beside LOCG's
# top-level fields (enrichment 1i). LOCG's snapshot is rewritten whole on every
# refresh, so these are carried across it — and community_at stays LOCG's clock
# (the sweep reads it as "LOCG asked"); each nested snapshot carries its own 'at'.
COMMUNITY_SOURCES = ("cbr", "openlibrary", "hardcover")


def set_community(series_id: int, number: float, data: dict, path=None) -> None:
    """LOCG's community snapshot for one issue, stamped now. A row that doesn't
    exist yet is made as a covers-only placeholder ('variants' state), the same
    way the variant list makes one — the details are still owed. Other sources'
    nested snapshots (COMMUNITY_SOURCES) survive the rewrite."""
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT community_json FROM issue_record WHERE tracked_series_id = ? AND number = ?",
                         (series_id, number)).fetchone()
        merged = dict(data or {})
        if r and r["community_json"]:
            try:
                prior = json.loads(r["community_json"]) or {}
            except (TypeError, ValueError):
                prior = {}
            for k in COMMUNITY_SOURCES:
                if k in prior and k not in merged:
                    merged[k] = prior[k]
        if r:
            conn.execute("UPDATE issue_record SET community_json = ?, community_at = ? WHERE tracked_series_id = ? AND number = ?",
                         (json.dumps(merged), _now(), series_id, number))
        else:
            conn.execute("""INSERT INTO issue_record (tracked_series_id, number, covers_json, fill_state, community_json, community_at)
                VALUES (?, ?, '[]', 'variants', ?, ?)""", (series_id, number, json.dumps(merged), _now()))


def merge_community(series_id: int, number: float, source: str, snapshot: dict, path=None) -> None:
    """One other source's snapshot into community_json[source], leaving LOCG's
    fields and community_at alone. A row that doesn't exist yet is made as the
    same covers-only placeholder set_community makes, with no community_at —
    LOCG still owes its ask."""
    if source not in COMMUNITY_SOURCES:
        raise ValueError(f"not a community source: {source!r}")
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT community_json FROM issue_record WHERE tracked_series_id = ? AND number = ?",
                         (series_id, number)).fetchone()
        if r:
            try:
                cur = json.loads(r["community_json"] or "{}") or {}
            except (TypeError, ValueError):
                cur = {}
            cur[source] = snapshot
            conn.execute("UPDATE issue_record SET community_json = ? WHERE tracked_series_id = ? AND number = ?",
                         (json.dumps(cur), series_id, number))
        else:
            conn.execute("""INSERT INTO issue_record (tracked_series_id, number, covers_json, fill_state, community_json)
                VALUES (?, ?, '[]', 'variants', ?)""", (series_id, number, json.dumps({source: snapshot})))


def fill_issue(series_id: int, number: float, path=None, force: bool = False, issue: dict | None = None,
               metron=None, locg=None, locg_open=None) -> dict | None:
    """One issue into the record. Metron when the issue carries a Metron id;
    else LOCG, only while LOCG is open to us; else a 'miss' that waits a week.
    Returns the row, or None when the only source was shut (try again later).
    Raises metron_client.MetronUnavailable so a trickle stops at the first refusal."""
    path = path or DB_PATH
    existing = get_issue(series_id, number, path)
    gcd_row = bool(existing) and existing.get("source") == "gcd"
    # GCD and LOCG stand in for runs Metron hadn't got; once the issue carries a Metron
    # id, Metron's answer (creator ids, cast, universes) replaces theirs however fresh
    # they are. Without this a fresh stand-in was handed straight back, and the
    # trickle re-picked it every tick, counting each as done (2026-10-16).
    stand_in = bool(existing) and existing.get("source") in ("gcd", "locg")
    if existing and not force and _fresh(existing) and not stand_in:
        return existing
    if issue is None:
        issue = next((i for i in db.get_issues_for_series(series_id, path) if i["number"] == number), None)
        if issue is None:
            return None
    if stand_in and not force and _fresh(existing) and not issue.get("metron_issue_id"):
        return existing                                        # its answer stands until Metron has the issue
    from kometa import issue_meta
    if issue.get("metron_issue_id"):
        from kometa import metron_client
        found = (metron or issue_meta._metron)(issue, path)
        if found is None:
            raise metron_client.MetronUnavailable("Metron didn't answer")   # keep the row for next time
        return write_issue(series_id, number, found, "metron", "full", issue, path)
    if gcd_row:
        # GCD answered and Metron still can't: keep it. Neither LOCG's thinner
        # details nor a miss replaces it (harvest.gcd_fill_tick, 1g)
        _touch(series_id, number, path)
        return existing
    if issue.get("locg_issue_id"):
        from kometa import locg_client
        is_open = (locg_open if locg_open is not None else (lambda: not locg_client.locg_paused()))()
        if not is_open:
            from kometa import topup
            topup.enqueue("issue_details", f"{series_id}:{number:g}", path)
            return None                                        # not a miss: the door was shut, it's queued
        try:
            found = (locg or (lambda i, p: issue_meta._locg(i, p, False)))(issue, path)
        except Exception as e:
            logger.info(f"Record: LOCG details for issue {issue.get('locg_issue_id')} skipped: {e}")
            return None
        if found is None:
            return None
        return write_issue(series_id, number, found, "locg", "partial", issue, path)
    if existing and existing.get("source") in STOPGAP_SOURCES:
        _touch(series_id, number, path)                        # nothing to ask: keep the stopgap, ask again in a week
        return existing
    return write_issue(series_id, number, {}, None, "miss", issue, path)


def _touch(series_id: int, number: float, path):
    with db._connect(path) as conn:
        conn.execute("UPDATE issue_record SET fetched_at = ? WHERE tracked_series_id = ? AND number = ?",
                     (_now(), series_id, number))


def fill_series(series_id: int, path=None, force: bool = False, detail=None) -> dict | None:
    """The series' own facts: Metron's when linked, else what tracked_series holds."""
    path = path or DB_PATH
    ensure_tables(path)
    existing = get_series(series_id, path)
    if existing and not force and _age_days(existing.get("fetched_at")) < RECORD_TTL_DAYS:
        return existing
    s = db.get_series_by_id(series_id, path)
    if not s:
        return None
    facts = {"desc": None, "publisher": s.get("publisher"), "year_began": s.get("year_began"), "year_end": None,
             "series_type": s.get("metron_type"), "issue_count": None}
    source, state = ("locg" if s.get("locg_series_id") else None), ("partial" if s.get("locg_series_id") else "miss")
    if s.get("metron_series_id"):
        from kometa import metron_client
        d = (detail or metron_client.series_detail)(s["metron_series_id"])
        facts.update(publisher=d.get("publisher") or facts["publisher"], year_began=d.get("year") or facts["year_began"],
                     year_end=d.get("year_end"), series_type=d.get("type") or facts["series_type"],
                     issue_count=d.get("issue_count"), desc=d.get("desc"))
        extra = {k: d[k] for k in ("associated", "imprint", "alt_names") if d.get(k)}
        source, state = "metron", "full"
    else:
        d, extra = None, {}
    with db._connect(path) as conn:
        # An upsert that names its own columns. INSERT OR REPLACE deleted the row first,
        # so every column it didn't list (Komga's genres_json among them) came back NULL.
        conn.execute("""INSERT INTO series_record (tracked_series_id, desc, publisher, year_began, year_end,
            series_type, issue_count, metron_series_id, locg_series_id, cv_volume_id, source, fetched_at, fill_state,
            extra_json, facts_v)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tracked_series_id) DO UPDATE SET desc = excluded.desc, publisher = excluded.publisher,
              year_began = excluded.year_began, year_end = excluded.year_end, series_type = excluded.series_type,
              issue_count = excluded.issue_count, metron_series_id = excluded.metron_series_id,
              locg_series_id = excluded.locg_series_id, cv_volume_id = excluded.cv_volume_id, source = excluded.source,
              fetched_at = excluded.fetched_at, fill_state = excluded.fill_state, extra_json = excluded.extra_json,
              facts_v = excluded.facts_v""",
                     (series_id, facts["desc"], facts["publisher"], facts["year_began"], facts["year_end"],
                      facts["series_type"], facts["issue_count"], s.get("metron_series_id"), s.get("locg_series_id"),
                      s.get("cv_volume_id"), source, _now(), state, json.dumps(extra) if extra else None, FACTS_V))
    if d is not None:
        set_tags(series_id, "metron", d.get("genres") or [], path)      # Metron's answer, even when it's none
    return get_series(series_id, path)


# --- the trickle: oldest-unfilled first, pull-list series first ------------------------
def pending_issues(path=None, limit: int = 200) -> list[dict]:
    """Issues with no record, or a miss older than its TTL. Pull-list series lead."""
    path = path or DB_PATH
    ensure_tables(path)
    cutoff = (datetime.utcnow() - timedelta(days=RECORD_MISS_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT i.tracked_series_id, i.number, i.metron_issue_id, i.locg_issue_id, i.store_date, i.owned
            FROM issue_status i JOIN tracked_series s ON s.id = i.tracked_series_id
            LEFT JOIN issue_record r ON r.tracked_series_id = i.tracked_series_id AND r.number = i.number
            WHERE (s.kind IS NULL OR s.kind != 'arc')
              AND (r.tracked_series_id IS NULL
                   OR r.fill_state = 'variants'
                   OR (r.fill_state = 'miss' AND r.fetched_at < ?)
                   -- a stopgap row (file tags / Komga): ask now when there's a catalogue id to ask,
                   -- else look again in a week in case one has been matched since
                   OR (r.source = 'comicinfo'
                       AND (i.metron_issue_id IS NOT NULL OR i.locg_issue_id IS NOT NULL OR r.fetched_at < ?))
                   -- owned issues Metron filled before its extra fields were kept: once more each
                   OR (r.source = 'metron' AND r.extra_json IS NULL AND i.owned = 1 AND i.metron_issue_id IS NOT NULL)
                   -- GCD or LOCG stood in while Metron had no id; it has one now, so Metron refills it
                   -- (a series linked to Metron later — the sourcing deck — has LOCG rows with names, no ids)
                   OR (r.source IN ('gcd', 'locg') AND i.metron_issue_id IS NOT NULL)
                   -- out lately and no credits yet: Metron adds them days after release (CREDITLESS_DAYS)
                   OR (r.source = 'metron' AND (r.credits_json IS NULL OR r.credits_json = '[]')
                       AND i.store_date BETWEEN date('now', ?) AND date('now')
                       AND r.fetched_at < datetime('now', ?)))
            -- what you OWN first: 2,528 owned issues sat behind 1,956 unowned record-less ones (2026-10-12)
            ORDER BY s.on_pull_list DESC, i.owned DESC, (r.tracked_series_id IS NOT NULL), i.tracked_series_id, i.number
            LIMIT ?""", (cutoff, cutoff, f"-{CREDITLESS_DAYS} days", f"-{CREDITLESS_RETRY_DAYS} days", limit))]


def trickle(limit: int = TRICKLE_LIMIT, path=None) -> int:
    """Scheduler tick: fill up to `limit` issues, stop at Metron's first refusal.
    LOCG-only issues are skipped while LOCG is shut and don't count. Also fills
    a few series records so the series facts land alongside."""
    path = path or DB_PATH
    from kometa import metron_client
    done = 0
    for cand in pending_issues(path, limit=limit * 4):
        if done >= limit:
            break
        issue = dict(cand)
        try:
            row = fill_issue(cand["tracked_series_id"], cand["number"], path, issue=issue)
        except metron_client.MetronUnavailable as e:
            logger.info(f"Record trickle paused: {e}")
            break
        except Exception as e:
            logger.info(f"Record trickle skipped {cand['tracked_series_id']}#{cand['number']}: {e}")
            continue
        if row is not None and row.get("fill_state") != "miss":
            done += 1
    try:
        credits_tick(path)                                     # off until the owner's OK (credits_ladder)
    except Exception as e:
        logger.info(f"Credits ladder tick skipped: {e}")
    try:
        with db._connect(path) as conn:
            missing = [r[0] for r in conn.execute("""SELECT s.id FROM tracked_series s LEFT JOIN series_record r ON r.tracked_series_id = s.id
                WHERE (s.kind IS NULL OR s.kind != 'arc') AND r.tracked_series_id IS NULL ORDER BY s.on_pull_list DESC, s.id LIMIT 5""")]
        for sid in missing:
            fill_series(sid, path)
        # Metron-linked rows read with an older parser: re-read a few a tick, pulled
        # runs first. Owner decisions aren't touched — this only re-reads a link that exists.
        with db._connect(path) as conn:
            stale = [r[0] for r in conn.execute("""SELECT s.id FROM tracked_series s JOIN series_record r ON r.tracked_series_id = s.id
                WHERE s.metron_series_id IS NOT NULL AND (s.kind IS NULL OR s.kind != 'arc') AND COALESCE(r.facts_v, 0) < ?
                ORDER BY s.on_pull_list DESC, s.id LIMIT ?""", (FACTS_V, FACTS_REFILL_PER_TICK))]
        for sid in stale:
            fill_series(sid, path, force=True)
    except metron_client.MetronUnavailable:
        pass
    except Exception as e:
        logger.info(f"Record trickle: series facts skipped: {e}")
    return done


def backlog(path=None) -> dict:
    path = path or DB_PATH
    ensure_tables(path)
    with db._connect(path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM issue_status i JOIN tracked_series s ON s.id = i.tracked_series_id WHERE s.kind IS NULL OR s.kind != 'arc'").fetchone()[0]
        filled = conn.execute("SELECT COUNT(*) FROM issue_record WHERE fill_state != 'miss'").fetchone()[0]
        miss = conn.execute("SELECT COUNT(*) FROM issue_record WHERE fill_state = 'miss'").fetchone()[0]
    return {"issues": total, "filled": filled, "miss": miss, "pending": max(0, total - filled - miss)}


# --- the interaction path: the issue modal -------------------------------------------
def details(issue: dict, path=None, stored_only: bool = False) -> dict:
    """What the modal's Details tab shows, from the record. Absent → today's
    resolver answers AND fills the record; old → answer now, refresh behind.
    stored_only (a reader, not the admin): what's kept, and nothing more — a
    tester browsing must never spend Metron's or LOCG's budget."""
    path = path or DB_PATH
    if stored_only:
        row = get_issue(issue["tracked_series_id"], issue["number"], path) or {}
        return {"desc": row.get("desc") or "", "credits": row.get("credits") or [], "source": row.get("source"),
                "arcs": row.get("arcs") or [], "store_date": row.get("store_date") or issue.get("store_date"),
                "record": bool(row)}
    from kometa import localfirst
    if localfirst.enabled(path):
        return localfirst.details(issue, path)                   # 5a–5c: record → our ComicInfo → online, off by default
    sid, number = issue["tracked_series_id"], issue["number"]
    row = get_issue(sid, number, path)
    if row and row.get("fill_state") in ("full", "partial"):
        if not _fresh(row):                                   # old, or a stopgap: answer now, ask behind
            threading.Thread(target=lambda: _safe_refresh(sid, number, path), daemon=True).start()
        elif not row.get("credits") and credits_ladder_on(path):
            threading.Thread(target=lambda: _safe_credits(sid, number, path), daemon=True).start()
        return {"desc": row.get("desc") or "", "credits": row.get("credits") or [], "source": row.get("source"),
                "arcs": row.get("arcs") or [], "store_date": row.get("store_date"), "record": True}
    from kometa import issue_meta
    d = issue_meta.details_for(issue, path)
    if d.get("source"):
        state = "full" if d["source"].startswith("metron") else "partial"
        write_issue(sid, number, d, d["source"].split("+")[0], state, issue, path)
    return {"desc": d.get("desc", ""), "credits": d.get("credits", []), "source": d.get("source"),
            "arcs": d.get("arcs") or [], "store_date": d.get("store_date") or issue.get("store_date"), "record": False}


def _safe_refresh(sid: int, number: float, path):
    try:
        row = fill_issue(sid, number, path, force=True)
        if row is not None and not row.get("credits") and credits_ladder_on(path):
            fill_credits(sid, number, path)                 # Metron still has none: the next rungs
    except Exception as e:
        logger.info(f"Record refresh for {sid}#{number} skipped: {e}")


def _safe_credits(sid: int, number: float, path):
    try:
        fill_credits(sid, number, path)
    except Exception as e:
        logger.info(f"Credits ladder for {sid}#{number} skipped: {e}")


# --- variants: the cover list, typed, into the record -------------------------------
VARIANTS_TTL_DAYS = 30          # a settled issue's list is asked again after this
VARIANTS_FRESH_HOURS = 6        # an issue out in the last 60 days: re-asked this often on interaction
VARIANTS_RECENT_DAYS = 60
_RATIO_RE = re.compile(r"\b1\s*(?::|in|/)\s*(\d{1,3})\b|\bincentive\b|\bratio\b|\b(\d{1,3})\s*copy\b", re.I)
_TYPE_RULES = (
    ("virgin", re.compile(r"\bvirgin\b", re.I)),
    ("store exclusive", re.compile(r"\b(?:store|shop|retailer|exclusive|excl\.?)\b", re.I)),
    ("convention", re.compile(r"\b(?:convention|con\b|nycc|sdcc|eccc|c2e2|wondercon|thought bubble|mcm)\b", re.I)),
    ("sketch", re.compile(r"\b(?:sketch|b&w|black (?:and|&) white|line art|inks? only)\b", re.I)),
    ("reprint", re.compile(r"\b(?:\d+(?:st|nd|rd|th)\s+print(?:ing)?|reprint|second print|facsimile)\b", re.I)),
    ("foil", re.compile(r"\b(?:foil|metal|glow|lenticular|holo(?:graphic|foil)?)\b", re.I)),
)


def variant_type(name: str | None) -> str:
    """A conservative read of a variant's name: 'ratio' for 1:25 / incentive,
    then virgin, store exclusive, convention, sketch, reprint, foil; else 'cover'.
    No catalogue gives a typed enum (2026-10-10 research), so the name is all we have."""
    n = (name or "").strip()
    if not n:
        return "cover"
    if _RATIO_RE.search(n):
        return "ratio"
    for label, rx in _TYPE_RULES:
        if rx.search(n):
            return label
    return "cover"


def _typed(covers: list[dict], source: str) -> list[dict]:
    out, seen = [], set()
    for c in covers or []:
        cid = str(c.get("id") or "")
        key = cid or (c.get("name") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"id": cid or key, "name": c.get("name") or "Cover", "thumb": c.get("thumb"), "large": c.get("large") or c.get("thumb"),
                    "type": variant_type(c.get("name")), "source": c.get("source") or source, "local": None})
    return out


def _variants_fresh(row: dict | None) -> bool:
    if not row or not row.get("variants_at"):
        return False
    age_days = _age_days(row["variants_at"])
    sd = row.get("store_date") or ""
    recent = bool(sd) and _age_days(sd + " 00:00:00") < VARIANTS_RECENT_DAYS
    return age_days < (VARIANTS_FRESH_HOURS / 24 if recent else VARIANTS_TTL_DAYS)


def fill_variants(series_id: int, number: float, path=None, force: bool = False, issue: dict | None = None,
                  metron=None, locg=None, locg_open=None) -> list[dict] | None:
    """The issue's cover list into the record: Metron's variants first (its
    issue detail carries them), LOCG's list merged in by name while LOCG is open
    — queued for the top-up when it isn't. Returns the typed list, or None when
    nothing could be asked. Raises MetronUnavailable for a trickle to stop."""
    path = path or DB_PATH
    ensure_tables(path)
    row = get_issue(series_id, number, path)
    if row and not force and _variants_fresh(row):
        return row.get("covers") or []
    if issue is None:
        issue = next((i for i in db.get_issues_for_series(series_id, path) if i["number"] == number), None)
        if issue is None:
            return None
    from kometa import issue_meta, locg_client, metron_client
    covers: list[dict] = []
    asked = False
    if issue.get("metron_issue_id"):
        found = (metron or issue_meta._metron)(issue, path)
        if found is None:
            raise metron_client.MetronUnavailable("Metron didn't answer")
        covers = _typed(found.get("covers") or [], "metron")
        asked = True
    if issue.get("locg_issue_id"):
        is_open = (locg_open if locg_open is not None else (lambda: not locg_client.locg_paused()))()
        if not is_open:
            from kometa import topup
            topup.enqueue("variants", f"{series_id}:{number:g}", path)
        else:
            try:
                extra = (locg or (lambda lid: locg_client.fetch_variants(lid)["covers"]))(issue["locg_issue_id"])
                seen = {(c.get("name") or "").lower() for c in covers}
                main_id = str(issue["locg_issue_id"])
                for c in _typed(extra, "locg"):
                    if c["id"] == main_id and covers:
                        continue                                 # LOCG's 'Cover A (Main)' is Metron's main
                    if (c.get("name") or "").lower() in seen:
                        continue
                    covers.append(c)
                asked = True
            except Exception as e:
                logger.info(f"Variants: LOCG skipped for {series_id}#{number:g}: {e}")
    if not asked:
        return None
    with db._connect(path) as conn:
        if row:
            conn.execute("UPDATE issue_record SET covers_json = ?, variants_at = ? WHERE tracked_series_id = ? AND number = ?",
                         (json.dumps(covers), _now(), series_id, number))
        else:
            conn.execute("""INSERT INTO issue_record (tracked_series_id, number, desc, credits_json, arcs_json, covers_json,
                store_date, metron_issue_id, locg_issue_id, source, fetched_at, fill_state, variants_at)
                VALUES (?, ?, '', '[]', '[]', ?, ?, ?, ?, ?, ?, 'variants', ?)""",
                         (series_id, number, json.dumps(covers), issue.get("store_date"), issue.get("metron_issue_id"),
                          issue.get("locg_issue_id"), "metron" if issue.get("metron_issue_id") else "locg", _now(), _now()))
    return covers


def variants(issue: dict, path=None, stored_only: bool = False) -> dict:
    """The Variants tab: the record now, a refresh behind when it's old or the
    issue is new; today's live resolver only when the record holds nothing.
    stored_only (a reader): the record's covers or none — never a fetch."""
    path = path or DB_PATH
    sid, number = issue["tracked_series_id"], issue["number"]
    row = get_issue(sid, number, path)
    if stored_only:
        return {"covers": (row or {}).get("covers") or [], "source": (row or {}).get("source"), "record": bool(row)}
    if row and row.get("variants_at") and row.get("covers"):
        if not _variants_fresh(row):
            threading.Thread(target=lambda: _safe_variants(sid, number, path), daemon=True).start()
        return {"covers": row["covers"], "source": row.get("source"), "record": True}
    covers = None
    try:
        covers = fill_variants(sid, number, path, force=True, issue=issue)
    except Exception as e:
        logger.info(f"Variants: fill for {sid}#{number:g} failed: {e}")
    if covers is None:
        from kometa import issue_meta
        d = issue_meta.details_for(issue, path, want_covers=True)
        return {"covers": _typed(d.get("covers") or [], d.get("source") or "catalogue"), "source": d.get("source"), "record": False}
    return {"covers": covers, "source": "metron" if issue.get("metron_issue_id") else "locg", "record": False}


def _safe_variants(sid: int, number: float, path):
    try:
        fill_variants(sid, number, path, force=True)
    except Exception as e:
        logger.info(f"Variants refresh for {sid}#{number:g} skipped: {e}")


# --- trades: collected editions, Metron first, LOCG second --------------------------
# Metron files a run's collected editions as SIBLING series ('East of West TPB
# (2013)', type Trade Paperback) whose issues are the volumes. LOCG lists them
# under the run itself, by name only. Both land here as one list per series,
# written to trades_cache in the shape the Trades tab, the shelf page, tidy and
# refresh_trades_owned already read — so none of them change.
COLLECTED_TYPES = {"Trade Paperback": "TPB", "Hardcover": "HC", "Omnibus": "Omnibus", "Graphic Novel": "GN"}
TRADE_DETAIL_BUDGET = 20        # issue details fetched per fill, for subtitle / pages / ISBN
TRADES_TRICKLE_LIMIT = 5
_FORMAT_SUFFIX_RE = re.compile(r"\s+(?:tpb|tp|hc|omnibus|gn|ogn|compendium|deluxe(?: edition)?|library edition|"
                               r"absolute|hardcover|trade paperback|collected edition|book)$", re.I)


def metron_key(metron_issue_id: int) -> str:
    """The queue/tab key for a Metron-sourced trade. The Trades tab, the download
    endpoint and download_queue all key on a TEXT locg_id; LOCG's are digits,
    the pack and proposal sentinels are -1 / -2, so 'm<id>' can collide with none."""
    return f"m{int(metron_issue_id)}"


def _base_name(name: str) -> str:
    t = re.sub(r"\s*\(\d{4}\)\s*$", "", name or "").strip()
    prev = None
    while prev != t:
        prev, t = t, _FORMAT_SUFFIX_RE.sub("", t).strip(" :-")
    return t


def collected_siblings(series: dict, search=None) -> list[dict]:
    """Metron series that are this run's collected editions: same base name,
    a collected type, begun no earlier than the year before the run."""
    from kometa import metron_client
    from kometa.naming import norm_key
    want = norm_key(_base_name(series["title"]))
    year = series.get("year_began")
    raw = search or (lambda q: metron_client._get("series/", name=q).get("results", []))
    out, seen = [], set()
    for q in metron_client.title_variants(_base_name(series["title"])):
        for r in raw(q):
            st = r.get("series_type")
            fmt = COLLECTED_TYPES.get(st.get("name") if isinstance(st, dict) else st)
            if not fmt or r["id"] in seen:
                continue
            if norm_key(_base_name(r.get("series") or r.get("name") or "")) != want:
                continue
            if year and r.get("year_began") and r["year_began"] < year - 1:
                continue
            seen.add(r["id"])
            out.append({"id": r["id"], "name": r.get("series") or r.get("name"), "format": fmt,
                        "year": r.get("year_began"), "issue_count": r.get("issue_count")})
    return out


def _metron_trades(series: dict, search=None, issues=None, detail=None, budget: int = TRADE_DETAIL_BUDGET) -> list[dict]:
    from kometa import metron_client
    issues = issues or metron_client.series_issues
    detail = detail or (lambda iid: metron_client._get(f"issue/{int(iid)}/"))
    base = _base_name(series["title"])
    out, looked = [], 0
    for sib in collected_siblings(series, search):
        for i in issues(sib["id"]):
            n = i.get("number")
            vol = int(n) if n is not None and float(n) == int(n) else None
            t = {"format": sib["format"], "vol": vol, "vol_range": None, "is_variant": False,
                 "cover": i.get("image"), "store_date": i.get("store_date"),
                 "metron_series_id": sib["id"], "metron_issue_id": i.get("metron_issue_id"),
                 "locg_id": metron_key(i["metron_issue_id"]) if i.get("metron_issue_id") else None,
                 "subtitle": None, "page_count": None, "isbn": None, "price": None, "desc": None, "collects": [],
                 "source": "metron"}
            if i.get("metron_issue_id") and looked < budget:
                looked += 1
                try:
                    d = detail(i["metron_issue_id"])
                    t["subtitle"] = (d.get("title") or "").strip() or None
                    t["page_count"] = d.get("page")
                    t["isbn"] = d.get("isbn") or None
                    t["price"] = d.get("price")
                    t["desc"] = d.get("desc") or None
                    # what the edition reprints ('Fantastic Four (1998) #67'), not its story titles
                    from kometa.tradefill import parse_reprint
                    t["collects"] = [dict(p, metron_issue_id=rp.get("id")) for rp in (d.get("reprints") or [])
                                     if isinstance(rp, dict) and (p := parse_reprint(rp.get("issue")))]
                except metron_client.MetronUnavailable:
                    raise
                except Exception as e:
                    logger.info(f"Trade detail for {base!r} vol {vol} skipped: {e}")
            label = f"{base} Vol. {vol}" if vol is not None else base
            t["title"] = f"{label}: {t['subtitle']} {sib['format']}" if t["subtitle"] else f"{label} {sib['format']}"
            t["edition_title"] = t["title"]
            out.append(t)
    return out


def _merge_locg(trades: list[dict], locg: list[dict]) -> list[dict]:
    """An edition both catalogues know keeps the Metron row and gains the LOCG id;
    the rest of LOCG's list is appended as is."""
    by = {(t.get("vol"), t.get("format")): t for t in trades if t.get("vol") is not None}
    for l in locg:
        hit = by.get((l.get("vol"), l.get("format"))) if l.get("vol") is not None else None
        if hit:
            hit["locg_trade_id"] = l.get("locg_id")
            hit["cover"] = hit.get("cover") or l.get("cover")
        else:
            trades.append(dict(l, source="locg"))
    return trades


def fill_trades(series, force: bool = False, path=None, search=None, issues=None, detail=None,
                locg_fetch=None, locg_open=None, enrich=None) -> list[dict]:
    """The series' collected editions into trade_record AND trades_cache (enriched
    with owned/file, the two stored facts). Metron first; LOCG only when open.
    Raises MetronUnavailable so a trickle can stop; any LOCG trouble is logged."""
    path = path or DB_PATH
    ensure_tables(path)
    if isinstance(series, int):
        series = db.get_series_by_id(series, path)
    if not series:
        return []
    from kometa import locg_client
    trades: list[dict] = []
    metron_out: Exception | None = None
    locg_out: Exception | None = None
    locg_closed = False
    if series.get("metron_series_id"):
        try:
            trades = _metron_trades(series, search=search, issues=issues, detail=detail)
        except Exception as e:
            # Metron away is not the end of it: LOCG still gets its turn below.
            # The refusal is re-raised afterwards (so a trickle stops) only when
            # LOCG couldn't stand in — otherwise the list is LOCG's and Metron's
            # half comes on the next fill, since a partial fill is never cached as full.
            metron_out = e
    locg_id = series.get("locg_series_id")
    if locg_id:
        is_open = (locg_open if locg_open is not None else (lambda: not locg_client.locg_paused()))()
        if not is_open:
            locg_closed = True
            from kometa import topup
            topup.enqueue("trades", str(series["id"]), path)
        if is_open:
            try:
                # through sync's names, not locg_client's: that's the seam the LOCG
                # budget tests (and any caller counting LOCG traffic) hook
                from kometa import sync as _sync
                fetch = locg_fetch or (lambda lid: _sync.select_editions(_sync.get_trades_anon(lid)))
                trades = _merge_locg(trades, fetch(locg_id))
            except Exception as e:
                locg_out = e
                logger.info(f"Trades: LOCG skipped for {series.get('title')!r}: {e}")
    if metron_out is not None and not trades:
        raise metron_out
    stand_in = metron_out is not None          # LOCG's list only: Metron's half is still owed
    # A refetch that came back thinner than what's held — empty, or shorter while a
    # source was paused or refusing — keeps the cached list. 2026-10-15: a forced refresh
    # with LOCG's pass expired wrote [] over a 3-trade list and 1 over a 13-trade one.
    held = (db.get_trades(series["id"], path) or {}).get("trades") or []
    thinner = len(trades) < len(held) and (metron_out is not None or locg_out is not None or locg_closed)
    if held and (not trades or thinner):
        logger.info(f"Trades: kept the cached {len(held)} for {series.get('title')!r} — the refetch gave {len(trades)}")
        return held
    if not trades and not force:
        cached = db.get_trades(series["id"], path)
        if cached and cached["trades"]:
            return cached["trades"]
    from kometa.sync import enrich_trades
    if enrich:
        enrich(series, trades)
    else:
        enrich_trades(series, trades, landed=db.landed_trade_files(series["id"], path))
    with db._connect(path) as conn:
        for t in trades:
            key = metron_key(t["metron_issue_id"]) if t.get("metron_issue_id") else f"l:{t.get('locg_id')}"
            conn.execute("""INSERT OR REPLACE INTO trade_record (tracked_series_id, key, title, vol, vol_range_json, format,
                edition_title, subtitle, cover, store_date, page_count, isbn, price, desc, collects_json,
                metron_series_id, metron_issue_id, locg_id, source, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (series["id"], key, t.get("title"), t.get("vol"), json.dumps(t.get("vol_range")), t.get("format"),
                          t.get("edition_title"), t.get("subtitle"), t.get("cover"), t.get("store_date"), t.get("page_count"),
                          t.get("isbn"), t.get("price"), t.get("desc"), json.dumps(t.get("collects") or []),
                          t.get("metron_series_id"), t.get("metron_issue_id"),
                          str(t.get("locg_trade_id") or (t.get("locg_id") if t.get("source") == "locg" else "") or "") or None,
                          t.get("source") or "locg", _now()))
    db.set_trades(series["id"], trades, path)
    if stand_in:
        # not a full fill: pre-age the cache so _trades_due comes round tomorrow
        # instead of in TRADES_REFRESH_DAYS, and Metron's editions join the list
        with db._connect(path) as conn:
            conn.execute("UPDATE trades_cache SET fetched_at = datetime('now', ?) WHERE tracked_series_id = ?",
                         (f"-{max(_sync_refresh_days() - 1, 0)} days", series["id"]))
    return trades


def _sync_refresh_days() -> int:
    from kometa import sync as _sync
    return int(getattr(_sync, "TRADES_REFRESH_DAYS", 7))


def trade_details(key: str, path=None) -> dict | None:
    """The trade modal's Details for a Metron-sourced edition: description and
    what it collects, from the record. None for an LOCG key (the live scrape)."""
    path = path or DB_PATH
    if not str(key).startswith("m"):
        return None
    ensure_tables(path)
    with db._connect(path) as conn:
        r = conn.execute("SELECT * FROM trade_record WHERE key = ? LIMIT 1", (key,)).fetchone()
    if not r:
        return {"desc": "", "credits": [], "source": "metron"}
    collects = json.loads(r["collects_json"] or "[]")
    desc = r["desc"] or ""
    if collects:
        # reprinted issues ({series, number}) since 2026-10-11; story titles before that
        names = [f"{c['series']} #{c['number']:g}" if isinstance(c, dict) and c.get("series") else str(c) for c in collects]
        desc = (desc + "\n\n" if desc else "") + "Collects: " + "; ".join(names)
    bits = [b for b in (f"{r['page_count']} pages" if r["page_count"] else None, f"ISBN {r['isbn']}" if r["isbn"] else None,
                        f"Released {r['store_date']}" if r["store_date"] else None) if b]
    if bits:
        desc = (desc + "\n\n" if desc else "") + " · ".join(bits)
    return {"desc": desc, "credits": [], "source": "metron", "store_date": r["store_date"]}


def pending_trade_series(path=None, limit: int = 50) -> list[dict]:
    """Series with a catalogue id and no trades list yet, pull list first."""
    path = path or DB_PATH
    with db._connect(path) as conn:
        return [dict(r) for r in conn.execute("""
            SELECT s.* FROM tracked_series s LEFT JOIN trades_cache c ON c.tracked_series_id = s.id
            WHERE (s.kind IS NULL OR s.kind != 'arc') AND c.tracked_series_id IS NULL
              AND (s.metron_series_id IS NOT NULL OR s.locg_series_id IS NOT NULL)
            ORDER BY s.on_pull_list DESC, s.id LIMIT ?""", (limit,))]


def trades_trickle(limit: int = TRADES_TRICKLE_LIMIT, path=None) -> int:
    """Scheduler tick: a few series' trade lists, stopping at Metron's first refusal."""
    path = path or DB_PATH
    from kometa import metron_client
    done = 0
    for s in pending_trade_series(path, limit=limit * 3):
        if done >= limit:
            break
        try:
            fill_trades(s, path=path)
            done += 1
        except metron_client.MetronUnavailable as e:
            logger.info(f"Trades trickle paused: {e}")
            break
        except Exception as e:
            logger.info(f"Trades trickle skipped {s.get('title')!r}: {e}")
    return done
