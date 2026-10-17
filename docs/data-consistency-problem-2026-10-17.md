# Consistent comic data: problem statement (2026-10-17)

## Problem

Kometa leans on Metron for almost everything it knows about a comic: description, credits,
release date, cover, page count, and the series' description and genres. Metron is
volunteer-curated, so its coverage is uneven. New issues get their credits days after release,
older issues often have a cover date but no on-sale date, and some publishers and eras are
thinner than others. Where Metron has a gap, Kometa usually shows the gap: an issue sheet with a
description and no credits, an issue with no date that sorts and files oddly, a series page with
no description.

**How might we** source each piece of a comic's details so every comic gets the same experience:
the same fields filled, links that work, dates that sort, whatever catalogue happens to know it,
without putting the burden on the owner and within each source's limits and terms?

## What "consistent" means

For every comic on the shelf (and every issue of a tracked run):

| Field | Used by | Today's main source |
|---|---|---|
| Description | Issue sheet, ComicInfo | Metron |
| Credits, each with a person id | Issue sheet links, creator pages, More from rows, Related | Metron (LOCG for runs Metron lacks) |
| On-sale date | Release-date sort, On Deck rows, missing vs upcoming | Metron |
| Cover | Everywhere | Metron, LOCG, the file |
| Page count | Reader, quick-read rows | Metron |
| Series description and genres | Series page, Related themes | Metron, GCD genres |
| Community rating | Issue sheet, Top rated sort | LOCG, ComicBookRoundup |

## Evidence (live library, 2026-10-17)

10,472 tracked issues across 833 series.

**By how the series is linked:**

| Series link | Issues | No description | No credits | No on-sale date | No page count | No cover |
|---|---|---|---|---|---|---|
| Metron | 9,757 | 3% | 1% | 24% | 8% | 0% |
| LOCG only | 513 | 16% | 8% | 2% | 92% | 6% |
| No catalogue | 202 | 79% | 89% | 32% | 2% | 96% |

LOCG-only credits carried no Metron person ids (92%) until today's name lookup, so their names
couldn't link.

**By when the issue came out:**

| Era | Issues | Notable gaps |
|---|---|---|
| Not out yet | 87 | 69% no credits (solicitation stage) |
| Last 60 days | 64 | 9% no credits (Metron fills them days after release; 100 Bullets: The US of Anger #4) |
| 2011–2026 | 6,385 | mostly complete; 15% no page count |
| 1991–2010 | 1,474 | 6% no description, 12% no page count |
| 1990 and earlier | 51 | 35% no description, credits or cover |
| No on-sale date at all | 2,408 (23%) | mostly Metron back catalogue with a cover date only; Dark Horse 55% |

**Series level:** 472 of 833 series (57%) have no series description, 326 (39%) no genres, 147
(18%) no creators on record, 120 no catalogue link at all (this includes the owner's deliberate
"no match" answers, which stay as they are). Community ratings exist on 40% of issue records.

## Why the gaps happen

1. **One source per record.** When Metron answers, its answer is the record, even with an empty
   field. A Metron row with no credits never asks LOCG or anyone else for credits. Fields are not
   merged across sources.
2. **Timing.** Metron fills credits after release, and solicitations arrive in stages. Until
   today a row counted as fresh for 90 days whatever it lacked (fixed for credits: a recent issue
   with none is re-asked daily for 60 days).
3. **Walls.** LOCG's issue pages now carry a Cloudflare check FlareSolverr can't pass; its lists
   and search still work. Fandom is walled too. Comic Vine is rate-limited per resource.
4. **No catalogue at all.** Indie, small-press and some collected editions aren't in Metron, and
   nothing stands in except the file's own tags.
5. **Back-catalogue dates.** Metron often has only a cover date for older issues. Cover dates run
   about two months after the on-sale date, so they can't simply stand in.

## Constraints

- **Rate limits:** Metron: 20 requests a minute, 5,000 a day, shared by every Kometa job. Comic
  Vine: 200 per resource per hour, non-commercial use only. GCD: 30 an hour anonymous, 2,000 a day
  with a free account. VerseDB: 300 an hour free. Details: `docs/research/comic-data-sources.md`.
- **Terms and licences:** Metron and GCD are CC BY-SA; Comic Vine and VerseDB are non-commercial
  and forbid mass download; LOCG has no public API.
- **No owner chores:** no passes to paste, no setup screens, no explainer copy on screen. Gaps fill
  themselves or stay quietly empty.
- **Owner decisions stand:** "no match" answers are never re-opened.

## Where each field could come from

A ladder per field: the first source that has the value wins, and the record remembers which
source gave which field.

| Field | Ladder (first that has it) | Notes |
|---|---|---|
| Credits | Metron → Comic Vine → GCD → LOCG list data → the file's tags | GCD is strongest for the back catalogue; Comic Vine for 1990s onwards. Person ids come from Metron by name (built today) or via the cv_id/gcd_id Metron stores. |
| Description | Metron → Comic Vine → publisher solicitation → GCD synopsis | Publisher pages (Image, Marvel, DC) carry the solicitation text for current issues. |
| On-sale date | Metron → Comic Vine store_date → GCD on_sale_date → LOCG | Never derive one from the cover date; show no date rather than a wrong one. |
| Page count | The file itself (owned issues) → Metron → GCD | Kometa already counts the pages of every file it owns. Today's 14% gap on owned issues needs no outside source. |
| Cover | Metron → LOCG list → Comic Vine → GCD → the file's first page | Already mostly there. |
| Series description, genres | Metron → Comic Vine volume → GCD → Wikipedia lead | GCD genres are already being filled. |
| Community rating | LOCG (when reachable) → ComicBookRoundup | Hardcover and Open Library for collected editions, by ISBN. |

**Joining sources without guessing.** Metron stores `cv_id` and `gcd_id` for issues and series,
so a Metron-linked issue can be asked of Comic Vine and GCD by id, with no title matching. VerseDB
cross-references about a million issues across Comic Vine, GCD and Metron. UPC (single issues) and
ISBN (collected editions) join the rest.

**Owned issues have a source no catalogue can match: the file.** Page count and cover are already
there. The credits page inside most comics (usually page 2 or the last page) lists the creative
team. Reading it (on-device text recognition, or a vision model) would fill credits for owned
issues no catalogue covers. This is the only route for the 202 no-catalogue issues.

## Approaches to weigh

1. **Merge per field.** Keep each field's source and age, fill the gaps from the next source on the
   ladder, and re-ask on a schedule set per field. This is the core change; the rest feeds it.
2. **Add Comic Vine as the second source, joined by cv_id.** It covers the 1990s onwards well, and
   the id join means no guessing. It's slow (rate limit), so it suits a background fill that only
   asks about gaps.
3. **GCD for the back catalogue and dates.** Its free-account limit (2,000 a day) is enough for the
   2,408 issues with no on-sale date and the pre-2000 credit gaps. The data dumps are an option for
   a one-off bulk fill.
4. **The file as a source for owned issues.** Page counts at once; credits-page reading as an
   experiment on the no-catalogue shelf first.
5. **Give back to Metron.** It's community-edited. A list of what Kometa had to find elsewhere
   could go upstream, so the gaps close for every Metron user. That's the one approach that
   shrinks the problem at its source.
6. **Leave the gaps quiet.** Whatever stays missing shows as nothing: no placeholder, no "unknown"
   label. That's how the issue sheet and the "no cover, no suggestion" rule already behave.

## Open questions

- Is Comic Vine's non-commercial licence fine for a personal library? (It reads that way.)
- How far back does the owner care about? The 1990-and-earlier shelf is 51 issues.
- Credits-page reading: on-device text recognition (free, rougher), or a vision model (better,
  costs per page, sends page images off the box)?
- Contributing to Metron: the owner's account, or a list for the owner to review and submit?
