# Kometa Recommendations: Multi-Signal Design, Round 1

**Date:** 2026-10-16
**Status:** Design discussion, nothing built yet.

This document has three parts:

1. The problem and the first proposed solution.
2. A design partner's full response, unedited.
3. The engineer's verification of that response and planned pushback for round 2.

---

## Part 1: The problem and the first proposal

### Problem

Kometa is a self-hosted comic library: about 840 series, three readers. Its "Related" recommendations are almost entirely "shares a creator". A shared writer scores 3, a shared artist 2, an inker 1, and shared colorists and letterers are ignored. A shared story arc or a reading list adds a little more. If nothing matches, it falls back to "same publisher, same title stem".

That makes the results narrow and sometimes empty:

- Saga returns five Brian K. Vaughan books and nothing else.
- Monstress returns nothing at all, because its only shared creator is a letterer.
- Batman (1940) returns Knightfall, then The Infinity Gauntlet through a shared inker.

There's no sense of genre, tone or theme.

The data is there but unused:

- Genres sit on only 190 series. They're coarse, copied once from an old server, and nothing refreshes them. "Superhero" is on 83 of them, including Saga.
- The Metron catalogue, which 600 series link to, has a genre list per series (Monstress is "Fantasy") and a list of associated series. It costs one API call per series, against a budget of 5,000 calls a day shared with everything else.
- Per-issue characters, teams and universes are already stored for about 460 series. Batman, the X-Men and the Fantastic Four dominate.
- Also stored: issue descriptions, community scores for about 280 series, and each reader's reading history, reading lists and pull list.

The owner wants recommendations that overlap on several things at once (writer, artist, genre, and more), each with a short honest "why" on the card.

### First proposal

Harvest genres from Metron for the 600 linked series. Score shared genres by rarity, so a shared "superhero" is worth almost nothing and a shared "post-apocalyptic" is worth a lot. Keep that below creators, so creators still lead and genre mainly fills empty rows like Monstress. Leave characters and teams out for now.

Its weaknesses: Metron's genres are coarse, rarity weighting on 840 series is noisy, nothing captures tone or theme finer than genre, and there's no per-reader taste.

---

## Part 2: Design partner's response (unedited)

Before the design, there are three things in the code you need to know. The first one breaks your harvest as currently planned.

### 0. Blocking findings

1. **The refill would delete the Komga genres.** `fill_series` writes with `INSERT OR REPLACE INTO series_record` (`kometa/record.py:366`) and doesn't list `genres_json`/`tags_json` (added at lines 76–79). SQLite's REPLACE deletes the old row and inserts a new one, so those two columns come back NULL. Any refill through that path erases the 190 series' only fine genre data. Komga is retired, and the retag (`comicinfo.py:130`) rebuilds `<Genre>` from that same column, so the copy in the files can go too. **Fix:** copy the data into a new table first, and switch to `ON CONFLICT DO UPDATE` on the columns `fill_series` owns.
2. **`series_detail` drops more than genres.** `metron_client.py:374–389` also drops `desc`, `associated`, `imprint` and `alt_names`. `fill_series` reads `d.get("desc")` (record.py:363), so `series_record.desc` is NULL on every Metron row.
3. **The genre call is already made, once.** `record.trickle` calls `fill_series` only for series that have no row (record.py:426) and never refreshes. So "harvest genres" is a parse fix plus a forced refill. It isn't a new pipeline.

### 1. Critique of the first proposal

- **Genre-only rows are ties, not rankings.** With Metron's 1–3 coarse genres per series, every Fantasy series on the shelf ties for Monstress. IDF ranks genres, not the candidates that share one. Twelve cards in dict order, all saying "Fantasy", is filler.
- **It throws away finer data you already have:** the Komga genres, GCD story genres (GCD is already integrated and has its own budget), and the descriptions.
- **Leaving out universes is the biggest miss.** Absolute, Invincible and Ultimate are rare, near-perfect and cost no calls.
- **The real defect is unbounded additive scoring.** The Infinity Gauntlet inker link, five ties at 3.0, and the letterer band-aid all come from it. Adding genre to the same sum repeats the problem: three genre overlaps would roughly equal one writer.
- **There's no diversity rule.** Genre would only add fantasy cards *below* Saga's five Brian K. Vaughan books.
- **IDF at df=1–2 is unstable.** One wrong Komga tag (Saga as "superhero" is the proof) becomes the strongest link a series has.
- **No evaluation plan**, so the weights would be guesses.

### 2. Design

**Feature store.** A new table `series_features(tracked_series_id, channel, key, label, weight, source)`, built locally after `fill_signals`/`fill_series` and nightly. An in-memory index (postings, df, stop-lists) is kept warm the way `_list_neighbours` is, keyed on a fingerprint of the tables it reads. `related()` then only walks postings for the seed's ~60 features, about 2–5 ms.

**Channels** (all inputs are local):

| Channel | Series profile | Pair raw score | cap / τ | Can it create a card? | Card text |
|---|---|---|---|---|---|
| Creators | `series_signals` | Σ min(role_a, role_b) · clamp(idf_p / median idf, 0.5, 1.5) | 6 / 3 | Only via a shared credit with role weight ≥2.0. A shared inker adds score but never creates a card. | "Writer: Brian K. Vaughan" |
| Arcs | `arcs_json` | Σ 3 · arc idf factor (same clamp) | 4 / 3 | yes | "Arc: Knightfall" |
| Lists | `_list_neighbours` | today's 1.5–3 | 3.5 / 2 | yes | "Reading list: {name}" |
| Linked on Metron | `associated` | 1 | 3 flat | yes | "Linked on Metron" |
| Universe | universes on ≥25% of the run's records | idf · min(presence) | 2.5 / 1.5 | yes | "Absolute Universe" |
| Cast | top 5 characters + teams, each on ≥25% of records and ≥2 issues | max over shared features (idf · min presence) + 0.25 · the rest | 2 / 1.5 | if contribution ≥0.6 | "Stars Wolverine" / "Team: X-Men" |
| Genre & theme | `series_tags` with a trust weight per source: Metron 1.0, GCD 0.8, theme word list 0.7, Komga 0.6. Themes count ×1.5 vs genres. | IDF-weighted cosine | 2 / 0.5 | if contribution ≥0.6 and a shared tag isn't stop-listed | "Theme: post-apocalyptic" |
| Community (fixed version) | `cbr_people_scores` | see below | 2 / 0.4 | yes | "Liked by 14 Saga readers" |
| Franchise stem | always on (today it only runs when nothing else scored) | 1 | 1.5 flat | yes | "Franchise: Aliens" |
| Tie-breakers | imprint, age rating, era within 10 years, format, toprated score | ± values, total clamped to ±0.8 | — | never | never shown |

**Score:** `score = Σ cap_c · (1 − e^(−raw_c / τ_c)) + tie-breakers`. Only candidates with at least one channel that can create a card are scored. Each channel saturates, so no single one can run away. Ties break on score, then quality, then title.

**Theme word list.** About 50 reviewed regex entries (witches, kaiju, heist, cosmic horror and so on) run over series and issue descriptions. A theme is assigned only with ≥2 hits in ≥2 texts. Komga labels map to a canonical set. "Superhero" counts only if two sources agree.

**Keeping Batman, X-Men and superhero from swamping everything:**

- A feature on more than 15% of a channel's series is stop-listed. It scores 0 and can't be a reason, which kills "superhero".
- Cast counts only lead characters, and scores by max rather than sum, so Batman, Gordon and Alfred count once.
- Hub damping: divide a candidate's score by `1 + 0.5 · max(0, log2(in-degree / median in-degree))`.
- Diversity: at most 3 of the top 6 cards, and 5 of 12, can share the same primary reason.
- A seed's own family members (`family.py`) are excluded, so Absolute Batman's row isn't its annuals.

**Community blend, fixed** (based on the per-person CBR scores harvest; see question 3 below):

- Defect 1, low-support winners: use adjusted cosine on centred scores, multiply by n/(n+10), and require n ≥ 5 co-raters. Infinity Gauntlet at k=3 is out.
- Defect 2, Hellboy in Hell everywhere: centred cosine penalises things everyone loves, and hub damping on top handles what's left.
- Defects 3 and 4, dropped lists and the letterer regression: the blend becomes one channel inside the same combiner rather than a separate scorer. Regression tests pin both.
- Neighbours are precomputed in the background, so it costs nothing at request time.

**Card text.** One reason per channel, ordered by contribution, at most 3. A reason is shown only if its channel gave ≥25% of the card's score. The card line is truncated, so `why[0]` has to be the strongest reason, and that's what drove the score. Creators get a role prefix (today the card shows a bare name). Add an optional `channels` object for debugging. The apps' `why` list is unchanged.

**Reading history.** The series page stays the same for everyone, except a −0.3 tie-breaker for a run you've finished. On Deck keeps its anchors and adds a multiplier: `1 + 0.3 · tanh(Σ affinity)`, where affinity is how often a reader finishes series with that feature, shrunk by n/(n+5). The two light readers stay close to neutral. This is phase 3.

### 3. Harvest plan

| Step | What | Calls | Timing |
|---|---|---|---|
| 0 | Copy the Komga genres into `series_tags`; switch `fill_series` to UPSERT | 0 | — |
| 1 | `series_detail` returns genres, associated, imprint, desc, alt_names (and gcd_id if present). Refill Metron-linked rows marked `facts_v < 2`, 4 per 10-minute tick, inside `harvesting()` | 600 | ~576/day, about 12% of the 5,000; done in ~25 h; the existing harvest floor still applies |
| 1b | A series page opened with a weak row (<4 cards) triggers one `fill_series` call | 1 per page | — |
| 2 | Characters, teams, universes from `issue_record.extra_json` | 0 | already local |
| 3 | Theme word list over the descriptions | 0 | after step 1 lands |
| 4 (optional, owner's call) | One GCD issue fetch per run via its `gcd_id` | ≤600 | ~150/day on GCD's own 1,800/day cap, no Metron calls |

Refill order: empty rows first (Monstress), then any reader's On Deck anchors, then the pull list, then the rest by number of owned issues. Series marked "no run" are never matched or asked. After the refill, the 90-day TTL costs about 7 calls a day.

### 4. Cold start (no credits or genres yet)

- **Instantly, from local data:** reading lists, arcs, franchise stem, the files' own ComicInfo cast, and title cast (title words matched against the shelf's cast vocabulary, at presence 0.6).
- **One call:** opening the page fetches genres and `associated`, and the existing pending flag redraws the row.
- **Then:** show whatever cards qualify. Never fill with imprint- or publisher-only cards.
- **LOCG-only runs:** theme word list over the LOCG descriptions, plus Komga or GCD genres where present.

### 5. Evaluation

Build `kometa/related_eval.py`. It runs on a DB copy with no network and prints old vs new top 8 with each channel's contribution. The owner marks the combined list good / ok / bad, and that gives precision@5.

Shelf-wide gates:

- Coverage: share of series with ≥4 cards.
- Hubness: no series appears in more than 3% of top-5 rows.
- No more than 3 of a top 5 share the same primary reason.
- Zero cards created only by an inker or letterer.
- List recall: with the list channel switched off, how many list neighbours other channels still find.
- p95 latency under 50 ms warm.

Seeds and what a good top 5 looks like ("if shelved" means the partner assumes the owner has it):

| Seed | Good top 5 |
|---|---|
| Monstress | ≥4 cards; Saga in the top 3 (theme + community); no superhero; no letterer |
| Saga | At most 3 BKV books, ordered by genre fit (Paper Girls / Y: The Last Man before Ex Machina, if shelved); Monstress present |
| Batman (1940) | Knightfall, then arc/list companions; Infinity Gauntlet gone; ≤3 cards whose only reason is "Batman" |
| Absolute Batman | Absolute Universe siblings 1–4, then Scott Snyder; none of its own specials |
| Invincible | Invincible Universe spin-offs, then Kirkman's other runs |
| Fantastic Four | Other FF volumes via a single "Team: Fantastic Four" reason, not three stacked cast reasons |
| Aliens | Franchise siblings, Predator/AvP, then SF-horror theme |
| Department of Truth | James Tynion IV's other runs, then conspiracy/horror theme; no Scott Snyder (he was a guest) |

Puzzlebox is the cold-start test: honest cards or none, and ≥4 cards within one tick of its credits landing.

### 6. What to cut or defer, and what to build first

- **Cut:** inker-only links, "superhero" as a reason, the ×15 scale, and the rule that franchise only fires when nothing else scored.
- **Defer:** personalisation (phase 3), description TF-IDF beyond the word list, genre-based recommendations off the shelf (there's no catalogue genre index), community until it passes its tests, GCD until the owner approves it.
- **Build first:** step 0+1, about half a day. It protects the Komga data and starts the 25-hour clock running. While that harvest runs, build the eval harness and the combiner with universes and cast, which need no calls.

### 7. Expected pushback, and answers

- **"Too many channels; ship genre first."** The combiner has to change before genre goes in, or genre makes today's ties worse. Universes and cast are free once the combiner exists.
- **"GCD is another source."** It's optional, already integrated, free and on its own budget. The design works without it.
- **"The theme word list is brittle."** It's about 50 reviewed entries. Gate it: if precision on a 50-series audit is under 80%, ship without it.
- **"Batman walls."** Lead-cast only, max not sum, cap 2, stop-list and the diversity cap handle this, and arcs and lists outrank cast anyway.
- **"The 50 ms budget."** Postings are precomputed and community neighbours are cached, so a request is just a postings walk.

### Numbers requested from the live DB

1. `series_record` row count by `source`, and how many have a non-empty `desc` (to confirm finding 2).
2. How many series each of Batman, Wolverine, X-Men and Fantastic Four is lead cast in, under the ≥25%-of-records rule. Also whether any default universe (Earth-616 or similar) is tagged.
3. Counts of `cbr_people_scores` rows and distinct people, how many shelf runs have ≥20 raters, and what data the drafted blend actually reads. It isn't in this repo, and `~/code/kometa-recommend` is only the old LOCG scraper.
4. How many Metron-linked runs have an `extra.gcd_id`, and how many GCD records already carry `extra.genre`.
5. Baseline: how many of the 834 series have 0 related cards today, and how many have fewer than 4.

### Critical files

- `kometa/related.py`
- `kometa/record.py`
- `kometa/metron_client.py`
- `kometa/cbr_people.py`
- `kometa/comicinfo.py`

---

## Part 3: Engineer's notes after round 1

### Verified in the code

- **Finding 1 is correct.** `fill_series` writes with `INSERT OR REPLACE` and doesn't list the genre columns, so a refill would wipe all 190 series' genres. Nothing is wiping them today: its only caller fills series that have no record yet.
- **Finding 2 is correct.** The Metron series parser drops the description, so it's empty on every Metron-backed record.

### Agreed

- Scores that keep adding up are the root problem. The saturating combiner fixes it.
- Universes and cast are the best free win.
- The evaluation harness comes before any weights get chosen.
- No refill runs until the genre data is protected.

### Pushback planned for round 2

- **Scale.** Nine channels plus a theme word list, hub damping and a diversity cap is a lot to land at once. Ship the combiner first with creators, arcs, lists, universe, cast and genre. Gate the theme word list and the community blend behind the evaluation.

### Next step

Pull the five numbers the partner asked for from the live database, and send them back with the pushback as round 2.
