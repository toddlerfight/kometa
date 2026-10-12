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
| GCD (optional) | 30/h anonymous, 2,000/day with a free account | Only with an account; back catalogue Metron lacks |
| Image hosts (S3, Metron static, CV uploads) | No API limit | Polite: one at a time, 0.5 s gap (the existing trickle) |

**Every source has a Settings card** (credentials, autosave, verified dot, on/off toggle, disconnect): ComicVine and LOCG already do, and GCD has one since de8648f. **Metron has none**: its login lives only in the config table, with no way to see or change it from the app. Add it before the Metron worker (1d). A new source isn't done until its card is.

**Parallel.** Each source has its own budget, so each gets its own worker and they run at the same time. They write into the same record under one merge policy (field by field: the best source wins, a weaker one fills gaps, a stopgap never overwrites a catalogue). The only dependency is that ComicVine works on ids as Metron supplies them.

**1. Harvest (parallel workers, one per source)**
- 1a. Prefer the largest URL (`large` over `thumb`) for mains and variants; re-fetch stored images whose best URL changed (includes the 1,933 LOCG 170px covers). Image hosts only.
- 1b. The worker framework: a per-source worker, a per-item journal (pending, done, failed and why), resumable, the merge policy, yield to the reader.
- 1c. The progress view (per source: done, queued, failed, today's budget left) and **the Settings badge** (red dot, no copy) when LOCG work is waiting with no working pass.
- 1d. **Metron worker**: full issue detail for every owned issue with a Metron id (4,853), keeping what `issue_detail` currently drops (cv_id, gcd_id, UPC/ISBN, page count, price, characters, teams, universes, arcs, reprints). Then match the ~2,300 owned issues with no Metron id (series first). That's about 7k requests ≈ 2 days within the budget.
- 1e. **LOCG worker**: link the 360 untried series, then the variant list and details for every owned issue (404 of ~7k done), then series and trade data. Fix the duplicated combined-role credits. About 8–10k requests ≈ 1.5–2 days at 240/h; stops when the pass is refused (badge on).
- 1f. **ComicVine worker**: as cv ids arrive, the original main cover (kept if bigger) and confidently matched variant images; issues with no cv id searched by series and number, confident only. About 40 h.
- 1g. **GCD worker (optional, needs a free account)**: credits and covers for back-catalogue issues Metron doesn't have.
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
- **Community ratings** (LOCG, ComicBookRoundup, Hardcover, Open Library): stored as a **dated snapshot**, refreshed on the +7/+30/+90 schedule (decided 2026-10-12).

**K. Retire Komga (decided 2026-10-12; independent of the harvest, do it now)**
- K1. Read history: apply the one-time import (85 match; the write is stale-rejecting, so newer kometa progress wins). Recover the 10 unmatched (Komga's old pre-rename paths) by series and issue number instead of path.
- K2. Reading lists and collections: re-run the import for anything added since (15 imported).
- K3. Metadata: one last full harvest (fills only where no catalogue has answered; 241 records today).
- K4. Disconnect everywhere: sync's series linking and folder fallback, arcs, reading-list import, last-resort covers, harvest, history import, the Settings card and config keys, `komga_series_id` use. Tests updated.
- K5. Stop the Komga container (with the user's OK; check first that nothing else reads from it, e.g. a reading app on the tailnet URL). Remove the container and its config a few days later, after a backup.

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

**4. On arrival**
- 4a. ComicInfo goes into finalize's existing rebuild.
- 4b. Enrich on arrival (every source, covers beside the comic).
- 4c. Re-enrich at +7/+30/+90 days; retag only when the XML hash changes.

**5. Read local first (last)**
- 5a. DB → file ComicInfo → online.
- 5b. Online results go to the DB; the retag is queued until the book is closed.
- 5c. Every online fallback is logged and listed as a pipeline miss.

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
