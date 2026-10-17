# Review request: the credits ladder, as built (2026-10-17)

You reviewed the design (`docs/credits-sourcing-review-2026-10-17.md`). It's now built with its
switch **off** and live on the server, and a dry run has been taken. Please review the
**implementation and the dry run**, and say whether it's ready to switch on.

## Read first

- Design: `docs/credits-sourcing-design-2026-10-17.md`
- Your review of it: `docs/credits-sourcing-review-2026-10-17.md`
- Dry run (live DB, nothing written): `docs/credits-dry-run-2026-10-17.md`
- The code: commit `4214715` ("Credits ladder (switch off) and Illustrator counted as an artist"):
  - `kometa/record.py`: `CREDIT_RANK`, `credits_source`, `_credits_to_keep` (called from
    `write_issue`), `_cv_credits`, `_ask_gcd`, `_ask_comicvine`, `fill_credits`, `credits_wanted`,
    `credits_ladder_on`, `credits_tick`, and the hooks in `details`, `_safe_refresh`, `trickle`
  - `kometa/related.py`: Illustrator added to `ROLE_GROUPS` and `_ROLE_WEIGHT`
  - `tests/test_credits_ladder.py` (8 tests)
- Related fix the same day: `f316a38` (`locg_client._clean_json_text`: JSON returned through
  FlareSolverr is now HTML-unescaped; before it, every LOCG list read through FlareSolverr
  parsed to zero rows).

House rules: INSTRUCTIONS.md, "Working with a second agent". In particular:
- **Read-only.** Don't commit code, don't deploy, and don't change config (`credits_ladder` stays
  off).
- **Never edit an existing test.**
- **Any call to GCD, Comic Vine or Metron goes through its module** (`gcd_client` with its budget,
  `sources.comicvine()`, `metron_client`). Keep calls few.
- **Say where every number comes from**: the live DB, a dry run, or reading the code.

## What to check

1. **The invariants in `_credits_to_keep` and `fill_credits`**, against the design:
   - one catalogue's list, never blended;
   - Metron > GCD > Comic Vine > LOCG > file tags;
   - a higher rung replaces a lower one, and a lower one never replaces a higher one;
   - an empty answer never clears a list.

   Look for any write path that gets around them. Who else writes `credits_json` directly?
2. **Switch off must mean no behaviour change** apart from (a) the protection in `write_issue` and
   (b) the Illustrator grouping. Confirm that every hook is gated on `credits_ladder_on`.
3. **Provenance.** `field_sources_json` should be absent for untouched rows, record GCD or Comic Vine
   when they fill a row, and survive Metron's later rewrites. Check `credits_tried` and the
   one-week retry.
4. **`credits_wanted`.** It filters `extra_json` with `LIKE '%"gcd_id": %'`. Is that sound for how
   `extra_json` is written (`json.dumps`, default separators)? Does it ever pick rows the ladder
   can't improve, and so ask again uselessly?
5. **Budgets.** GCD goes through `gcd_client.budget()`. Comic Vine is called with `cv._get` directly.
   Is there a Comic Vine limiter the ladder should use (200 an hour per resource, shared with the
   arc lookups)? At 6 issues a tick every 10 minutes, what's the worst case per hour?
6. **The mappings.**
   - GCD: covers are kept apart by story type, editors are dropped, roles use Metron's names.
   - Comic Vine: comma-joined roles are split; editor and production are dropped.

   Is anything lost or mis-mapped that the issue sheet or the ComicInfo writer would show wrongly?
7. **Downstream effects.**
   - Names get Metron person ids through `related.with_creator_ids` (as text, then by name).
   - The retag picks up changed credits (`retag.requeue_changed`).
   - The design asks that the ComicInfo `Notes` line name the credits' source. That is **not built
     yet**; say whether it should block switching on.
8. **Tests.** Are the 8 tests enough? Name any case that's missing.

## Decisions to give

From the dry run:

1. **A short GCD list against a fuller Comic Vine one.** Deadpool Corps: GCD has 2, Comic Vine 25;
   Hit-Monkey #1: GCD has 1, Comic Vine 5. Keep the strict rank? Or prefer Comic Vine when GCD's list
   has no writer or no artist? (That isn't a blend: the whole list still comes from one source.)
2. **Long lists from anthologies and collected editions.** Judge Dredd: The Complete Case Files #48
   gets 52 credit lines from GCD. Show them all, or trim or group them? If so, where: storage or
   display?
3. **Ready to switch on?** If not, what must change first?

Not part of this review: Comic Vine by series and number (no `cv_id` yet; a follow-up), the LOCG
real-browser test (running separately), and the 237 files no catalogue matches.

## Your answer

Write it to `docs/credits-ladder-impl-review-2026-10-17.md`:
- **Verdict:** ready / ready after named changes / not ready.
- **Findings:** each with `file:line`, severity (bug / risk / nit), what's wrong, and how you
  confirmed it (read the code, ran a test, queried the DB).
- **Answers to the three decisions.**
