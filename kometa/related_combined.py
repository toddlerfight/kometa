"""Related, phase 1 of the redesign (docs/recommendations-design-round1.md, round3.md).

Every pair of series is scored across several channels at once — the credits they
share, story arcs, reading lists, Metron's companion series, universe, cast, genre,
franchise — and each channel SATURATES: cap · (1 − e^(−raw/τ)). Stacking more of the
same stops paying, so one prolific writer can't run away with a row, and three thin
genre overlaps never outweigh a writer.

Rules that came out of the design rounds and the baseline harness run:
- a card needs a channel that can create one: a weak shared credit (inker, colorist,
  letterer) adds to a score but never puts a card on the row by itself (208 such
  cards in the baseline); cast and genre need a real contribution first;
- creators count over the issues you OWN, where there are any: over Batman (1940)'s
  693 credited issues the 20% rule kept four 1940s names and none of yours;
- no more than 3 of the top 6, or 5 of 12, share a primary reason;
- a series' own family (annuals, specials) is never its recommendation;
- a feature on too many series says nothing (superhero), and the default
  continuities (Earth 616, Prime Earth) aren't a universe worth naming.

All local, read from an index built once and kept warm; nothing asks a catalogue.
It runs behind config `related_engine` = 'combined' until the owner switches it on.

Phase 2 (2026-10-17, the design partner's build, reviewed on live data) adds two
channels, each behind its own switch:
- config `related_themes` = 'on': themes read out of the issue descriptions (vampires,
  heists, time travel…) join the genre channel, below a file's or GCD's own label;
- config `related_community` = 'boost': "liked by the same readers" (CBR's reviewers,
  centred per person) lifts a card another channel earned; = 'cards' lets it make a
  card on its own. Those reviewers co-read by era — Saga's strongest neighbours are
  Hawkeye and Batman (2011), Monstress's include Spider-Man/Deadpool — so 'cards'
  waits for the owner's blind scoring.
And an anthology's one-off contributors stop being a creator link: The Joker 80th
Anniversary Special put Fiona Staples' pin-up in Saga's top five.
"""
import json
import logging
import math
import re
import threading
import time
from collections import Counter, defaultdict

import kometa.db as db
from kometa.naming import norm_key

DB_PATH = db.DB_PATH

# channel: (cap, τ). Agreed in round 1, kept in round 3.
CHANNELS = {"creators": (6.0, 3.0), "arcs": (4.0, 3.0), "lists": (3.5, 2.0), "universe": (2.5, 1.5),
            "cast": (2.0, 1.5), "genre": (2.0, 0.5)}
ASSOCIATED_FLAT = 3.0               # Metron lists them together: worth a card on its own
FRANCHISE_FLAT = 1.5
CARD_MIN = {"cast": 0.6, "genre": 0.6}       # these channels need this much before they can make a card
STRONG_CREDIT = 2.0                 # writer 3, plot/artist/penciller 2; an inker (1) can't make a card alone
PRESENCE = 0.20                     # a creator on this share of the issues considered
LONG_RUN, LONG_RUN_FLOOR = 50, 25   # a run you own none of, over 50 issues: 25 issues (two years) is a defining creator
CAST_PRESENCE = 0.25                # a character/team/universe on this share of a run's issues
CAST_TOP_CHARACTERS, CAST_TOP_TEAMS = 5, 2
STOP_SHARE = 0.15                   # a feature on more of a channel's series than this says nothing
# Genres are coarse and few series carry them: horror and science fiction sit near
# 20% of the tagged shelf and still mean something (Saga is science fiction to
# Metron and nothing else); superhero, at 44%, doesn't.
STOP_SHARE_BY = {"genre": 0.30}
# Universes are judged by name, never by share: Absolute Universe is on 12 of the 46
# series that have one (26%) and is the best link those 12 have (design round 2).
DEFAULT_UNIVERSES = {"earth 616", "earth-616", "prime earth", "earth 0", "earth-0", "new earth"}
THEME_MULT = 1.5                    # a theme says more than a coarse genre
REASON_SHARE = 0.25                 # a channel's reason shows if it gave this share of the card's score
DIVERSITY_TOP, DIVERSITY_TOP_MAX, DIVERSITY_ALL_MAX = 6, 3, 5
ROW = 12                            # the row's length
HUB_TOP = 5                         # hubness is counted over the top 5 rows people see first — what the harness gate measures
HUB_DAMP = 0.5                      # score / (1 + 0.5 · log2(rows it's in / the median)) above the median
HUB_GATE, HUB_PASSES = 0.03, 6      # re-damp until no series is in more than 3% of top-5 rows
INDEX_TTL_S = 300
# ...but a series the index has nothing on gets a look sooner: Absolute Evil, tracked at
# 06:21 on 2026-10-17 a minute after a restart built the index, had its credits by
# 06:23 and an empty row until 06:26 — the page had given up by then
FRESH_RECHECK_S = 20
# A short run with more than 8 writers and artists per issue is an anthology: a one-off
# contributor isn't a reason to recommend it (The Joker 80th Anniversary Special: 25 of
# them in one issue, in 23 top-five rows; Fiona Staples' pin-up put it in Saga's top
# five). Its own row still walks them all — Tom King's Batman is a fair pick from it.
ANTHOLOGY_RUN, ANTHOLOGY_PEOPLE = 4, 8

# --- phase 2: description themes ------------------------------------------------------
DESC_THEME_TRUST = 0.5              # read out of a blurb: below a file's theme (0.6) or GCD's (0.8)
# The design partner's 27 patterns. Keys that the tag vocabulary already has use its
# name (record._THEMES), so a file's "occult" and a blurb's witchcraft are one theme.
THEME_PATTERNS = {
    "vampires": r"\b(?:vampire\w*|nosferatu|dracula|bloodsucker\w*|undead\s+covens?)\b",
    "werewolves": r"\b(?:werewol(?:f|ves)|lycanthrop\w*|wolf-?man)\b",
    "zombies": r"\b(?:zombie\w*|walking\s+dead|living\s+dead|undead\s+horde\w*|necromanc\w*)\b",
    "ghosts": r"\b(?:haunted|haunting|poltergeist\w*|apparition\w*|specter\w*|spectre\w*|ectoplasm\w*|restless\s+spirit\w*)\b",
    "demons": r"\b(?:demonic|hellspawn|underworld|satan\w*|hellfire|infernal|damnation|depths\s+of\s+hell|forces\s+of\s+hell)\b",
    "occult": r"\b(?:witch(?:es|craft)?|coven\w*|occult\w*|ritual\s+magic|dark\s+magic|grimoire\w*|sorcer(?:y|er|ess)\w*)\b",
    "monsters": r"\b(?:kaiju|giant\s+monster\w*|behemoth\w*|leviathan\w*|lovecraft\w*|eldritch|cosmic\s+horror)\b",
    "aliens": r"\b(?:xenomorph\w*|extraterrestrial\w*|alien\s+(?:invasion|species|world\w*|planet\w*|threat|organism|lifeform\w*)|first\s+contact)\b",
    "space opera": r"\b(?:starship\w*|spaceship\w*|interstellar|galactic\s+empire\w*|deep\s+space|space\s+station\w*|hyperspace|federation|rebel\s+alliance|bounty\s+hunter\w*)\b",
    "time travel": r"\b(?:time\s+travel\w*|time\s+loop\w*|time\s+machine\w*|timeline\w*|temporal\s+(?:paradox|anomaly|rift)|chrononaut\w*|future\s+self)\b",
    "multiverse": r"\b(?:multivers\w*|parallel\s+(?:world\w*|universe\w*|earth\w*)|alternate\s+(?:reality|realities|universe\w*|timeline\w*|dimension\w*)|interdimensional)\b",
    "post-apocalyptic": r"\b(?:post-?apocalyp\w*|wasteland\w*|doomsday|nuclear\s+winter|after\s+the\s+end|collapsed\s+civilization|dying\s+earth)\b",
    "cyberpunk": r"\b(?:cyberpunk|cyborg\w*|android\w*|artificial\s+intelligence|sentient\s+(?:robot\w*|ai|machine\w*)|virtual\s+reality|megacorp\w*)\b",
    "heists": r"\b(?:heist\w*|bank\s+robber\w*|getaway\s+driver|con\s+artist\w*|crime\s+syndicate\w*|mob\s+boss\w*|cartel\w*|organized\s+crime)\b",
    "serial killers": r"\b(?:serial\s+killer\w*|copycat\s+killer\w*|grisly\s+murder\w*|ritual\s+murder\w*|homicide\s+detective\w*|cold\s+case\w*)\b",
    "noir": r"\b(?:private\s+(?:eye|investigator\w*|detective\w*)|hard-?boiled|femme\s+fatale|gumshoe|(?:crime|detective|hard-?boiled|neo-?)\s*noir)\b",
    "espionage": r"\b(?:espionage|secret\s+agent\w*|double\s+agent\w*|covert\s+op\w*|black\s+ops|intelligence\s+agency|spy\s+thriller)\b",
    "conspiracy": r"\b(?:conspirac\w*|cover-?up|secret\s+society|shadowy\s+cabal|shadowy\s+organization|deep\s+state)\b",
    "revenge": r"\b(?:vendetta\w*|blood\s+feud\w*|quest\s+for\s+revenge|out\s+for\s+revenge|avenge\s+(?:the\s+)?(?:murder|death))\b",
    "martial arts": r"\b(?:martial\s+art\w*|kung\s+fu|ninjutsu|ninja\w*|samurai|ronin|dojo|shogun\w*|katana)\b",
    "sword and sorcery": r"\b(?:sword\s+and\s+sorcery|dungeon\w*|barbarian\w*|magic\s+sword|high\s+fantasy|dark\s+fantasy|elves|dwarv\w*|magical\s+realm\w*)\b",
    "mythology": r"\b(?:mytholog\w*|folklore|pantheon|old\s+gods|norse\s+god\w*|greek\s+god\w*|arthurian|fairy\s+tale\w*|fable\w*)\b",
    "war": r"\b(?:battlefield\w*|platoon\w*|trench\w*|world\s+war\s+(?:i|ii|one|two|three)|vietnam\s+war|cold\s+war|front\s+line\w*|war\s+zone\w*|special\s+forces)\b",
    "survival": r"\b(?:shipwreck\w*|stranded|wilderness\s+survival|fight\s+for\s+survival|quarantine\s+zone)\b",
    "prison": r"\b(?:maximum\s+security|prison\s+break|penitentiary|cellblock\w*|death\s+row|inmate\w*)\b",
    "steampunk": r"\b(?:steampunk|clockwork|airship\w*|victorian\s+london|gaslamp)\b",
    "pirates": r"\b(?:buccaneer\w*|galleon\w*|high\s+seas|mutiny|privateer\w*|pirate\s+ship\w*|pirate\s+crew)\b",
}
_THEME_RX = {k: re.compile(p, re.I) for k, p in THEME_PATTERNS.items()}
# 10,000 issue blurbs take 1.5 s to read (one merged pattern was slower, 2.1 s), and the
# index rebuilds whenever a record lands: a series' themes are kept against its blurbs
_theme_memo: dict[tuple, tuple[int, list[str]]] = {}
# Words that are also somebody's name: capitalised mid-sentence they're the Silk
# Spectre (Watchmen came out "ghosts"), Cyborg, the Scarlet Witch, Leviathan
# (Checkmate came out "monsters"), not the thing.
_NAME_WORDS = re.compile(r"^(?:spectre|specter|cyborg|android|witch|leviathan)", re.I)

# --- phase 2: liked by the same readers ----------------------------------------------
COMMUNITY = (2.0, 0.4)              # cap, τ. The partner's τ 0.12 put five shared readers (1.4) level with a hundred (2.0)
COMMUNITY_CARD_MIN = 1.0            # 'cards' only: a card of its own needs this much
COMM_MIN_RATERS, COMM_MIN_POS = 5, 3     # people who rated both, and who liked both more than they like most things
COMM_ALPHA, COMM_K = 0.05, 25.0     # the rest of a series' raters count a little; shrink by n / (n + 25)
COMMUNITY_REASON = "Liked by the same readers"

_ROLE_LABEL = {"writer": "Writer", "story": "Writer", "script": "Writer", "plot": "Writer", "artist": "Artist",
               "penciller": "Artist", "inker": "Inker", "colorist": "Colorist", "letterer": "Letterer"}


def _label(role: str) -> str:
    """'artist, penciller, inker' (LOCG writes them together) reads as its strongest part."""
    from kometa import related
    parts = [p.strip() for p in (role or "").lower().split(",") if p.strip()] or [""]
    best = max(parts, key=lambda p: related._role_weight(p))
    return _ROLE_LABEL.get(best, best.title() or "Creator")


def _saturate(raw: float, cap: float, tau: float) -> float:
    return cap * (1.0 - math.exp(-raw / tau)) if raw > 0 else 0.0


# --- the index -------------------------------------------------------------------------
class Index:
    """Per series, per channel: {feature: weight}, plus the postings to walk them."""

    def __init__(self):
        self.features: dict[str, dict[int, dict[str, float]]] = defaultdict(dict)
        self.labels: dict[str, dict[str, str]] = defaultdict(dict)     # channel → feature → display text
        self.roles: dict[int, dict[str, str]] = {}                     # series → creator → role label
        self.postings: dict[str, dict[str, list[tuple[int, float]]]] = {}
        self.idf: dict[str, dict[str, float]] = {}
        self.stop: dict[str, set[str]] = {}
        self.associated: dict[int, set[int]] = defaultdict(set)
        self.family: dict[int, set[int]] = defaultdict(set)
        self.franchise: dict[int, str] = {}
        self.series: dict[int, dict] = {}
        self.lists: dict[int, dict[int, float]] = {}
        self.idf_median: dict[str, float] = {}
        self.franchise_members: dict[str, list[int]] = defaultdict(list)
        self.damp: dict[int, float] = {}                               # series → hub damping factor
        self.opts: dict = {"themes": False, "community": "off"}
        self.unposted: dict[str, dict[int, set]] = defaultdict(dict)  # channel → series → features others can't reach it by
        self.community: dict[int, dict[int, float]] = {}               # series → series → raw similarity

    def finish(self):
        for ch, per in self.features.items():
            post: dict[str, list] = defaultdict(list)
            hidden = self.unposted.get(ch, {})
            for sid, feats in per.items():
                for k, w in feats.items():
                    if k not in hidden.get(sid, ()):
                        post[k].append((sid, w))
            n = max(1, len(per))
            self.postings[ch] = post
            self.idf[ch] = {k: math.log(1 + n / len(v)) for k, v in post.items()}
            share = STOP_SHARE_BY.get(ch, STOP_SHARE)
            self.stop[ch] = set() if ch == "universe" else {k for k, v in post.items() if len(v) / n > share and n >= 20}
            vals = sorted(self.idf[ch].values())
            self.idf_median[ch] = (vals[len(vals) // 2] if vals else 1.0) or 1.0
        self.stop["universe"] = self.stop.get("universe", set()) | DEFAULT_UNIVERSES
        for sid, f in self.franchise.items():
            self.franchise_members[f].append(sid)
        return self


def _owned_credits(path, one_offs: dict | None = None) -> dict[int, dict[str, tuple[float, str, str]]]:
    """series → creator key → (role weight, role label, name), from the issues you own
    where you own any credited ones, else the run's whole record. For an anthology,
    `one_offs` gets the contributors in only one of its issues: a way out of its row,
    never a way in."""
    from kometa import record, related
    record.ensure_tables(path)
    per: dict[int, list[tuple[list, bool]]] = defaultdict(list)
    with db._connect(path) as conn:
        for sid, cj, owned in conn.execute("""
                SELECT r.tracked_series_id, r.credits_json, COALESCE(i.owned, 0)
                FROM issue_record r LEFT JOIN issue_status i
                  ON i.tracked_series_id = r.tracked_series_id AND i.number = r.number
                WHERE r.credits_json IS NOT NULL AND r.credits_json NOT IN ('', '[]')"""):
            try:
                per[sid].append((json.loads(cj), bool(owned)))
            except (TypeError, ValueError):
                continue
    out = {}
    for sid, rows in per.items():
        mine = [c for c, o in rows if o]
        use = mine or [c for c, _ in rows]
        total = len(use)
        # Over Batman (1940)'s 693 catalogue issues (you own none) 20% means 139: Doug
        # Moench's 103 and Grant Morrison's 28 fell out, a colorist's 189 stayed. A run
        # you own none of keeps anyone on 25+ issues.
        floor = (LONG_RUN_FLOOR if not mine and total > LONG_RUN else max(1, math.ceil(total * PRESENCE)))
        seen: Counter = Counter()
        info = {}
        for credits in use:
            for c in {(norm_key(x.get("name") or ""), (x.get("role") or "").lower(), x.get("name")) for x in credits}:
                if c[0]:
                    seen[c[:2]] += 1
                    info[c[:2]] = c[2]
        strong = {k for k, role in seen if related._role_weight(role) >= STRONG_CREDIT}
        anthology = total <= ANTHOLOGY_RUN and len(strong) / total > ANTHOLOGY_PEOPLE
        issues_in: Counter = Counter()
        for (k, role), n in seen.items():
            issues_in[k] = max(issues_in[k], n)
        feats = {}
        for (k, role), n in seen.items():
            w = related._role_weight(role)
            if w <= 0 or (total > 1 and n < floor):
                continue
            if k not in feats or w > feats[k][0]:
                feats[k] = (w, _label(role), info[(k, role)])
        if anthology and one_offs is not None:
            one_offs[sid] = {k for k in feats if issues_in[k] < 2}
        out[sid] = feats
    return out


def _cast(path) -> tuple[dict, dict, dict, dict]:
    """(characters, teams, universes, display names): series → {name key: presence} from
    Metron's per-issue cast, owned issues first; leads only (presence ≥ CAST_PRESENCE,
    on 2+ issues)."""
    per: dict[int, list[tuple[dict, bool]]] = defaultdict(list)
    with db._connect(path) as conn:
        for sid, xj, owned in conn.execute("""
                SELECT r.tracked_series_id, r.extra_json, COALESCE(i.owned, 0)
                FROM issue_record r LEFT JOIN issue_status i
                  ON i.tracked_series_id = r.tracked_series_id AND i.number = r.number
                WHERE r.extra_json LIKE '%characters%' OR r.extra_json LIKE '%teams%' OR r.extra_json LIKE '%universes%'"""):
            try:
                per[sid].append((json.loads(xj) or {}, bool(owned)))
            except (TypeError, ValueError):
                continue
    chars, teams, unis, names = {}, {}, {}, {}
    for sid, rows in per.items():
        mine = [x for x, o in rows if o]
        use = mine if len(mine) >= 2 else [x for x, _ in rows]
        total = len(use)
        for field, dest, top in (("characters", chars, CAST_TOP_CHARACTERS), ("teams", teams, CAST_TOP_TEAMS),
                                 ("universes", unis, None)):
            cnt = Counter(norm_key(n) for x in use for n in set(x.get(field) or []) if n)
            for x in use:
                for n in x.get(field) or []:
                    names.setdefault(norm_key(n), n)
            leads = {k: c / total for k, c in cnt.items() if c >= 2 and c / total >= CAST_PRESENCE}
            if top:
                leads = dict(sorted(leads.items(), key=lambda kv: -kv[1])[:top])
            if leads:
                dest[sid] = leads
    return chars, teams, unis, names


def options(path=None) -> dict:
    """The phase-2 switches, from config."""
    cfg = db.get_config(path or DB_PATH)
    mode = cfg.get("related_community") or "off"
    return {"themes": cfg.get("related_themes") == "on",
            "community": mode if mode in ("boost", "cards") else "off"}


def build_index(path=None, opts: dict | None = None) -> Index:
    from kometa import related, record
    from kometa.family import attach_families
    path = path or DB_PATH
    opts = options(path) if opts is None else opts
    ix = Index()
    ix.opts = opts
    series = related._series_with_counts(path)
    ix.series = series
    rows = [dict(s) for s in series.values()]
    attach_families(rows)
    for s in rows:
        group = set(s.get("family_children") or [])
        if s.get("family_parent"):
            group.add(s["family_parent"])
        ix.family[s["id"]] |= group
        for g in group:
            ix.family[g].add(s["id"])
    # creators (owned issues first), falling back to series_signals for runs with no record
    credits = _owned_credits(path, ix.unposted["creators"])
    sig = related._signals(path)
    for sid in set(sig) | set(credits):
        feats = credits.get(sid)
        if not feats:
            feats = {}
            for c in sig.get(sid, {}).get("creators", []):
                w = related._role_weight(c.get("role"))
                k = norm_key(c.get("name") or "")
                if w > 0 and k and (k not in feats or w > feats[k][0]):
                    feats[k] = (w, _label(c.get("role") or ""), c["name"])
        if feats:
            ix.features["creators"][sid] = {k: v[0] for k, v in feats.items()}
            ix.roles[sid] = {k: v[1] for k, v in feats.items()}
            for k, v in feats.items():
                ix.labels["creators"].setdefault(k, v[2])
        arcs = {norm_key(a): a for a in sig.get(sid, {}).get("arcs", []) if a}
        if arcs:
            ix.features["arcs"][sid] = {k: 1.0 for k in arcs}
            for k, a in arcs.items():
                ix.labels["arcs"].setdefault(k, a)
    chars, teams, unis, names = _cast(path)
    for sid, leads in chars.items():
        ix.features["cast"][sid] = {f"c:{k}": v for k, v in leads.items()}
    for sid, leads in teams.items():
        ix.features["cast"].setdefault(sid, {}).update({f"t:{k}": v for k, v in leads.items()})
    for k, n in names.items():
        ix.labels["cast"][f"c:{k}"] = n
        ix.labels["cast"][f"t:{k}"] = n
        ix.labels["universe"][k] = n
    for sid, leads in unis.items():
        ix.features["universe"][sid] = leads
    for sid, tags in record.effective_tags_all(path).items():
        feats = {t["key"]: t["weight"] * (THEME_MULT if t["kind"] == "theme" else 1.0) for t in tags}
        if feats:
            ix.features["genre"][sid] = feats
            for t in tags:
                ix.labels["genre"].setdefault(t["key"], ("Theme: " if t["kind"] == "theme" else "Genre: ") + t["key"])
    if opts.get("themes"):
        for sid, keys in description_themes(path).items():
            feats = ix.features["genre"].setdefault(sid, {})
            for k in keys:
                feats[k] = max(feats.get(k, 0.0), DESC_THEME_TRUST * THEME_MULT)
                ix.labels["genre"].setdefault(k, f"Theme: {k}")
    ix.community = _community(path) if opts.get("community") != "off" else {}
    # Metron's associated runs, mapped onto the shelf by Metron id
    by_metron = {}
    for s in series.values():
        for mid in [s.get("metron_series_id")] + [int(x) for x in (s.get("metron_extra_ids") or "").split(",") if x.strip().isdigit()]:
            if mid:
                by_metron[int(mid)] = s["id"]
    with db._connect(path) as conn:
        for sid, xj in conn.execute("SELECT tracked_series_id, extra_json FROM series_record WHERE extra_json LIKE '%associated%'"):
            try:
                assoc = (json.loads(xj) or {}).get("associated") or []
            except (TypeError, ValueError):
                continue
            for a in assoc:
                other = by_metron.get(int(a.get("id") or 0))
                if other and other != sid:
                    ix.associated[sid].add(other)
                    ix.associated[other].add(sid)
    for s in series.values():
        pub, stem = related._pub_key(s.get("publisher") or ""), related._clean_stem(s.get("title", "")).lower()
        if pub and stem and s.get("kind") != "arc":
            ix.franchise[s["id"]] = f"{pub}|{stem}"
    ix.lists = related._list_neighbours(path)
    ix.finish()
    # Hub damping, measured not assumed: how many top-5 rows each series lands in. An
    # anthology with fifty creators or the lead of thirty runs was in 30–36 rows on
    # 2026-10-16 against a 3% gate. Damping one Batman title promoted the next (56
    # Batman runs feed every Batman row), so it's re-counted with the damping applied
    # until no series is above the gate or HUB_PASSES runs out.
    rows = {sid: _score(ix, sid) for sid in ix.series}
    # over the series that have a row, as the harness counts: arcs don't (832 rows, 840
    # entries on 2026-10-17 let a 25th row through a gate of 24)
    limit = max(1, int(HUB_GATE * sum(1 for s in ix.series.values() if s.get("kind") != "arc")))
    for _ in range(HUB_PASSES):
        indeg: Counter = Counter()
        for cands in rows.values():
            top = sorted(cands, key=lambda c: (-c["score"] * ix.damp.get(c["series_id"], 1.0), c["title"] or ""))
            for c in _diversify(_one_per_family(ix, top), ROW)[:HUB_TOP]:      # the row exactly as served
                indeg[c["series_id"]] += 1
        if not indeg or indeg.most_common(1)[0][1] <= limit:
            break
        vals = sorted(indeg.values())
        med = max(1, vals[len(vals) // 2])
        for sid, n in indeg.items():
            # only what's over the gate: damping everything above the median pushed Paper
            # Girls (in ~10 rows, a good match, not a hub) out of Saga's top three
            if n > limit:
                ix.damp[sid] = ix.damp.get(sid, 1.0) / (1.0 + HUB_DAMP * math.log2(n / med))
    return ix


def description_themes(path=None) -> dict[int, list[str]]:
    """series → up to 3 themes read out of its descriptions. A theme counts when the
    series blurb names it twice, or once with one issue agreeing, or it's in 15% of
    the issue blurbs (two at least)."""
    path = path or DB_PATH
    s_desc, i_descs = {}, defaultdict(list)
    with db._connect(path) as conn:
        try:
            for sid, d in conn.execute("SELECT tracked_series_id, desc FROM series_record WHERE desc IS NOT NULL AND desc != ''"):
                s_desc[sid] = d
            for sid, d in conn.execute("SELECT tracked_series_id, desc FROM issue_record WHERE desc IS NOT NULL AND desc != ''"):
                i_descs[sid].append(d)
        except Exception:
            return {}

    def hits(rx, text) -> int:
        n = 0
        for m in rx.finditer(text or ""):
            w, before = m.group(0), text[:m.start()].rstrip()
            if _NAME_WORDS.match(w) and w[0].isupper() and before and before[-1] not in '.!?"\u201c':
                continue                                # the Silk Spectre, not a spectre
            n += 1
        return n

    out = {}
    for sid in set(s_desc) | set(i_descs):
        sd, ids = s_desc.get(sid, ""), sorted(i_descs.get(sid, []))
        key = hash((sd, tuple(ids)))
        memo = _theme_memo.get((path, sid))
        if memo and memo[0] == key:
            themes = memo[1]
        else:
            min_i = max(2, math.ceil(0.15 * len(ids))) if ids else 2
            scored = []
            for k, rx in _THEME_RX.items():
                s_hits = hits(rx, sd)
                per_issue = [hits(rx, t) for t in ids]
                i_cnt, i_hits = sum(1 for h in per_issue if h), sum(per_issue)
                if s_hits >= 2 or (s_hits >= 1 and i_cnt >= 1) or (i_cnt >= min_i and i_hits >= 2):
                    scored.append((i_cnt / max(1, len(ids)) + (0.5 if s_hits else 0.0), i_hits + s_hits, k))
            themes = [k for _, _, k in sorted(scored, reverse=True)[:3]]
            _theme_memo[(path, sid)] = (key, themes)
        if themes:
            out[sid] = themes
    return out


_comm_memo: dict[tuple, tuple[tuple, dict[int, dict[int, float]]]] = {}


def _cbr_fingerprint(conn):
    try:
        r = conn.execute("SELECT value FROM cbr_people_state WHERE key = 'scores_version'").fetchone()
        if r and r[0] is not None:
            return r[0]
    except Exception:
        pass
    try:
        r = conn.execute("SELECT MAX(rowid) FROM cbr_people_scores").fetchone()
        return r[0] if r else None
    except Exception:
        return None



def _community(path=None) -> dict[int, dict[int, float]]:
    """series → series → how alike CBR's reviewers rate them: cosine over scores centred
    on each person's own mean (someone who gives everything a 9 says little with a 9),
    critics at 0.3, prolific raters damped, shrunk by n / (n + 25) shared raters."""
    from kometa import cbr_people
    path = path or DB_PATH
    with db._connect(path) as conn:
        try:
            linked = cbr_people._linked(conn)
            if not linked:
                return {}
            cbr_stamp = _cbr_fingerprint(conn)
            memo_key = (cbr_stamp, tuple(sorted(linked.items())))
            memo = _comm_memo.get(path)
            if memo and memo[0] == memo_key:
                return memo[1]
            rows = conn.execute("SELECT person, series_path, AVG(score) FROM cbr_people_scores "
                                "GROUP BY person, series_path").fetchall()
        except Exception:
            return {}
    by_p = defaultdict(list)
    for p, sp, sc in rows:
        by_p[p].append((sp, sc))
    by_sid: dict[int, dict[str, float]] = defaultdict(dict)
    for p, v in by_p.items():
        on_shelf = sum(1 for sp, _ in v if sp in linked)
        if len(v) < 2 or on_shelf < 2:
            continue
        mean = sum(sc for _, sc in v) / len(v)
        w = (0.3 if p.startswith("critic:") else 1.0) / math.log2(2.0 + on_shelf)
        for sp, sc in v:
            if sp in linked:
                by_sid[linked[sp]][p] = (sc - mean) * w
    sq = {sid: sum(x * x for x in u.values()) for sid, u in by_sid.items()}
    out: dict[int, dict[int, float]] = defaultdict(dict)
    sids = list(by_sid)
    for i, a in enumerate(sids):
        ua = by_sid[a]
        for b in sids[i + 1:]:
            ub = by_sid[b]
            shared = ua.keys() & ub.keys()
            n = len(shared)
            if n < COMM_MIN_RATERS or sum(1 for u in shared if ua[u] > 0 and ub[u] > 0) < COMM_MIN_POS:
                continue
            dot = sum(ua[u] * ub[u] for u in shared)
            if dot <= 0:
                continue
            co_a, co_b = sum(ua[u] ** 2 for u in shared), sum(ub[u] ** 2 for u in shared)
            na = math.sqrt(co_a + COMM_ALPHA * (sq[a] - co_a))
            nb = math.sqrt(co_b + COMM_ALPHA * (sq[b] - co_b))
            raw = dot / (na * nb) * n / (n + COMM_K)
            out[a][b] = out[b][a] = raw
    res = dict(out)
    _comm_memo[path] = (memo_key, res)
    return res


_cache = {"at": 0.0, "path": None, "fp": None, "ix": None, "opts": None}
_lock = threading.Lock()
_building = threading.Lock()        # one background rebuild at a time (stale-while-revalidate)


def _fingerprint(path) -> tuple:
    with db._connect(path) as conn:
        def one(sql):
            try:
                return tuple(conn.execute(sql).fetchone())
            except Exception:
                return None
        return (one("SELECT COUNT(*), MAX(fetched_at) FROM series_signals"),
                one("SELECT COUNT(*), MAX(at) FROM series_tags"),
                one("SELECT COUNT(*), MAX(fetched_at) FROM issue_record"),
                one("SELECT COUNT(*), MAX(id) FROM tracked_series"),
                one("SELECT COUNT(*), MAX(fetched_at) FROM series_record"),
                _cbr_fingerprint(conn))



def _knows(ix: Index, series_id: int) -> bool:
    """Has the index got anything a fill brings? (Not the franchise: that's the title,
    there from the moment a series is tracked.)"""
    return any(series_id in per for per in ix.features.values()) or bool(
        ix.lists.get(series_id) or ix.associated.get(series_id))


def index(path=None, want: int | None = None) -> Index:
    """The warm index: rebuilt when what it reads has changed, at most every INDEX_TTL_S —
    or every FRESH_RECHECK_S when asked for a series (`want`) it has nothing on.

    Stale-while-revalidate (2026-10-29): a warm index whose inputs have moved is still SERVED
    and one background thread rebuilds it (~2.3 s). Before, the request that found the
    fingerprint moved rebuilt it inline, under the lock, with every other Related / On Deck
    call queued behind it — and the signals trickle, the record ticks and the overnight CBR
    pages moved it every few minutes. Only a cold start, or a series the index has nothing on
    yet (an empty row otherwise), still waits."""
    path = path or DB_PATH
    now = time.time()
    opts = options(path)
    with _lock:
        c = _cache
        same = c["ix"] is not None and c["path"] == path and c.get("opts") == opts
        known = same and (want is None or _knows(c["ix"], want))
        ttl = INDEX_TTL_S if not same or known else FRESH_RECHECK_S
        if same and now - c["at"] < ttl:
            return c["ix"]
        fp = _fingerprint(path)
        if same and c["fp"] == fp:
            c["at"] = now
            return c["ix"]
        if known:                                     # moved, but warm: serve this one, rebuild behind
            c["at"] = now                             # ...and don't re-fingerprint on every call meanwhile
            if _building.acquire(blocking=False):
                threading.Thread(target=_rebuild, args=(path, opts, fp), name="related-rebuild", daemon=True).start()
            return c["ix"]
        ix = build_index(path, opts)
        c.update(at=now, path=path, fp=fp, ix=ix, opts=opts)
        return ix


def _rebuild(path, opts, fp) -> None:
    """The background half of index(): build outside the lock, swap under it. Holds _building."""
    try:
        ix = build_index(path, opts)
        with _lock:
            _cache.update(at=time.time(), path=path, fp=fp, ix=ix, opts=opts)
    except Exception as e:
        logging.getLogger(__name__).warning(f"Related index rebuild failed: {e}")
    finally:
        _building.release()


def prewarm(path=None) -> threading.Thread | None:
    """Build the warm index in the background at startup, so the first Related row after
    a deploy doesn't wait ~2.3 s for it. Only when the combined engine is the one in use."""
    path = path or DB_PATH
    if db.get_config(path).get("related_engine") != "combined":
        return None

    def run():
        try:
            index(path)
        except Exception as e:
            logging.getLogger(__name__).warning(f"Related index pre-warm failed: {e}")
    t = threading.Thread(target=run, name="related-prewarm", daemon=True)
    t.start()
    return t


# --- scoring -----------------------------------------------------------------------------
def _channel_raw(ix: Index, ch: str, seed: int) -> dict[int, tuple[float, str, float]]:
    """candidate → (raw score, the feature behind its reason, the strongest shared weight)."""
    mine = ix.features.get(ch, {}).get(seed) or {}
    if not mine:
        return {}
    idf = ix.idf.get(ch, {})
    med = ix.idf_median.get(ch, 1.0)
    stop = ix.stop.get(ch, set())
    post = ix.postings.get(ch, {})
    if ch == "genre":
        # IDF-weighted cosine over the two series' tag vectors
        def vec(feats):
            return {k: w * idf.get(k, 1.0) for k, w in feats.items() if k not in stop}
        a = vec(mine)
        na = math.sqrt(sum(v * v for v in a.values())) or 1.0
        out = {}
        for sid in {sid for k in a for sid, _ in post.get(k, [])}:
            if sid == seed:
                continue
            b = vec(ix.features[ch].get(sid, {}))
            dot = {k: a[k] * b[k] for k in a.keys() & b.keys()}
            if not dot:
                continue
            nb = math.sqrt(sum(v * v for v in b.values())) or 1.0
            best = max(dot, key=dot.get)
            out[sid] = (sum(dot.values()) / (na * nb), best, a[best])
        return out
    acc: dict[int, dict] = {}
    for k, w in mine.items():
        if k in stop:
            continue
        f = max(0.5, min(1.5, idf.get(k, med) / med))
        for sid, w2 in post.get(k, []):
            if sid == seed:
                continue
            shared = min(w, w2)
            if ch == "creators":
                part = shared * f
            elif ch == "arcs":
                part = 3.0 * f
            else:                                       # universe, cast: idf · shared presence
                part = idf.get(k, 1.0) * shared
            a = acc.setdefault(sid, {"sum": 0.0, "best": None, "best_rank": -1.0, "top_part": 0.0, "strength": 0.0})
            a["sum"] += part
            a["strength"] = max(a["strength"], shared)
            a["top_part"] = max(a["top_part"], part)
            # the reason: a shared team names it over one of its members ("Team: Fantastic
            # Four", not "Stars Human Torch" — round 3); the score is untouched by this
            rank = part * (1.25 if k.startswith("t:") else 1.0)
            if rank > a["best_rank"]:
                a["best"], a["best_rank"] = k, rank
    if ch == "cast":
        # the strongest shared lead counts whole, the rest a quarter: Batman, Gordon
        # and Alfred are one reason, not three
        return {sid: (a["top_part"] + 0.25 * (a["sum"] - a["top_part"]), a["best"], a["strength"])
                for sid, a in acc.items()}
    return {sid: (a["sum"], a["best"], a["strength"]) for sid, a in acc.items()}


def _reason(ix: Index, ch: str, seed: int, feat: str) -> str:
    lab = ix.labels.get(ch, {}).get(feat, feat)
    if ch == "creators":
        return f"{(ix.roles.get(seed) or {}).get(feat, 'Creator')}: {lab}"
    if ch == "arcs":
        return f"Arc: {lab}"
    if ch == "cast":
        return ("Team: " if feat.startswith("t:") else "Stars ") + lab
    return lab                                          # universe name; genre labels carry their prefix


def _score(ix: Index, series_id: int) -> list[dict]:
    """Every candidate that can make a card, scored and explained, before damping and
    the diversity rule."""
    contrib: dict[int, dict[str, float]] = defaultdict(dict)
    reasons: dict[int, dict[str, str]] = defaultdict(dict)
    can_card: dict[int, bool] = defaultdict(bool)
    credit: dict[int, float] = {}
    for ch, (cap, tau) in CHANNELS.items():
        if ch == "lists":
            continue
        for sid, (raw, feat, strength) in _channel_raw(ix, ch, series_id).items():
            c = _saturate(raw, cap, tau)
            if c <= 0:
                continue
            contrib[sid][ch] = c
            reasons[sid][ch] = _reason(ix, ch, series_id, feat)
            if ch == "creators":
                credit[sid] = strength
                if strength >= STRONG_CREDIT:
                    can_card[sid] = True
            elif c >= CARD_MIN.get(ch, 0.0):
                can_card[sid] = True
    cap, tau = CHANNELS["lists"]
    for sid, w in (ix.lists.get(series_id) or {}).items():
        contrib[sid]["lists"] = _saturate(w, cap, tau)
        reasons[sid]["lists"] = "On a reading list together"
        can_card[sid] = True
    for sid in ix.associated.get(series_id, ()):
        contrib[sid]["associated"] = ASSOCIATED_FLAT
        reasons[sid]["associated"] = "Companion series"
        can_card[sid] = True
    fr = ix.franchise.get(series_id)
    if fr:
        from kometa import related
        label = f"Franchise: {related._clean_stem(ix.series.get(series_id, {}).get('title', ''))}"
        for sid in ix.franchise_members.get(fr, ()):
            if sid != series_id:
                contrib[sid]["franchise"] = FRANCHISE_FLAT
                reasons[sid]["franchise"] = label
                can_card[sid] = True
    if ix.opts.get("community") != "off":
        # a boost to what another channel found; with 'cards', strong enough to stand alone
        cap, tau = COMMUNITY
        for sid, raw in (ix.community.get(series_id) or {}).items():
            c = _saturate(raw, cap, tau)
            contrib[sid]["community"] = c
            reasons[sid]["community"] = COMMUNITY_REASON
            if ix.opts["community"] == "cards" and c >= COMMUNITY_CARD_MIN:
                can_card[sid] = True
    family = ix.family.get(series_id, set())
    out = []
    for sid, chans in contrib.items():
        s = ix.series.get(sid)
        if sid == series_id or sid in family or not can_card[sid] or not s or s.get("kind") == "arc":
            continue
        score = sum(chans.values())
        order = sorted(chans, key=lambda ch: -chans[ch])
        # a channel says its piece when it gave a quarter of the score — or, for genre and
        # cast, when it's strong enough to have made the card alone (Saga → Paper Girls:
        # "Writer: Brian K. Vaughan, Genre: science fiction")
        why = [reasons[sid][ch] for ch in order
               if chans[ch] >= REASON_SHARE * score or chans[ch] >= CARD_MIN.get(ch, float("inf"))][:3] \
            or [reasons[sid][order[0]]]
        out.append({"series_id": sid, "title": s["title"], "publisher": s.get("publisher"), "score": score,
                    "why": why, "owned": s.get("owned") or 0,
                    "total": (s.get("owned") or 0) + (s.get("missing") or 0),
                    "channels": {ch: round(v, 3) for ch, v in chans.items()},
                    "credit_strength": credit.get(sid, 0.0)})
    return out


_with_cache: dict = {}


def _index_with(path, opts: dict) -> Index:
    """An index built with these switches whatever config says — so the harness can
    put the phase-2 settings side by side on one copy of the database."""
    key = (path, tuple(sorted(opts.items())))
    with _lock:
        if key not in _with_cache:
            _with_cache[key] = build_index(path, dict(opts))
        return _with_cache[key]


def related_combined(series_id: int, limit: int = 12, path=None, explain: bool = False,
                     opts: dict | None = None) -> list[dict]:
    """The Related row for one series: same card shape as related.related(), plus
    `channels` ({channel: contribution}) and `credit_strength` (the strongest credit
    the two share, for the harness). `opts` overrides the phase-2 switches (harness)."""
    path = path or DB_PATH
    ix = index(path, want=series_id) if opts is None else _index_with(path, opts)
    ranked = _score(ix, series_id)
    for c in ranked:
        c["score"] = round(c["score"] * ix.damp.get(c["series_id"], 1.0), 2)
    ranked.sort(key=lambda c: (-c["score"], c["title"] or ""))
    return _diversify(_one_per_family(ix, ranked), limit)


def _one_per_family(ix: Index, ranked: list[dict]) -> list[dict]:
    """The best card per family: Ex Machina, not Ex Machina and its Special; Head Lopper,
    not Head Lopper and the same title shelved twice."""
    from kometa.family import _base
    out, taken = [], set()
    for c in ranked:
        sid = c["series_id"]
        keys = {("s", m) for m in ix.family.get(sid, set()) | {sid}} | {("t", norm_key(_base(c["title"] or "")))}
        if keys & taken:
            continue
        taken |= keys
        out.append(c)
    return out


def _diversify(ranked: list[dict], limit: int) -> list[dict]:
    """No more than DIVERSITY_TOP_MAX of the first DIVERSITY_TOP cards, or DIVERSITY_ALL_MAX
    of the row, share a primary reason — so a second reason gets a slot whenever there is
    one. What's held back fills whatever is left: when one writer is all a series has,
    holding their books back would leave fewer cards, not different ones."""
    out, held = [], []
    count: Counter = Counter()
    for c in ranked:
        if len(out) >= limit:
            break
        p = c["why"][0]
        if (len(out) < DIVERSITY_TOP and count[p] >= DIVERSITY_TOP_MAX) or count[p] >= DIVERSITY_ALL_MAX:
            held.append(c)
            continue
        out.append(c)
        count[p] += 1
    return out + held[:max(0, limit - len(out))]
