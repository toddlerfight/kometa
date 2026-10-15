"""Is this run still going? The series page's badge: Ongoing / Ended / Hiatus,
or nothing when nobody knows.

No "Cancelled" badge, ever. Metron's volunteers stamp Cancelled on anything that
stopped — Sandman, Transmetropolitan, Y: The Last Man, East of West, 118 of this
library's runs — so the word carries no signal; a planned finale and an axed run
look identical in their data. Both read as Ended.

Metron's status alone can't be trusted: every series is born 'Ongoing' there and
stays that way until a volunteer changes it, so a run that died in 2019 still
says Ongoing. Our own dates can't be trusted alone either: a monthly between
solicits looks finished for a few weeks. So, in order — first answer wins:

1. Something solicited (a store date after today) → Ongoing. Whatever Metron says.
2. Metron Completed / Cancelled → Ended; Hiatus → Hiatus.
3. Metron has a year it ended, or the run is finite by kind (limited, one-shot,
   graphic novel…) with nothing to come → Ended.
4. Our last release: within 6 months → Ongoing; within 2 years → Hiatus;
   older → Ended. Metron's default 'Ongoing' never overrules a dead run.
5. No dates at all → no badge. Guessing is worse than saying nothing.
"""
from datetime import date

ACTIVE_DAYS = 180             # a release this recent and it's still running (sync.py's window too)
QUIET_DAYS = 730              # gone this long without a word: it's over
FINITE_TYPES = {"limited series", "one-shot", "graphic novel", "annual", "trade paperback",
                "hardcover", "omnibus", "digital chapter"}


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days


def compute(series: dict, issues: list[dict], today: str | None = None) -> dict | None:
    """{'state': 'ongoing'|'finished'|'hiatus', 'label': str, 'why': str} or None."""
    today = today or date.today().isoformat()
    dates = [str(i["store_date"])[:10] for i in issues if i.get("store_date")]
    if any(d > today for d in dates):
        return {"state": "ongoing", "label": "Ongoing", "why": "next issue solicited"}
    m = (series.get("metron_status") or "").strip()
    if m == "Completed":
        return {"state": "finished", "label": "Ended", "why": "Metron: completed"}
    if m == "Cancelled":
        return {"state": "finished", "label": "Ended", "why": "Metron: run ended"}
    if m == "Hiatus":
        return {"state": "hiatus", "label": "Hiatus", "why": "Metron: on hiatus"}
    if series.get("metron_year_end"):
        return {"state": "finished", "label": "Ended", "why": f"ended {series['metron_year_end']}"}
    if (series.get("metron_type") or "").strip().lower() in FINITE_TYPES:
        return {"state": "finished", "label": "Ended", "why": f"{series['metron_type'].lower()}, nothing to come"}
    past = [d for d in dates if d <= today]
    if not past:
        return None
    age = _days(max(past), today)
    if age <= ACTIVE_DAYS:
        return {"state": "ongoing", "label": "Ongoing", "why": "an issue out in the last 6 months"}
    if age <= QUIET_DAYS:
        return {"state": "hiatus", "label": "Hiatus", "why": "nothing out in 6+ months, nothing solicited"}
    return {"state": "finished", "label": "Ended", "why": "nothing out in 2+ years"}
