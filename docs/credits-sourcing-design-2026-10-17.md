# Credits for every comic: design for review (2026-10-17)

Status: **proposal, not built.** It follows `docs/data-consistency-problem-2026-10-17.md` and
answers `consistent_comic_data_plan.md` (the partner's plan) for one field: creator credits. Every
number below was measured on the live library on 2026-10-17.

## 1. What this solves

An issue's sheet should list its creators, and each name should open that person's page, whichever
catalogue happened to know the issue. Today Kometa takes credits from Metron alone. When Metron has
none (new issues, some older ones) the sheet shows a description and no credits, and Kometa doesn't
ask anyone else.

Out of scope here: dates, descriptions, page counts. The per-field design below is built so they can
follow, but this change only fills credits.

## 2. The gap today

10,472 tracked issues. **364 records have no credits at all:**

| Group | Records | What they are | Reachable by id? |
|---|---|---|---|
| File-tag stand-ins | 237 | Owned files no catalogue matches; their own tags have no credits | No (no catalogue id) |
| Metron, no credits | 90 | 60 not out yet, 15 undated, 9 older, 6 out in the last 60 days | 26 have a GCD or Comic Vine id |
| No record | 29 | Not filled yet | n/a |
| GCD, no credits | 8 | Older issues GCD itself hasn't credited | n/a |

**Incomplete lists:** 435 Metron records have no artist role and 56 no writer. Part of that is
naming: Metron sometimes says "Illustrator", which Kometa's role groups don't count as an artist (a
bug in its own right; see §8). Detective Comics #996 is a real case: writer, colorist and letterer,
no penciller.

**Names that wouldn't link:** the 485 LOCG-sourced records carried no Metron person ids. Fixed live
on 2026-10-17: a missing id now comes from the shelf by name, or from one Metron lookup per name.

## 3. Who has credits for a new issue (tested 2026-10-17)

| Issue | Out | Metron | Comic Vine | GCD | LOCG |
|---|---|---|---|---|---|
| 100 Bullets: The US of Anger #4 | 14 Oct | none | no cv_id yet | none | **Azzarello, Risso, Robins** |
| Batman: Bad Seeds – Gotham Central #2 | 14 Oct | none | no cv_id yet | **Cantwell, Phillips, Carey** | (not checked) |
| The Seasons #12 | 14 Oct | none | none | none | (not checked) |
| Monstress #64 | 16 Sep | none | **Liu, Takeda, Marzan** | (not checked) | (not checked) |

What this shows:

- Each source fills credits on its own timetable. LOCG has them before release (from the
  publisher's solicitation). GCD's volunteers enter new releases within days. Comic Vine and Metron
  take days to weeks.
- **Metron stores a GCD id even on brand-new issues** (all three 14 October issues have one); the
  Comic Vine id arrives later. So for new issues GCD is reachable by id, and Comic Vine isn't yet.
- LOCG's issue pages load in an ordinary browser, but FlareSolverr can no longer pass their check
  (two minutes made no difference). LOCG's lists still load, but don't carry credits.

## 4. How it works today (code)

- `record.write_issue` stores what one catalogue said as the whole row. `issue_meta.details_for`
  returns Metron's answer as soon as Metron has the issue, even with no credits; LOCG is asked only
  when Metron doesn't know the issue at all.
- Since 2026-10-17 a credit-less record for an issue out in the last 60 days is re-asked of Metron
  daily (`record.CREDITLESS_DAYS`).
- Since 2026-10-17 LOCG lists read through FlareSolverr parse again. Until then every one came back
  empty (the JSON arrived HTML-escaped). That touched new-issue detection for LOCG-only series and
  LOCG's collected-edition lists.

## 5. Proposal

### 5.1 Credits as their own field, with a source

- `issue_record` gains `field_sources_json`, e.g. `{"credits": {"source": "gcd", "at": "2026-10-17 09:00:00"}}`.
  No entry means "whoever wrote the row" (today's rows), so no migration is needed.
- **One source's list, never a blend.** The credits shown come whole from the highest-ranked source
  that has any. Merging two catalogues' lists person by person invites duplicates ("Jock" vs "Mark
  Simpson") and mixed role spellings.
- **Rank: Metron > GCD > Comic Vine > LOCG > the file's own tags.** Metron ranks first because it is
  the main catalogue and its people already carry ids. GCD ranks above Comic Vine because it is
  reachable by id on new issues and its credits are per story.
- A higher-ranked source with credits replaces a lower one: when Metron later fills #4, Metron's list
  wins. A lower-ranked source never overwrites a higher one, and an empty answer never overwrites a
  list.

### 5.2 The ladder, run in the background

When a record has no credits (or its credits came from a lower rung), ask the next rung:

1. **Metron**: as now, including the daily re-ask for recent issues.
2. **GCD by `gcd_id`** (from Metron's record). Use the comic stories only (skip covers, ads and text
   pieces), joining script / pencils / inks / colors / letters across stories, without the
   "(credited)" / "(signed as …)" notes (`gcd_client._people` already parses these), deduplicated.
   Budget: the configured account allows 1,800 calls a day; `gcd_client.budget()` already tracks it.
3. **Comic Vine by `cv_id`**, or, when there's no id yet, by the series' Comic Vine volume plus the
   issue number. `person_credits` comes with roles. Budget: 200 calls an hour, shared with the arc
   lookups.
4. **LOCG issue page**: only if the real-browser test (§6) passes. It's the one source with credits
   before release.
5. **The file's own tags**: only tags Kometa didn't write itself, from a known tagger. Already used
   as a stand-in for the 237.

When it runs:
- in the record trickle (which already picks up credit-less rows);
- in the daily re-ask for recent issues;
- on opening an issue, as a background thread exactly like today's refresh.

The issue sheet never waits on a rung.

**Expected yield now:** the 26 Metron rows with ids and most of the 6 recent ones. Then a handful each
week as new books land. It's a small number today, but it's the right rule for every future release.

### 5.3 What follows automatically

- **Links:** the names link through `related.with_creator_ids`, the name→Metron-id lookup built on
  2026-10-17. GCD and Comic Vine names arrive as text and are resolved the same way.
- **Files:** the file is retagged when its credits change (`retag.requeue_changed`). Its Notes line
  should name the credits' source ("credits from GCD").
- **Recommendations:** creator pages and More from rows read the same records, so they gain these
  people too.

### 5.4 Incomplete lists: a decision for the reviewer

435 records have a list with no artist. The options:

- **(a) Leave them.** One source per list, as above.
- **(b) Fill the missing roles only.** Take the artist roles from the next rung when the winning list
  has none. That's more complete, but it's a blended list, and needs care with name spellings.

Recommendation: (a) now, after fixing the "Illustrator" naming; then measure what's left before
deciding on (b).

## 6. Real-browser test for LOCG issue pages (parallel, no app code)

Question: does a real Chrome on the server (Playwright driving the installed Chrome, persistent
profile, one page at a time) load LOCG issue pages that FlareSolverr can't?

- Test set: #4 above, plus 20 issue pages across publishers, at one page every 10 s or slower.
- Pass: 19 of 20 load with credits, and the run doesn't make later requests worse (FlareSolverr's
  share keeps working).
- Watch: LOCG's terms; any change in the challenge LOCG shows after the run.
- If it passes, it becomes rung 4, and the only way to have credits on release day.

## 7. The 237 owned files with no catalogue (separate follow-up)

They have no catalogue id, so no credit source can be asked about them. Two routes, each its own
proposal:

- **Match them to a catalogue.** GCD's search covers small press and indie that Metron lacks; a
  match brings credits, dates and descriptions together.
- **Read the credits page in the file.** Text recognition on the page that lists the creative team.

This change doesn't touch them.

## 8. Corrections to the partner's plan

| Plan says | Finding |
|---|---|
| Credits: Metron → Comic Vine → GCD | For new issues GCD first: Metron stores the GCD id from day one, the Comic Vine id later |
| Date worker fills `issue_record.store_date` | The screens read `issue_status.store_date`; a fill must write there too or it shows nowhere |
| LOCG lists will fill ~1,500 of 2,408 missing dates in one call per series | Couldn't have worked before today's fix (every list parsed to zero). Lists come in pages of 140, so long runs need paging. Accuracy of LOCG's back-catalogue dates is untested. Measure before relying on it |
| Page counts from the file close a 14% gap | The reader, OPDS and lists already read the file's own page count; the record's field is barely shown. Low value |
| (not covered) | LOCG issue pages are walled to FlareSolverr; the uncatalogued shelf needs matching, not field merging |

Also found: Metron's "Illustrator" role isn't grouped with artists in `related.ROLE_GROUPS` or
`_ROLE_WEIGHT`, so those people fall into "Other credits" and never get a More from row. Fix with
this change.

## 9. Tests

- Ranking: a GCD list fills an empty Metron row; Metron's later list replaces it; Comic Vine never
  replaces GCD; an empty answer never clears a list.
- GCD parsing: comic stories only, notes stripped, roles mapped, duplicates folded (fixture from a
  real multi-story issue).
- Comic Vine by volume and number when `cv_id` is missing.
- Budgets: GCD's daily cap and Comic Vine's hourly cap stop the ladder without errors.
- Display: GCD and Comic Vine names come back linked (`with_creator_ids`).
- Retag: a credits change re-queues the file, and Notes names the source.

## 10. Rollout

1. **Dry run.** A report: for each credit-less record, what each rung would return, with a 20-issue
   side-by-side sheet for the owner to eyeball. No writes.
2. **Owner's OK.**
3. **Enable**, and watch the GCD and Comic Vine budgets for a week.

## 11. Questions for the reviewer

1. Rank: is GCD above Comic Vine right for all issues, or only new ones (by id availability)?
2. Incomplete lists: (a) or (b) in §5.4?
3. The real-browser test: go ahead in parallel?
4. The 237 uncatalogued files: matching to GCD first, or credits-page reading first?
