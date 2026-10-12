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

### Reading order (when a comic is opened or shown)

1. **The DB record**: local, milliseconds. Always first; it's the index.
2. **The file's ComicInfo**: only when the DB has no row (a rebuilt DB, or a file dropped in by hand). Harvest it into the DB.
3. **Online** (Metron → LOCG → CV): write the DB, then queue a retag. Never rewrite on the spot: a file swapped while it's being read changes under the reader. Retag once the book is closed.

An online fetch at view time means the pipeline missed something. Log every one and surface them as a list, so the cause gets fixed for the whole class, not just one issue at a time.

**Where the library stood on 2026-10-12** (7,492 books): 2,594 have catalogue records (Metron/LOCG); **4,583 only have stopgap data** (the file's ComicInfo or Komga); 133 are tracked and numbered with no record row; 141 are tracked with no parsed issue number (trades, one-shots, specials); 36 are on the shelf with no series link; 5 are true catalogue misses. The catalogue record was built on 10-09/10, while LOCG was walled (since 10-08) and Metron was rate-capped or banned, so most of the library predates a working fill. Step 1 should confirm the cause for each group before trusting that explanation.

## Open questions

- Community stats (LOCG pull counts, rating): store as a snapshot, or skip because they change over time?
- Phases 3 and 4 both touch every series folder. Run them as one pass?
