# Library enrichment — every variant, best resolution, facts in the files

Decided 2026-10-12. Apply to the whole library, not one issue.

## Goal

1. Every variant cover of every issue in the library, at the highest resolution any source offers, stored locally.
2. Everything useful LOCG knows about issues, series and trades, copied into Kometa's record so LOCG can go dark without loss.
3. The facts about each issue written into its own file (ComicInfo.xml), so the library carries its own data and the DB can be rebuilt from it.
4. Komga is on the way out. Nothing here has to suit Komga.

## Decisions (user, 2026-10-12)

- **All variants**: retailer and convention exclusives, virgins and reprints included.
- **Highest resolution available.** Store originals as downloaded; never re-encode or shrink.
- **ComicInfo is ours, not the uploader's.** Existing XML is patchy and unreliable, so we write a clean document from Kometa's record and drop the uploader's.
- **More sources are welcome.** The user will supply keys for ComicVine and anything else viable.

## What the DB stays responsible for

The DB remains the index the app reads from. Opening 7.5k archives over NFS at runtime isn't an option. It also stays the only home for personal data (progress, ratings, favourites, Want to read, lists, cover choices) and for data with no ComicInfo field (variant lists, series and trade data, catalogue ids beyond one). The files become a durable copy of the facts; the DB isn't removed.

## Sources and resolution (measured 2026-10-12, Midnight Spider-Man #1)

| Source | Main cover | Variants | Notes |
|---|---|---|---|
| The owned file, page 1 | Full scan resolution | — | Already stored as `issue/<sid>/<n>/file`. Only covers the cover that's in the file |
| ComicVine `original_url` | 854×1371 (new issue; older issues are often larger) | `associated_images`, but often empty (0 here) and not a dependable variant list | 200 requests per resource per hour; key already configured |
| Metron | 768×1166 | 600px | Variant list is the official solicit set (10 here) |
| LOCG (S3 `large-`) | 600×923 | 600px | **Most complete variant list (59 here).** `original-` and `xlarge-` return 403, so 600px is LOCG's ceiling. Images come from S3, outside Cloudflare; only the list page costs a LOCG request |
| GCD | w400 | — | Not worth it for images |
| VerseDB | — | Typed variant metadata (type, ratio, artist) | ToS forbids mass download and image caching; metadata only, if at all |

Resolution order per image: file > ComicVine original > Metron > LOCG. For a variant, the bigger version replaces the smaller one only when it's the same cover (same id, or a confident name/artist match).

## Phases

**Build order:** harvest → covers beside the comics → ComicInfo bulk pass → on-arrival → reading order last (checklist steps 1–5). The reading order (local first, online fallback, retag) only makes sense once the local data and the XML exist. Built earlier, every comic opened would go to the network and queue a file rewrite. Until then, the detail view keeps today's behaviour: it fetches online into the DB and never writes to files.

### Build checklist (each piece ships on its own; show the result before the next)

**Where it runs.** Every harvest and rewrite runs **inside the kometa app on ginbako, as scheduler workers**: in the background, independent of any Claude session, surviving restarts, with progress on the 1c view. Never as `docker exec` scripts or a second process: Metron's rule is one client, one throttle (INSTRUCTIONS.md), and the same goes for every source here. Watching progress is a background job too; no session waits on it.

**Rate limits: one limiter per source, shared with kometa's normal traffic (the UI and syncs always come first).**

| Source | Limit | Harvest share |
|---|---|---|
| Metron | burst 20/min, 5,000/day per user; obey `X-RateLimit-*` and `Retry-After`; 401/403/404 bursts are IP bans | Through `metron_client` only (3.5 s gap). The harvest stops for the day with **1,000 left**, keeping headroom for kometa's own syncs (the existing floor of 200 is for normal use) |
| LOCG | Cloudflare; kometa's pace is 3 s gap and 20 per 5 min; a refusal pauses 3 h | Through `locg_client` only; the UI's requests go first |
| ComicVine | 200 per resource per hour, plus velocity detection | Spread evenly (about 1 every 18 s per resource), never in bursts |
| GCD | 30/h anonymous, 2,000/day with the free account (set up 2026-10-12) | Back catalogue Metron lacks; about 1 every 45 s |
| PRH cover CDN | Terms forbid robots | About 1 every 2 s, at most a few hundred a day |
| Marvel.com / DC.com | Terms forbid automation | The slowest of all; only where no comparable image exists elsewhere |
| Fandom wikis (MediaWiki API) | Behind Cloudflare | 1 per second or slower |
| ComicBookRoundup | HTML, no API | About 1 page every 3 s |
| Hardcover / Open Library | Hardcover 5,000/day, 60/min | Well under; by ISBN only |
| Image hosts (S3, Metron static, CV uploads) | No API limit | Polite: one at a time, 0.5 s gap (the existing trickle) |

**Every source has a Settings card** (credentials, autosave, verified dot, on/off toggle, disconnect): ComicVine, GCD (de8648f) and Metron (8e5d2d1) do. LOCG has a card but no toggle yet. A new source isn't done until its card is.

**Parallel.** Each source has its own budget, so each gets its own worker and they run at the same time. They write into the same record under one merge policy (field by field: the best source wins, a weaker one fills gaps, a stopgap never overwrites a catalogue). The only dependency is that ComicVine works on ids as Metron supplies them.

**1. Harvest (parallel workers, one per source)**
- 1a. Prefer the largest URL (`large` over `thumb`) for mains and variants; re-fetch stored images whose best URL changed (includes the 1,933 LOCG 170px covers). Image hosts only.
- 1b. **Re-scoped 2026-10-12: the workers already exist** as scheduler trickles: `record.trickle` (Metron issue fill, 25 per 10 min, merges LOCG when open), `locg_sweep` (LOCG-only owned issues), `topup` (a LOCG queue that already has a `community` kind), `trades_trickle`/`tradefill`, `images_trickle`, `covers_trickle`, `metron_rematch`. The record is the journal (`fill_state`, `source`, `fetched_at`) and `write_issue`/`_fresh` are the merge policy. Live diagnosis (2026-10-12): the record trickle filled 996 from Metron + 522 from LOCG in 24 h, but `pending_issues` orders "no record yet" ahead of "owned". 1,956 record-less issues (all but one **unowned**) sit in front of **2,528 owned stopgap issues that have a Metron id**. 1b is therefore:
  - owned first in every trickle (pull list still leads);
  - the harvest's Metron share: background trickles stop for the day at **1,000 sustained remaining** (read from `X-RateLimit-Sustained-Remaining`); interactive requests keep the existing floor of 200;
  - more per tick within the burst limit (Metron's 3.5 s gap allows about 17/min shared by every job);
  - one progress summary across the trickles (feeds 1c).
  - The 1,066 owned issues with no catalogue id at all are a matching problem: second half of 1d.
- **1d status 2026-10-12:** Metron extras LIVE (0ef96a8: `issue_record.extra_json` + cv_issue_id; verified on Midnight Spider-Man #1). The 1,071 owned issues with no catalogue id split into:
  - **A. Matched runs whose issue list was never fetched** (linked after their last sync, then left to the weekly not-pulled cycle): 27 runs / 517 owned issues (Hellblazer, Thief of Thieves, The Woods…). Fixed by `harvest.issue_ids_tick` (025e459), 3 runs per 10 min.
  - **B. Unmatched runs:** ~137 runs. **Uncanny X-Men (404 issues) is a folder problem**: 544 files mixing real numbers ("Breaking Point, Part 1 #535 (2011)") with volume-relative ones ("Vol. 1 #016 (2009)"); kometa reads it as #1–405, and Metron splits the run (1722 "The Uncanny X-Men", 1981, 405 issues, beside the 1963 X-Men). Needs the user's decision on cleanup or renaming before any match. The rest are small or European presses (Tintin/Casterman, Tank Girl minis, Sin City episodes, Neverender): GCD (1g) and LOCG linking (1e).
- 1c. The progress view (per source: done, queued, failed, today's budget left) and **the Settings badge** (red dot, no copy) when LOCG work is waiting with no working pass.
- 1d. **Metron worker**: full issue detail for every owned issue with a Metron id (4,853), keeping what `issue_detail` currently drops (cv_id, gcd_id, UPC/ISBN, page count, price, characters, teams, universes, arcs, reprints). Then match the ~2,300 owned issues with no Metron id (series first). That's about 7k requests ≈ 2 days within the budget.
- 1e. **LOCG worker**: link the 360 untried series, then the variant list and details for every owned issue (404 of ~7k done), then series and trade data. Fix the duplicated combined-role credits. About 8–10k requests ≈ 1.5–2 days at 240/h; stops when the pass is refused (badge on).
- 1f. **ComicVine worker**: as cv ids arrive, the original main cover (kept if bigger) and confidently matched variant images; issues with no cv id searched by series and number, confident only. About 40 h.
- **Status 2026-10-12 (evening):** 1a–1f LIVE. 1e: dbb3cea (sweep covers every owned issue with a LOCG id; Metron's half of the variant merge read from the record; community snapshot in `issue_record.community_json`; one page fetch per issue). 1f: 14e5b03 (CV originals via the batch `issues/` filter, 1 request per 100 issues; key `issue/<sid>/<n>/cv`; display unchanged). Multi-run Metron links (0211ebd, `metron_extra_ids`): Uncanny X-Men (1722+1581), Fables (793+3396), Fantastic Four (26+1711+1713); relinked Invincible, Manifest Destiny, Curse Words, Fear Itself: The Worthy (all complete except Fables #150, absent on Metron). Split-run finder: `GET /api/harvest/split-candidates` (suggestions only). Cover check tool: `POST /api/series/{id}/verify-covers`.
- **To do with 1g:** LOCG linking currently only tries Metron-matched series (`link_locg_existing`); make it independent so Metron-less series (Tintin, Dune: House Atreides) can still get LOCG data.
- 1g. **GCD worker** (account set up 2026-10-12): credits and covers for back-catalogue issues Metron doesn't have.
  - **Built 2026-10-12 (not deployed):** `gcd_client` paces every request (one lock, 3 s gap, 1,800/day cap, a refusal pauses an hour). `harvest.gcd_link_tick` (3 runs per 10 min) links an owned, Metron-less run only when exactly one GCD series has the same name (a leading "The" allowed), is in English, has at least as many issues as the highest owned number, and fits the folder's `(YYYY)`; several such, or same-name runs that fail a test, become `ambiguous` with up to 8 candidates (`GET /api/harvest/gcd-candidates`; pick with `PATCH /api/series/{id}/locg {"gcd_id": N}`, 0 = not on GCD). `harvest.gcd_fill_tick` (12 requests per 10 min) writes owned issues with no Metron id and no catalogue record as source `gcd`; a stopgap never overwrites it, Metron refills it once the issue gains a Metron id. LOCG linking no longer needs a Metron match.
- 1h. The image downloader takes everything the workers queue, at the largest size.
- Not planned: VerseDB (its ToS forbids mass download and image caching, and kometa already parses variant type and ratio from names).
- 1i. **More sources (user, 2026-10-12: "use them if we can — polite rate limiting, not a production rate")**, each a worker with its own Settings card and limiter (research: docs/research/comic-sources-2026-10-12.md):
  - PRH cover CDN by UPC/ISBN (print-quality TIFF: 1987px mains, 1400px variants; Marvel/IDW/BOOM!/Dark Horse). Hash-check the 200 placeholder first; convert TIFF → JPEG on arrival (no 45 MB files). About 1 request every 2 s, at most a few hundred a day.
  - Marvel.com and DC.com images (2750–3253px). The terms forbid automation: the slowest of all, only for issues with no PRH or Fandom image of comparable size.
  - Marvel/DC Fandom wikis (MediaWiki API): the most complete variant galleries (printings, exclusives), up to about 2000px. 1 request per second or slower; CC BY-SA text.
  - ComicBookRoundup: critic and user scores per issue and series (HTML, about 1 page every 3 s).
  - Hardcover (5,000/day, 60/min; attribution) and Open Library: trade ratings and covers by ISBN.
  - Bedetheque/comicguide.de for European books: only if usable within their robots rules (Bedetheque disallows `/api/` and `/Couvertures/`). Otherwise skipped.
  - Image precedence: PRH → Marvel/DC → Fandom → Metron → ComicVine → LOCG → GCD. Match variants across sources by UPC first, then image hash.
  - **Built 2026-10-12 (not deployed): PRH, Open Library, ComicBookRoundup.** Each has its own module and one `polite.Limiter` (lock, gap, daily cap, a pause after 403/429/503), a Settings card (toggle `<source>_enabled` and today's request count; no login, so never a red dot), a 10-min scheduler tick that no-ops when off, and a harvest card row.
    - `prh.tick`: owned issues with a 17-digit Metron UPC or a 13-digit ISBN. The small `/cover/{code}` JPEG is hashed against the placeholder (fetched once with 17 zeros, md5 kept in config `prh_placeholder_md5`). Only a real cover gets the TIFF, which becomes a quality-92 full-size JPEG (CMYK through its ICC profile) at `issue/<sid>/<n>/prh`. Narrower than 900px is not kept. `extra.prh` = TIFF URL or None, so each code is asked once. 2 s gap, 300/day, 6 requests per tick.
    - `openlibrary.tick`: ISBNs from owned issues first, then trades. Edition → work ratings (average, count, 1–5 histogram) → original cover by id (not `-L`). Answers go to `openlibrary_record` by ISBN (Goodreads id included), the cover to `isbn/<isbn>/ol`, and the snapshot to `community_json.openlibrary` on any issue with that ISBN. Asked again after 90 days, without re-downloading a cover already held. 1 s gap, 2,000/day.
    - `cbr.tick`: one **series** page answers a whole run, because it lists every issue's critic and user score and both review counts. URLs tried: `{publisher}/{slug}-({year})`, then `{publisher}/{slug}`. A page counts as a match only when its heading equals the run's name and its start year is within 1 of ours. With no year, nothing is requested (`unsure`). Scores go to `community_json.cbr` and the series score to `cbr_series`. Linked runs are re-read after 30 days, misses after 90. 3 s gap, 500/day, 4 pages per tick.
    - **Built 2026-10-12 (not deployed): Fandom galleries (1i-d) and Hardcover (1i-f).**
      - `fandom_covers.tick`: owned issues in Marvel/DC runs, pull list first, 10 per tick, one `fandom_client.gallery` request per issue through the wikis' one throttle (1 s gap, 3,000/day shared with the collected-edition lookups). Page title from the run's name (Metron's `series_name` only on multi-run series), `series_volume` else 1, a leading "The" dropped first (the wikis don't redirect it). Metadata only: `extra.fandom` = `{page, at, covers:[{name, url, width, height}]}` or `{page, at, missing: true}`; not asked again within 30 days. Downloads behind `fandom_download` (default off): the image trickle queues gallery covers last at `issue/<sid>/<n>/fandom/<name>`, and each is dHashed against the issue's stored main/variants/CV/PRH covers first; a match within 10 bits is recorded as `upgrade_of: <key>`.
      - `hardcover.tick`: owned issues' ISBNs then trades', 25 ISBN-13s per GraphQL request (`editions(where: {isbn_13: {_in: …}}) { id isbn_13 book { id slug title rating ratings_count } }` — one top-level field = one request against Hardcover's limits), 4 requests per tick; 1.2 s gap, 4,000/day, rests when Hardcover's own daily count drops under 250. Answers in `hardcover_record` by ISBN, snapshot in `community_json.hardcover` (with the book URL for attribution); asked again after 90 days. Token in config `hardcover_token`, never echoed; the Settings card nags only when switched on with no token.
      - A Metron refresh no longer drops the keys other workers merge into `extra` (`prh`, `cv_original`, `fandom`, `marveldc`).
    - **Built 2026-10-12 (not deployed): Marvel.com covers (1i-e).** `marveldc.tick`, every 10 min but only inside `marveldc_hours` (default `23-7`, Brisbane, wraps midnight); toggle `marveldc_enabled` (absent = on). Owned Marvel issues with a Metron UPC, pull list first, then newest store date. Page found through Marvel's search JSON (`/v1/pagination/search_cards?query=<run> #<n>&content_type=comics`, issue cards headlined `Run (YYYY) #N`), then the issue page's `issueDetails` blob; accepted **only** when the page's UPC equals the record's (digits only, exact; 12-digit base vs 17-digit is not a match). `extra.marveldc` = `{site, page, at, night, matched, reason, main, variants, images}`; a match is never asked again, a miss after 60 days. Images (size suffix dropped) are dHashed against the issue's held covers: the same cover is kept only when bigger (`upgrade_of`, `old_size`), a new one is stored at `issue/<sid>/<n>/marveldc/<variant id>`, the main at `.../marveldc`. Limits: site 60 s gap, 400/day, at most 3 page requests a tick, an hour's rest on 403/429/503 (Retry-After honoured); CDN 5 s gap, 400/day, 4 images a tick. CloudFront 403s a bare `Kometa/1.0` UA; `Mozilla/5.0 (compatible; Kometa/1.0; …)` passes. **DC.com is not asked:** its issue pages carry no UPC and an empty variant list (two pages probed). Morning report: `GET /api/harvest/marveldc-report`.
    - `record.merge_community` writes another source's snapshot under its own key and leaves LOCG's fields and `community_at` alone. `set_community` (LOCG) keeps those keys, and `write_issue` no longer drops `community_json` on a details refresh.
    - **Built 2026-10-13 (not deployed): ComicBookRoundup's people — "readers like you" (1i-g, backend only).** `kometa/cbr_people.py`. Harvests individual critics' and readers' scores (keyed `critic:{slug}` / `user:{id}`, never the name). Pass A: each linked run's #1 plus its 3 most-reviewed issues (counts from `community_json.cbr`), seed runs first. Pass B: profiles (`/comic-books/reviewer/{slug}`, `/user/reviews/{id}`) of people who scored ≥ 2 linked runs, most overlap first; re-read after 30 days. Issue pages are read once; newly linked runs bring theirs. One page a tick, tick every minute (`harvest_cbr_people`), its own limiter (60 s gap, 480/day, separate from `cbr.limiter`), only inside `cbr_people_hours` (default `23-7`); off with `cbr_people_enabled` or `cbr_enabled` = `0`. Three unreadable pages in a row rest it 6 h. Tables `cbr_people`, `cbr_people_scores` (INSERT OR REPLACE on person + run + number), `cbr_people_pages`, `cbr_people_series`, `cbr_people_state`. `GET /api/discover/community?limit=20`: every run is a weighted seed (user, 2026-10-13: "you're reading X, here's what the community who liked X also likes") — On Deck now (Continue/Next) 3.0 > starred 1.5 > finished an issue 1.0 > owned 0.5, the strongest tier plus 1.0 per star away from 3★ (own rating, else its issues' mean; a 1★ run is a negative seed), read live so new ratings count at once; each person's scores centred on their own mean, weighted by seed-weighted agreement on ≥ 2 seeds (`n/(n+5)` shrink), critic and reader pools normalised and blended 50/50, candidates = runs not on disk (owned tracked runs and shelf folders, same name ± 1 year) with ≥ 3 positive supporters, `Σ w·c / (Σ|w| + 1)`. Cached an hour. Per run: `GET /api/discover/community/because?series_id=N&limit=12` (one seed: everyone who scored it above their own mean; same entry shape plus `seed`), and `GET /api/discover/community/because-ondeck` (up to 3 such rows for On Deck runs with ≥ 3 such people — the Deck's "Because you're reading X"). Progress: `GET /api/harvest/cbr-people-report`.
- **Community ratings** (LOCG, ComicBookRoundup, Hardcover, Open Library): stored as a **dated snapshot**, refreshed on the +7/+30/+90 schedule (decided 2026-10-12).

**K. Retire Komga (decided 2026-10-12; independent of the harvest, do it now)**
- K1. Read history: apply the one-time import (85 match; the write is stale-rejecting, so newer kometa progress wins). Recover the 10 unmatched (Komga's old pre-rename paths) by series and issue number instead of path.
- K2. Reading lists and collections: re-run the import for anything added since (15 imported).
- K3. Metadata: one last full harvest (fills only where no catalogue has answered; 241 records today).
- K4. Disconnect everywhere: sync's series linking and folder fallback, arcs, reading-list import, last-resort covers, harvest, history import, the Settings card and config keys, `komga_series_id` use. Tests updated.
- K5. Stop the Komga container (with the user's OK; check first that nothing else reads from it, e.g. a reading app on the tailnet URL). Remove the container and its config a few days later, after a backup.
- **Status 2026-10-12: K1–K5 done.** K1 history: 89 written, 6 kept ours, 10 renamed recovered. K2: only Fear Itself imported (a blind re-import would have overwritten the Hickman and Fables CBL lists). K3: 718 series, 253 issues. K4: a40c863. K5: Komga stopped with restart policy `no`; the Komic app on two tailnet devices was its last client and the user is moving to YondeB. `media-boot.sh` no longer restarts it (arr-stack d0fd157, deployed). **Still to do: remove the container and `~/docker/config/komga` after a few days, with the user's OK.**

**2. Covers beside the comics**
- 2a. The `.kometa/covers/<issue>/…` layout and stable file names.
- 2b. Copy the store to the NAS, verified.
- 2c. The NAS copy is the permanent one; `data/covers/` can be rebuilt from it.
- 2d. Move `data/covers/` to Catbus (`create_host_path: false`).
- 2e. New covers are written to both.

**3. ComicInfo write-back**
- 3a. The generator (record → clean XML, using Metron's characters, teams and GTIN), tested on real and messy records.
- 3b. The per-file journal in the DB.
- 3c. The rebuild worker (Voot II read, Catbus build, CBR → CBZ, page CRC verify).
- 3d. Upload as a temp file, read back, swap, carry the page cache over.
- 3e. A test batch of about 20 mixed files (CBZ, CBR, trade, injected covers), checked by the user in kometa and another reader.
- 3f. The full run (about 10 h of network time), yielding to the reader, resumable, on the 1c progress view.
- **Status 2026-10-12:** 3b–3d built in `kometa/retag.py`, tested on temporary copies only (never run against the library). Not yet: reading from the Voot II mirror (it reads the NAS file), parallel builds, a scheduler job.
- **Status 2026-10-13:** 3f batch mode built in `kometa/retag_batch.py`: it reads from a Catbus snapshot of the Voot II mirror (falling back to the NAS per file, counted), builds and verifies 4 at a time on Catbus, stops for the user's check, then swaps through `retag.swap`. The runbook is in INSTRUCTIONS.md. Not yet run.

**4. On arrival**
- 4a. ComicInfo goes into finalize's existing rebuild.
- 4b. Enrich on arrival (every source, covers beside the comic).
- 4c. Re-enrich at +7/+30/+90 days; retag only when the XML hash changes.

**5. Read local first (last)**
- 5a. DB → file ComicInfo → online.
- 5b. Online results go to the DB; the retag is queued until the book is closed.
- 5c. Every online fallback is logged and listed as a pipeline miss.
- **Status 2026-10-12:** 5a–5c built in `kometa/localfirst.py`, OFF (config `read_local_first`, absent = '0'; PATCH /api/config, no Settings UI). Turn on after the full ComicInfo rewrite. Our own tags are recognised by the Notes marker and stored as source `comicinfo-kometa` (catalogue-grade). A book counts as open when a page was served or progress was written in the last 20 min; its retag waits in `retag_deferred`, flushed every 5 min. Misses: `GET /api/harvest/misses`, plus `misses` in /api/harvest/progress.

The phase notes below are background; the checklist above is the plan.

### 1. LOCG harvest (do first; the browser pass is the scarce resource)
- For every issue on the shelf (then every tracked issue): fetch the LOCG variant list and details, store every variant's name and image URL in the record, and download each image at `large-`.
- Re-fetch the 1,933 stored LOCG covers that are 170px `medium-` thumbnails at `large-`.
- Series and trade pages: anything the record lacks.
- Paced at the existing trickle (20 requests per 5 minutes ≈ 240/hour): about 8–10k requests, about 1.5–2 days. Resumable, owned issues first, and it stops cleanly when the pass is refused.
- Fix the credit parser's duplicated combined roles ("Writer, Editor").

### 2. ComicVine high-resolution pass
- Link issues to ComicVine via Metron's `cv_id` (no search needed where Metron has it).
- Main cover from `original_url`; variants from `associated_images` only where they match a known variant.
- 200 requests per hour per resource: about 7.5k issues ≈ 40 hours.

### 3. Covers live with the library
- Originals go to a dot-prefixed folder beside the comics on the NAS (`<Series>/.kometa/covers/…`), so the nightly comics mirror backs them up.
- Kometa's `data/covers/` becomes a fast local copy that can be rebuilt, so it can move to Catbus.

### 4. ComicInfo write-back
- One clean ComicInfo.xml per file, generated from the record: Series, Number, Title, Volume, Summary, Year/Month/Day, Writer/Penciller/Inker/Colorist/Letterer/CoverArtist/Editor, Publisher, Imprint, StoryArc, Characters/Teams, Web, GTIN, PageCount, and catalogue ids in Notes (Metron tagger convention).
- A CBZ entry can't be edited in place, so each file is rewritten: temp file, verify, atomic swap (reuse `_rebuild_as_cbz` and its zero-entry guard). The 779 CBRs become CBZ in the same pass.
- After the first pass, retag a file only when its rendered XML changes (store a hash in the DB).
- **Knock-ons of the first pass:** about 455G of NAS writes, trickled and paused while you're reading. The Voot II mirror copies everything again and keeps the old copies for 30 days (about 900G, with 3.1T free). The reader page cache (keyed on size and mtime) rebuilds.

### Rewrite mechanics (the bulk pass)

The CPU work is trivial on ginbako (generating the XML, a STORED repack, unrar); the gigabit link is the limit (measured 2026-10-12: NFS read ~108 MB/s, write ~102 MB/s). So:
1. **Read from the Voot II mirror** when its size and mtime match the NAS file, falling back to the NAS. That takes the reads off the network.
2. **Build on Catbus** in batches of a few GB, several files in parallel.
3. **Verify before upload:** every page entry's CRC32 and size match the original, the page count matches, and ComicInfo parses.
4. **Upload as a temp file in the same folder**, read it back and check, then rename over the original (atomic). A failure leaves the original untouched.
5. **Keep a journal per file in the DB** (pending, built, verified, swapped, with the old size, mtime and hash), so the job can resume and be audited. Yield to the reader.
6. **Carry the page cache over:** pages are byte-identical, so rename each book's page-cache version dir to the new size/mtime version instead of re-rendering 21G.

Network cost: about 5 hours of writes plus about 5 hours of read-back. Afterwards the 02:00 backup re-copies the new files (about 5 hours) and keeps the originals in `deleted/` for 30 days (about 455G extra; Voot II has 3.1T free).

### On arrival (every new comic, from day one)

- **Finalize** (`acquisition._finalize_download` → `ensure_cbz` / `inject_covers`) already rebuilds CBRs and covered issues. Writing the ComicInfo goes into that same rebuild, so a new file is written once, not twice. A clean CBZ costs one rewrite, done before placement where possible.
- **Enrich the issue on arrival:** variants from every source, the CV original, the record filled, and covers written to the series' `.kometa/covers/`.
- **Re-enrich new releases on a schedule** (for example +7, +30 and +90 days), because exclusives land on LOCG for weeks after release and CV originals arrive late. Retag the file only when the rendered XML's hash changes.

### Reading order (when a comic is opened or shown) — BUILT LAST

1. **The DB record**: local, milliseconds. Always first; it's the index.
2. **The file's ComicInfo**: only when the DB has no row (a rebuilt DB, or a file dropped in by hand). Harvest it into the DB.
3. **Online** (Metron → LOCG → CV): write the DB, then queue a retag. Never rewrite on the spot: a file swapped while it's being read changes under the reader. Retag once the book is closed.

An online fetch at view time means the pipeline missed something. Log every one and surface them as a list, so the cause gets fixed for the whole class, not just one issue at a time.

**Where the library stood on 2026-10-12** (7,492 books): 2,594 have catalogue records (Metron/LOCG); **4,583 only have stopgap data** (the file's ComicInfo or Komga); 133 are tracked and numbered with no record row; 141 are tracked with no parsed issue number (trades, one-shots, specials); 36 are on the shelf with no series link; 5 are true catalogue misses. The catalogue record was built on 10-09/10, while LOCG was walled (since 10-08) and Metron was rate-capped or banned, so most of the library predates a working fill. Step 1 should confirm the cause for each group before trusting that explanation.

## Open questions

- Community stats (LOCG pull counts, rating): store as a snapshot, or skip because they change over time?
- Phases 3 and 4 both touch every series folder. Run them as one pass?
