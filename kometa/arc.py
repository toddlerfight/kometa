"""Story-arc logic — the LENS model.

An arc owns NOTHING. It's a cross-title reading-order overlay over tracked SERIES:
its rows point at issues that live in their real series' folders, and its collected
editions are trades of the arc's MAIN series. This module is the arc-specific logic
that the series-first flow leans on — starting with: which series is "main"?
"""
import re
from collections import Counter


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def main_series_title(arc_issues: list[dict], arc_name: str = "") -> str | None:
    """The arc's MAIN series = the most-represented source title among its issues;
    ties broken by whichever leader the arc name leads with.

    Knightfall (10 Batman, 8 Detective, 3 Shadow of the Bat, 2 Showcase) → 'Batman'.
    The trade lives here, in this series' Trades tab — the arc owns no folder.
    """
    titles = [i.get("source_title") for i in arc_issues if i.get("source_title")]
    if not titles:
        return None
    counts = Counter(titles).most_common()
    best = counts[0][1]
    leaders = [t for t, c in counts if c == best]
    if len(leaders) == 1:
        return leaders[0]
    # tie: prefer the leader whose name the arc title leads with ("Batman Knightfall")
    an = _norm_title(arc_name)
    return next((t for t in leaders if _norm_title(t) in an), leaders[0])
