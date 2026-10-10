"""Families: a run and its specials, shown as one.

The catalogues file 'Absolute Batman: Ark M' and 'Absolute Batman 2025 Annual'
as their own series — their own ids, dates and issue lists — and Kometa mirrors
catalogue series one-to-one, so the Library showed three cards. This folds the
specials under the run for DISPLAY only: numbering, pull list and reading order
stay separate, because an annual is not #25.

A child is a series whose title is the parent's title plus a separator and a
tail, where the parent is a tracked series on the same shelf and the child is a
special (one-shot, annual, limited, OGN — by Metron type, or ≤ 12 issues when
the type is unknown). A one-word parent ('Batman') claims only the special-word
forms ('Batman Annual 2025'), never 'Batman: The Dark Knight Returns'.
"""
import re

from kometa.naming import norm_key

_SPECIAL_WORDS = re.compile(r"^(annual|special|one-?shot|giant-?size|prelude|preview|primer|sketchbook|yearbook|"
                            r"holiday special|winter special|summer special|\d{4} annual)\b", re.I)
_SPECIAL_TYPES = {"one-shot", "annual", "annual series", "limited series", "graphic novel", "one shot"}
_YEAR = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")


def _base(title: str) -> str:
    return _YEAR.sub("", (title or "").strip().lstrip("-–— ").strip())


def _is_special(s: dict) -> bool:
    t = (s.get("metron_type") or "").lower()
    if t:
        return t in _SPECIAL_TYPES
    n = (s.get("owned") or 0) + (s.get("missing") or 0) + (s.get("upcoming") or 0)
    return 0 < n <= 12


def _tail_after(parent: str, child: str) -> str | None:
    """The part of the child's title after the parent's, or None if it isn't a prefix."""
    p, c = norm_key(parent), norm_key(child)
    if not p or c == p or not c.startswith(p):
        return None
    tail = c[len(p):]
    if not tail.startswith(" "):
        return None                               # 'Batman' vs 'Batmanx'
    return tail.strip()


def attach_families(series: list[dict]) -> list[dict]:
    """Stamp family_parent (id) on children and family_children (ids) on parents."""
    rows = [s for s in series if s.get("kind") != "arc"]
    by_id = {s["id"]: s for s in rows}
    for s in rows:
        s["family_parent"] = None
        s["family_children"] = []
    for child in rows:
        if not _is_special(child):
            continue
        ctitle = _base(child["title"])
        best = None
        for parent in rows:
            if parent is child or (parent.get("metron_type") or "").lower() in ("one-shot", "annual"):
                continue                              # a one-shot never parents anything
            ptitle = _base(parent["title"])
            tail = _tail_after(ptitle, ctitle)
            if tail is None:
                continue
            # the separator in the ORIGINAL title tells a subtitle (':' / ' - ') from a plain word
            sep = ctitle[len(ptitle):len(ptitle) + 3]
            subtitle = sep.startswith(":") or sep.startswith(" -") or sep.startswith(" –")
            special_word = bool(_SPECIAL_WORDS.match(tail))
            one_word_parent = len(ptitle.split()) == 1
            if one_word_parent and not special_word:
                continue
            if not (subtitle or special_word):
                continue
            # a special comes with or after its run, never before it: the 1999
            # 'Batman Beyond - Volume 01' is not a special of Batman Beyond (2012)
            cy, py = child.get("year_began"), parent.get("year_began")
            if cy and py and cy < py - 1:
                continue
            if best is None or len(ptitle) > len(_base(best["title"])):
                best = parent
        if best is not None:
            child["family_parent"] = best["id"]
            best["family_children"].append(child["id"])
    return series


# --- franchises: one level above families ---------------------------------------
# Nine leading names cover 220 of the Library's 718 top-level cards (Batman 56,
# Aliens 45, Hellboy 31 …). A to Z was a scroll through dozens of Aliens. A
# franchise is a leading name shared by five or more top-level series; the
# Library draws one stack card for it and the members live on the stack's page.
# Specials keep their family inside the stack. Everything is additive — the
# member rows gain a `franchise` stamp, nothing else changes.
FRANCHISE_MIN = 5
_LEAD_YEAR = re.compile(r"^\s*[\[(](?:19|20)\d{2}[\])]\s*")
_SEP = re.compile(r"\s+[-–—]\s+|:\s+|(?<=\w)-\s+")
# first words that say nothing on their own: key on two words when the second varies
_STOP = {"star", "tank", "dark", "black", "marvel", "new", "the", "i", "war", "sin", "spider", "dead",
         "zombies"}          # Zombies vs Robots is the run; Zombies Christmas Carol is not (2026-10-10)


def _franchise_base(title: str) -> str:
    t = _LEAD_YEAR.sub("", (title or "").strip().lstrip("-–— ").strip())
    t = _YEAR.sub("", t).strip()
    return re.sub(r"^(the)\s+", "", t, flags=re.I)


def _lead_words(title: str) -> list[str]:
    lead = _SEP.split(_franchise_base(title), 1)[0]
    return norm_key(lead).split()


def _clean_prefix(p: str, titles: list[str] | None = None) -> str:
    """The shared prefix as a NAME: no half words ('Aliens - A' when the next
    titles continue 'Alchemy' / 'Apocalypse'), no trailing separators."""
    if titles and p and p[-1].isalnum() and any(len(t) > len(p) and t[len(p)].isalnum() for t in titles):
        p = p[:p.rfind(" ")] if " " in p else ""
    return p.strip(" -–—:(")


def _common_prefix(titles: list[str]) -> str:
    if not titles:
        return ""
    a, b = min(titles), max(titles)
    i = 0
    while i < min(len(a), len(b)) and a[i].lower() == b[i].lower():
        i += 1
    return a[:i]


def _shared_words(titles: list[str]) -> str:
    """The leading words every title shares, compared without case or
    punctuation ('Zombies Vs. Robots' = 'Zombies vs Robots'), spelt the way
    most members spell them."""
    import re
    from collections import Counter
    split = [t.split() for t in titles if t]
    norm = lambda w: re.sub(r"[^a-z0-9]", "", w.lower())
    n = 0
    while split and all(len(w) > n for w in split) and len({norm(w[n]) for w in split}) == 1:
        n += 1
    if not n:
        return ""
    words = [Counter(w[i].rstrip(".:") for w in split).most_common(1)[0][0] for i in range(n)]
    while words and not re.search(r"[A-Za-z0-9]", words[-1]):      # 'Sin City -': a dash is no part of a name
        words.pop()
    return " ".join(words)


def attach_franchises(series: list[dict], minimum: int = FRANCHISE_MIN) -> list[dict]:
    """Stamp franchise {key, name, count} on every member of a leading-name group
    of `minimum` or more top-level series (and on their folded specials)."""
    rows = [s for s in series if s.get("kind") != "arc"]
    for s in rows:
        s["franchise"] = None
    top = [s for s in rows if not s.get("family_parent")]
    words = {s["id"]: _lead_words(s["title"]) for s in top}
    # a stop word leads with two words when its second word varies across the shelf
    seconds: dict[str, set] = {}
    for w in words.values():
        if len(w) >= 2:
            seconds.setdefault(w[0], set()).add(w[1])
    def key_of(w: list[str]) -> str | None:
        if not w:
            return None
        if w[0] in _STOP and len(seconds.get(w[0], ())) >= 2:
            return " ".join(w[:2]) if len(w) >= 2 else None
        return w[0]
    groups: dict[str, list[dict]] = {}
    for s in top:
        k = key_of(words[s["id"]])
        if k:
            groups.setdefault(k, []).append(s)
    by_id = {s["id"]: s for s in rows}
    for k, members in groups.items():
        if len(members) < minimum:
            continue
        bases = [_franchise_base(m["title"]) for m in members]
        name = _clean_prefix(_common_prefix(bases), bases)
        shared = _shared_words(bases)
        if len(name) < 3 or len(shared.split()) > len(name.split()):
            name = shared or " ".join(w.capitalize() for w in k.split())
        stamp = {"key": k, "name": name, "count": len(members)}
        for m in members:
            m["franchise"] = dict(stamp)
            for cid in m.get("family_children") or []:
                if cid in by_id:
                    by_id[cid]["franchise"] = dict(stamp)
    return series
