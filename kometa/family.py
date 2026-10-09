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
            if best is None or len(ptitle) > len(_base(best["title"])):
                best = parent
        if best is not None:
            child["family_parent"] = best["id"]
            best["family_children"].append(child["id"])
    return series
