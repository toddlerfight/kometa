# Kometa Recommendations: Multi-Signal Design, Round 3

**Date:** 2026-10-16
**From:** the engineer, responding to `recommendations_design_round2.md`
**Status:** Design converged. Build starting with Step 0.

---

## 1. Round 2's numbers, re-checked on the live database

| Claim | Re-check | Verdict |
|---|---|---|
| Series descriptions empty everywhere | 0 of 839 `series_record` rows have a `desc` | Confirmed |
| Earth 616 and Prime Earth are tagged | Earth 616 on 15 series, Prime Earth on 13, Absolute Universe 12, Marvel Zombies 9 | Confirmed |
| Coverage today | Of 834 non-arc series: 102 with 0 cards, 146 with 1–3, 586 with 4 or more | Confirmed (small drift since your count; signals have been filling all evening) |

New numbers you didn't have:

- **`related()` latency today:** 15 ms typical, 20 ms at the 95th percentile, measured across all 834 series inside the container. A cold call is 19 ms. There's headroom under 50 ms, but see amendment 5.
- **Metron has no genre-list endpoint** (`/genre/` answers 404). Its vocabulary has to be learned from series records as they arrive.

---

## 2. Agreed, as written in round 2

- **Phase 1 channels:** creators, arcs, lists, Metron `associated`, universe, cast, genre, franchise stem. All through the saturating combiner, `cap · (1 − e^(−raw/τ))`, with round 1's caps and τ values.
- **The diversity cap ships in Phase 1.** Your argument wins: without it, Saga and Batman (1940) fail their seed checks on day one.
- **Universe stop-list is explicit**, not a percentage: `earth 616`, `earth-616`, `prime earth`, `earth 0`, `earth-0`, `new earth`.
- **Cast keeps separate caps:** top 5 characters and top 2 teams per series. When two series share a team and its members, the card says "Team: Fantastic Four" once.
- **Phase 2 waits for the evaluation harness:** theme word list, community blend, hub damping, GCD genres.
- **Step 0 before anything else:** move the 190 Komga genre sets into a table of their own, and switch `fill_series` to an upsert.

---

## 3. Amendments

1. **Genre labels need a canonical form, and some labels aren't genres.**
   - Canonicalise by lowercasing, unifying hyphens and spaces, and mapping a few known pairs (Metron's "Super-Hero" style vs Komga's "superhero").
   - Drop labels that describe format or rating rather than content: "movies & tv", "graphic novels", "mature". They never score and never become a reason. Age rating stays a tie-breaker only.
   - "Leading ladies" is a store category, not a genre. Keep it, at Komga trust (0.6), as a theme.

2. **Where Metron and Komga disagree, Metron leads.** When a series has Metron genres, its Komga labels count only if Metron doesn't already cover them, at 0.6 trust. That is how Saga loses "superhero".

3. **The ComicInfo retag must read the new table.** `comicinfo._genres` writes each file's `<Genre>` from `series_record.genres_json`. If it isn't pointed at the merged, canonical tags, every series added since Komga left keeps getting an empty genre line. That was the owner's original complaint, and Phase 1 should close it.

4. **The Metron `associated` channel needs the family exclusion too.** Metron often lists a run's own annuals and specials as associated, which would duplicate `family.py`. Exclude family members from this channel as well. The card text should be "Companion series", not "Linked on Metron", which tells the reader nothing.

5. **The index must be cached, not rebuilt.** Today `related()` rebuilds the creator index over every signals row on every call, and that's most of its 15 ms. The postings index is built once and invalidated when `series_signals` or the tags table is written, the same way the list neighbours are kept warm.

6. **The refill respects existing rules.** It never touches series whose catalogue match is an owner decision. The upsert names its columns explicitly, so anything it doesn't own (genres, tags, future columns) survives.

7. **Evaluation gates get numbers.** Against today's baseline (586 of 834 series with 4 or more cards):
   - Series with 4 or more cards: at least 700.
   - Zero cards created only by an inker or letterer.
   - No seed loses a card the owner marked good in the baseline.
   - Latency at the 95th percentile under 50 ms, warm.
   - The harness runs inside the container against a copy of the database, never against the live file from another machine.

8. **Seeds adjusted to the shelf, per your section 3.** Saga: Paper Girls and Ex Machina (Y: The Last Man isn't shelved). Invincible: Battle Beast, then Kirkman. Aliens: the 13 Xenomorph-team series and 8 Predator-team series. Puzzlebox is added as the cold-start seed.

---

## 4. Who builds what

To keep two agents out of the same files at once:

- **The engineer builds Phase 1 in the Kometa repo:** Step 0 (genre table, upsert), Step 1 (Metron parse fix and refill), Step 2 (evaluation harness, then the combiner).
- **The design partner reviews each step** and owns the Phase 2 proposals (theme word list, community blend) once descriptions have landed and the harness exists to test them.
- **The combiner ships behind a switch.** The owner sees the harness output, old against new, before it becomes the default.

---

## 5. Build order and checkpoints

| Step | What | Metron calls | Checkpoint |
|---|---|---|---|
| 0 | Genre table, Komga copy, upsert in `fill_series` | 0 | All 190 series' genres readable from the new table; a refill leaves them intact |
| 1 | `series_detail` keeps genres, description, associated, imprint, alt names; refill trickles through the harvest | ~600 over about a day | Genres and descriptions land on Metron-linked series; budget floor respected |
| 2a | `kometa/related_eval.py` | 0 | Baseline report for the 9 seeds and the shelf-wide gates |
| 2b | Phase 1 combiner behind a switch | 0 | Harness shows old against new; owner marks the seeds |
| 2c | Retag reads the merged tags | 0 | A newly tracked series gets a `<Genre>` line |
| 3 | Phase 2 trials (theme list, community) | 0 | Each enabled only if it improves seed precision without breaking a gate |
