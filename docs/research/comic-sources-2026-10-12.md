# Comic sources, round 2: high-res covers, variants, ratings

Research date: 2026-10-12. Extends `docs/research/comic-data-sources.md` (2026-10-10) and does not repeat it. Target: enrich the ~7,500-issue library with every variant cover at the best available resolution, full issue metadata, series/trade data and community ratings.

**Ground rules followed:** no calls to Metron or leagueofcomicgeeks.com; no API-auth probing; no bulk downloads or solvers. Each site got at most a handful of unauthenticated GETs (full log in §5). "Measured" means the image was fetched once and its pixel size read with `sips`. "Unverified" means it came from documentation, a search snippet or memory and was not confirmed.

**Baseline already known (2026-10-12):** LOCG 600px (S3 `large-`); Metron 768px for main covers and 600px for variants; ComicVine `original_url` 854px on a new issue; GCD API w400.

---

## 1. New image and metadata sources

### 1.1 Summary

| Source | Max cover res (measured) | Variants? | Metadata beyond the cover | Access | ToS | Verdict |
|---|---|---|---|---|---|---|
| **PRH Comics cover CDN** (`images.penguinrandomhouse.com/cover/tif/{UPC17 or ISBN13}`) | **1987×3056 TIFF @300dpi** (Marvel main cover, 45 MB); **1400×2172** (Marvel 1:25 variant, 18 MB) | Yes, one image per 17-digit UPC; titles name the cover letter, artist and ratio | prhcomics.com product titles: `X-ORCISTS #1 COVER C NICK BRADSHAW 1:25 VARIANT` | Anonymous, no key, keyed by UPC/ISBN | PRH terms forbid "data mining, robots, or similar data gathering and extraction tools" | **Viable** for PRH-distributed publishers. Best resolution found anywhere. |
| **Marvel.com CDN** (`cdn.marvel.com/u/prod/marvel/i/mg/{path}.jpg`, with the size suffix removed) | **2750×4175** (main), **1821×2800** (variant) | Yes. The issue page JSON lists every variant image, but titles are only "(Variant)" | FOC date, price, page count, UPC, creators with roles, age rating, release date | Anonymous HTML/JSON + CDN | Disney Terms of Use forbid extraction "using a robot, spider, script, or other automated means" | **Maybe**: technically excellent, ToS-forbidden. Personal-use risk is the user's call. |
| **DC.com CDN** (`static.dc.com/{yyyy-mm}/{file}.jpg?w=N`, Akamai Image Server) | **3253×5000** (native; `?w=4000` and `?w=10000` return the same size, so no upscaling) | Unclear: the issue page has a `variantcover.items` field, empty on the sampled issue | `datePublished`, `ratingCode`, series links | Anonymous | DC terms forbid "scrape, crawl", except for search engines | **Maybe**, same caveat as Marvel. The variant listing is unverified. |
| **Fandom wikis** (Marvel Database, DC Database; MediaWiki `api.php`) | **1885×2879** (ASM v6 #1 virgin variant); DC 1997px wide. Median about 1000px, min about 400px | **Yes, the most complete found.** ASM v6 #1: 55 files incl. retailer/con exclusives. Absolute Batman #1: 66 incl. Midtown, NYCC and 2nd–11th prints | Credits, appearances, story arcs, reprint links (wikitext) | Anonymous MediaWiki API worked; `robots.txt` hit a Cloudflare managed challenge | Text CC BY-SA (`siteinfo.rightsinfo`); images uploaded as fair use. Fandom ToU page 402/403'd (unread) | **Viable**: best variant *gallery* plus exclusives LOCG/Metron miss. Throttle hard. |
| **Open Library Covers** (`covers.openlibrary.org/b/id/{id}.jpg`, no suffix = original) | **3008×4524** (Saga Vol 1). The `-L` suffix gives 332×500 | No (trades only) | Edition record with pages, publisher, plus **Goodreads and LibraryThing ids** | Anonymous. 1 req/s, or 3 with a contact UA (prior doc) | Open data. "Do not bulk download" via the API (prior doc) | **Viable** for trades by ISBN |
| **Image Comics** (`imagecomics.com/files/releases/{slug}-cover.jpeg`) | **780×1200** (Saga #64, 2023). Inline CDN copy 384×591 | Unverified (cover credits shown as "Cover A"; variant pages not probed) | Diamond ID, age rating, page count, cover price, creators | Anonymous HTML | Not read | **Maybe**: modest resolution, but authoritative Image metadata |
| **Comixology / Kindle** (`m.media-amazon.com/images/I/{id}.jpg`, size flags stripped) | Unverified. Blogs say stripping `._SL1500_` returns the original upload | Mostly main cover only | Ratings (Amazon) | No usable API: PA-API 5 retired 2026-04-30/05-15; Creators API needs 10 qualified sales in 30 days | Amazon conditions forbid scraping | **Not viable** |
| **Marvel Unlimited** | Same `cdn.marvel.com` images (the MU logo sits on marvel.com issue pages) | — | — | No public API. Marvel developer API retired (prior doc) | Disney ToU | Covered by the Marvel.com row |
| **DC Universe Infinite** | Not probed | — | — | No public API found | — | **Not viable** (no surface) |
| **Lunar Distribution** | Only preview art on `media.lunardistribution.com` is public; the product catalogue needs a retailer login (prior doc) | Retailer CSVs only | — | Retailer account | — | **Not viable** |
| **Midtown Comics** (`/images/PRODUCT/FUL/{id}_ful.jpg`) | **300×466**; `XL`/`LG` sizes return 404 | Exclusives listed on product pages | Price | Anonymous | Not read | **Not viable** (low res). Useful only to name Midtown exclusives. |
| **TFAW** | Homepage images on CloudFront; product covers not probed | — | — | — | — | **Unverified**, low priority |
| **mycomicshop** | Blocked: Incapsula bot wall on the first GET | — | Prices, inventory | — | — | **Not viable** |
| **ComicBookRealm** (`iwt.sfo2.cdn.digitaloceanspaces.com/cbr-covers/{hash}_l.jpg`) | **306×400** (`_l`); the unsuffixed original returns 403 | Price-guide variants | Prices | Anonymous | Not read | **Not viable** |
| **Comic Collector Live** (`images.comiccollectorlive.com/covers/…/{uuid}.jpg`) | **591×911** (unsuffixed); `_tn` 295×455 | Yes (collector DB) | Collector metadata | Anonymous CDN | Not read | **Maybe**, only as a fallback below 600px-class sources |
| **ComicBase / Atomic Avenue** | Vendor claims "1.1 million large-sized cover images… tens of thousands at 4K", but only in the paid desktop *4K Archive Edition* (unverified) | Yes | Yes | Paid desktop product; no API | — | **Not viable** (no programmatic access) |
| **CGC / CBCS census** | n/a (no covers) | Census rows per variant | Graded population counts | HTML only, no official API; third-party Apify scrapers exist. Search snippet: "CBCS to Close in April 2026" (unverified) | — | **Not viable** for enrichment |
| **CovrPrice, Key Collector, GoCollect** | n/a | Price-guide variant lists | Values, key flags | No public API. GoCollect API is Enterprise-only (custom pricing); Key Collector site 403s | — | **Not viable** |
| **Bedetheque / BDGest** (`bedetheque.com/media/Couvertures/Couv_{id}.jpg`) | **500×665** | Yes, EO/re-editions per album | Full Franco-Belgian album data; reader rating (unverified, pages 403'd) | Album pages 403 to curl (anti-bot); media served. `robots.txt` disallows `/api/`, `/Couvertures/`, `/ajax/` | No ToU found. Ad/licence-funded | **Maybe** for Casterman/Humanoids/Cinebook originals. CLU scrapes it at ≥2 s/request. |
| **Deutscher Comic Guide** (comicguide.de) | Thumbnails seen 140×196; issue-page cover not located | Vorzugsausgaben listed as separate entries | ISBN, pages, price, publisher, weekly German releases (KW 41/2026 live) | Anonymous HTML (Joomla), no API seen | Impressum only | **Maybe** for Panini Verlag / Splitter metadata. Weak for covers. |
| **Wikidata** | n/a | n/a | ID crosswalk: P5905 Comic Vine (48,598 items), P3589 GCD series (2,434), P11308 GCD issue (629), P8619 BD Gest' series (583) | SPARQL/API, anonymous | CC0 | **Maybe**: series-level crosswalk for European series only. Too thin for issues. |
| **ComicVine image sizes** | `original_url` is the uploaded file; the other keys (`super_url`, `screen_large_url`, `medium_url`, …) are downscales | `associated_images` unreliable (prior doc) | — | Key, 200/resource/hour | Non-commercial | No larger size exists beyond `original_url` |
| **GCD cover sizes** | Public sizes are **only w100/w200/w400**. Uploads are scaled to these three and the original scan is not served | Variant rows | — | `files1.comics.org` returned 403 to this host for every size | Images rights-reserved | **No higher-res path**: w400 is the ceiling |
| **The Comic Book Database** (comicbookdb.com) | — | — | — | **Closed 16 Dec 2019** after its sale to Pop Culture Media | — | **Dead** |

### 1.2 Detail on the three that matter

**PRH cover CDN.** The strongest find.
- prhcomics.com links products as `/book/?isbn={17-digit UPC}` (12-digit UPC plus the 5-digit add-on), and the cover CDN uses the same key.
- Paths:
  - `/cover/{key}`: about 293×450 JPEG.
  - `?width=N`: works up to about 450, then returns *smaller* images as N grows (a quirk, not a resize).
  - `/cover/tif/{key}`: the print-res TIFF. Also answers 200 for trade ISBNs.
- **Missing keys return 200 with a placeholder JPEG** (same md5 `25cb3b1a…` for `00000000000000000` and for absent add-ons). Kometa must hash-check the small JPEG before pulling the TIFF.
- Do not synthesise add-ons. The issue/cover/printing convention is not reliable: X-ORCISTS #1 Cover C 1:25 is `…00116`, not `…00131`. Take UPCs from Metron `variant.upc` / issue `upc` or from the PRH listing.
- Publishers seen on prhcomics.com today: Marvel (`759606…`), IDW (Star Trek), BOOM! (Power Rangers), Dark Horse (library edition ISBN), plus manga.
- Back-catalogue depth is unverified. PRH only carries what it distributed, so expect Marvel from its PRH era onward, not 1990s issues.
- Cost: 18–45 MB per TIFF. 10k covers would be roughly 200–400 GB transferred, so convert to high-quality JPEG/WebP on arrival.
- Sources: https://prhcomics.com/ ; https://www.penguinrandomhouse.com/terms/

**Marvel.com / DC.com CDNs.** Both serve print-grade masters.
- Marvel: the image path minus the `/portrait_uncanny` suffix gives the full image (2750×4175). `/clean.jpg` is the same size at a higher bitrate. Variants come back at 1821×2800.
- Marvel issue pages carry a JSON blob with variant image paths, FOC date, price, page count, UPC and creators. Variant titles are bare "(Variant)", so pair them with Metron/PRH names via UPC or perceptual hash.
- DC: `static.dc.com` is an Akamai Image Server that caps at the native size (3253×5000). Its variant listing could not be confirmed.
- Both ToUs explicitly forbid automated extraction. Sources: https://www.marvel.com/comics/issue/137039/midnight_spider-man_2026_1 ; https://disneytermsofuse.com/english/ ; https://www.dc.com/terms

**Fandom (Marvel/DC Database).**
- One API call per issue page returns every image with its size and URL: `api.php?action=query&generator=images&titles={Series_Vol_N_#}&prop=imageinfo&iiprop=size|url`.
- File names encode the variant type and artist ("…Artgerm Black Cat Virgin Variant", "…Variant Cover Midtown Comics Dan Panosian", "…10th Print Inhyuk Lee"), which makes this the best source for **retailer/convention exclusives and later printings**.
- Resolution is uneven (about 400 to 2000px wide); keep the largest per variant.
- Licence: text CC BY-SA; images are fair-use uploads. Fandom sits behind Cloudflare: the API answered, but `robots.txt` got a managed challenge. Pace at ≥1 s per call with a descriptive User-Agent.
- An Image Comics Fandom wiki also exists, but its depth is unverified.
- Sources: https://marvel.fandom.com/wiki/Variant_Covers ; live `api.php` calls (§5)

---

## 2. Community and critic ratings

| Source | What exists | Granularity | Anonymous? | Access | Limits / ToS | Verdict |
|---|---|---|---|---|---|---|
| **LOCG** | Rating, pull/collect counts, POTW (known) | Issue | Yes | Cloudflare-gated scrape | See prior doc | **Viable** (already in plan) |
| **ComicBookRoundup** | **Critic rating + user rating with review counts.** Saga series: 792 critic reviews, user 8.7 from 1,934. Saga #66: 3 critic, 14 user | Series and issue (`/comic-books/reviews/{publisher}/{series}/{n}`) | Yes, static HTML; no Cloudflare challenge seen | Scrape (`div.critic-rating`, `div.user-rating`) | `robots.txt` only blocks signin/search. No terms page (`/terms`, `/terms-of-use` 404). No API | **Viable**: the only critic aggregate. Thin for indie/European titles. |
| **Hardcover.app** | Average rating, reader counts per book/edition (ISBN) | Trades/GNs (book-level) | Free token | GraphQL. Free tier: 5,000/day, 60/min, burst 10, 30 s timeout; "beta… may reset tokens" | Aggregate data allowed if credited ("average Hardcover rating"); no LLM training | **Viable** for trades |
| **Open Library** | `/works/{id}/ratings.json`: average, count, 1–5 histogram (Saga v1: 4.35 from 68) | Trades | Yes | REST | 1–3 req/s | **Viable but thin**. Its Goodreads id crosswalk is the real value. |
| **Goodreads** | Largest trade ratings pool | Trades | — | **API closed to new keys 2020-12-08**; no replacement | Scraping against ToS | **Not viable** |
| **Google Books** | `averageRating` / `ratingsCount` (from memory, unverified) | Trades | Anonymous shared quota was already exhausted (429) | REST with own API key | Google API terms | **Maybe** (needs a key; ratings sparse) |
| **Metron** | `Issue.rating` is a FK to a `Rating` model (`name`, `short_description`, `description`, default=1). This is the **age rating**, matching ComicInfo `AgeRating`. No review/vote model exists | — | — | — | — | **No community ratings** (verified from source) |
| **GCD** | `rating` column is labelled "Publisher's Age Guidelines" | — | — | — | — | **No community ratings** |
| **ComicVine** | User and staff reviews on the website. No review resource in the API resource list | Issue | Site only | Scraping site = against terms | — | **Not viable** |
| **VerseDB** | Reviews exist, but "reviews" are listed as not in the API (prior doc) | Issue | — | — | No-scrape ToS | **Not viable** |
| **Amazon / Comixology** | Star ratings | Issue/volume | — | No API without Associates sales | Scrape-forbidden | **Not viable** |
| **Marvel Unlimited / DC UI** | No public ratings surface found (unverified) | — | — | — | — | **Not viable** |
| **Bedetheque** | Reader rating per album (unverified; pages 403'd to curl) | Album | — | Scrape | No ToU found | **Maybe** (European only) |
| **Reddit** | Discussion, no structured scores | — | — | Data API (OAuth) | Non-commercial tier | **Not viable** for scores |
| **Key Collector / CovrPrice / GoCollect** | Key flags and values, not ratings | — | — | App/paid | — | **Not viable** |
| **StoryGraph** | Ratings, no public API (unverified) | Trades | — | — | — | **Not viable** |

---

## 3. Ranking and what to run in parallel

### 3.1 Ranked additions

1. **PRH cover CDN by UPC/ISBN.** Print-res TIFF (1400–1987px wide) for the Marvel/IDW/BOOM!/Dark Horse modern run and their trades. It joins on Metron's `upc`. Costs: placeholder detection plus heavy bytes.
2. **Fandom wikis (Marvel/DC).** The variant *inventory*, including exclusives and later printings, with images up to about 2000px. One API call per issue.
3. **ComicBookRoundup.** The only critic + user score source. One page per issue.
4. **Hardcover + Open Library by ISBN.** Trade ratings plus up-to-3000px trade covers; Open Library adds the Goodreads id.
5. **Marvel.com / DC.com CDNs.** The highest resolution of all (2750–3253px wide), but the ToU forbids automation. Only if the user accepts that risk for a private library; then use them as the cover-upgrade tier for Marvel/DC main covers.
6. **Bedetheque + comicguide.de.** The European gap-fillers: metadata for Casterman/Humanoids/Cinebook (FR) and Panini/Splitter (DE). Covers are 500px at best.
7. **Image Comics site.** 780px covers plus Diamond ID / page count for Image issues.

Skip: Amazon/Comixology, Goodreads, retail shops (≤600px or bot-walled), price guides and census (no API, no covers), Wikidata for issues, ComicBase (paid desktop only), comicbookdb (dead).

### 3.2 Parallel worker plan

All workers run in-app on ginbako, following the enrichment spec.

| Worker | Source | Key | Budget (proposal) | Fills |
|---|---|---|---|---|
| A (existing) | Metron | — | 20/min burst, 5,000/day | Canonical issue, variant list, UPC, credits, arcs |
| B (existing) | LOCG | — | Trickle, pass-gated | Ratings/pulls, far-ahead variants |
| C (existing) | ComicVine | API key | 200/resource/hour | Back-catalogue ids, characters |
| **D (new)** | **PRH CDN** | Metron `upc` / `variant.upc`, issue/trade `isbn` | ~1 req/2 s; small JPEG hash-check first, then TIFF | Print-res main and variant covers |
| **E (new)** | **Fandom MediaWiki API** | Series + vol + issue → page title | ≤1 req/s, descriptive UA | Variant inventory (exclusives, printings), images up to ~2000px |
| **F (new)** | **ComicBookRoundup** | Publisher/series slug + issue number | ~1 req/3 s | Critic and user scores |
| **G (new)** | **Hardcover + Open Library** | Trade ISBN | Hardcover 60/min (5k/day); OL 1 req/s | Trade ratings, large trade covers, Goodreads id |
| H (optional) | Bedetheque / comicguide.de | Title + tome / ISBN | ≥2 s per request (CLU's pace) | European metadata |

**Cover pick rule:** keep the largest image per (issue, variant). PRH TIFF > Marvel/DC CDN (if allowed) > Fandom > Metron 768 > ComicVine original > LOCG 600 > GCD 400. Match variants across sources by UPC first, then perceptual hash (Metron already stores `cover_hash`).

---

## 4. Not verified

- PRH back-catalogue depth and DC/Image presence on the PRH CDN.
- DC.com variant listing; older Marvel issue sizes (only 2026 issues measured).
- Amazon original-image sizes; Google Books rating fields.
- Bedetheque reader ratings; ToS texts for Fandom, Image, Midtown, ComicBookRoundup, comicguide.de.
- The CBCS closure; ComicBase 4K claims.

## 5. Probe log (2026-10-12)

| Host | Requests | Result |
|---|---|---|
| marvel.com + cdn.marvel.com | 3 pages, ~9 images | 200 |
| dc.com + static.dc.com | 3 pages, 4 images, terms | 200 |
| imagecomics.com | 2 pages, 3 images | 200 |
| prhcomics.com + images.penguinrandomhouse.com | 1 page, ~20 small JPEGs, 2 full TIFFs (45 MB + 18 MB, deleted), 5 HEAD | 200 (placeholders noted) |
| marvel/dc.fandom.com | 3 `api.php`, 1 `robots.txt` | API 200; robots got a Cloudflare challenge |
| files1.comics.org | 8 | all 403 (stopped) |
| comicbookroundup.com | 2 pages, robots, 2 terms URLs | 200 / 404 |
| bedetheque.com | 1 page, robots, 1 image | 403 / 200 / 200 |
| comicguide.de | 2 pages, robots, 1 image | 200 |
| openlibrary.org / covers | 4 | 200 |
| googleapis.com/books | 1 | 429 (shared quota) |
| wikidata.org / query.wikidata.org | 7 + 4 | 200 |
| midtowncomics, comicbookrealm, comiccollectorlive, lunardistribution, tfaw, mycomicshop, atomicavenue, cgccomics, covrprice, gocollect, keycollectorcomics | 1 homepage each, plus ≤4 image-size probes on 3 of them | see §1.1 |
| GitHub (`gh api`) | Metron, GCD, Simyan, Hardcover docs, CLU sources | — |
