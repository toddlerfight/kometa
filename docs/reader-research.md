# Comic reader research: prior art for Kometa's reader and "On Deck"

**Scope.** This is a survey of existing comic, manga and ebook reading apps. It informs the design of a reader built into Kometa, which will replace Komga as the reading surface, and of a reading-focused "On Deck" section with four parts: Currently reading, Next / coming soon, Suggestions and Reading lists. Phase A is a reader in the existing responsive web app. Phase B is a native iOS client that downloads books for offline reading and syncs progress back. The library is roughly 900+ series and 7k+ books, mostly CBZ with some CBR, all Western comics: monthly singles plus trades and omnibuses, some books near 280 pages, some pages near 10 MB. The reading pattern to design for is mixed. The reader tries many #1s and drops most of them a few pages in, keeps up with a handful of monthlies, and sometimes binges a run. The survey covers home-screen sections, "up next" logic, how abandoned books are handled, reading lists (CBL), suggestions, reader-view mechanics, and server and sync APIs.

Research date: 2026-10. Claims link to primary sources: official docs, App Store listings, specs and source code. Anything else is marked *(secondary)*. Claims that could not be checked are marked *unverified*, and unknown cells in the matrix are `?`.

---

## 1. Feature matrix

Abbreviations: CR = continue reading, LTR/RTL = reading direction, PSE = OPDS Page Streaming Extension, DPS = double-page spread.

| App | Continue / On Deck | Up Next logic | Abandon handling | Reading lists (CBL?) | Suggestions | Spreads (auto-detect?) | Guided / panel view | Fit modes | RTL | Webtoon | Scrubber | Offline | Progress sync | Self-hosted protocol |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Komga** (web) | "Keep Reading" (in-progress books, newest first) + "On Deck" (next unread book) [^k-dash] | First unread book by `numberSort` in series that have ≥1 read and 0 in-progress books [^k-ondeck] | Only mark read/unread. No removal, no expiry [^k-ondeck]. Incognito reading saves no progress [^k-incog] | Read lists: manual or by release date. **CBL import** [^k-rl] | None | Two-up; landscape pages and first/last page shown single [^k-divina] | No | Screen / width / height / original [^k-divina] | Yes | Yes | Thumbnail explorer [^k-divina] | Server only | Per-user, server-side | OPDS 1.2 + PSE 1.2, OPDS 2, REST, Readium progression [^k-opds][^k-prog] |
| **Kavita** | "On Deck" stream + Recently Updated / Newly Added / Smart-filter streams [^kv-streams] | Series "continue point" plus `next-chapter` in logical volume/chapter order [^kv-reader] | **"Remove from On Deck"** per series, undone when you read it again. **Auto-expiry**: dropped after 30 days with no progress unless a chapter was added in the last 7 days [^kv-ondeck] | Reading lists, admin-promotable, **CBL v1 + JSON v2, URL lists re-synced every 3 days**, 6-tier matcher + remap rules [^kv-rl][^kv-cbl] | Kavita+ (paid) only: external recs [^kv-plus] | Single / Double / Double (Manga). Wide-image split options [^kv-reader-doc] | No | Height / width / original [^kv-reader-doc] | Yes | Yes | ? | Server only | Per-user, server-side | OPDS + PSE. Progress written on PSE page fetch. Panels-specific + KOReader endpoints [^kv-opds][^kv-implicit][^kv-panels] |
| **Panels** (iOS/Mac) | "Reading Now" tab from reading sessions [^p-sessions] | ? | ? (*unverified*: no documented "remove from Reading Now") | ? | None | One / Two / **Automatic** (orientation). Auto page grouping, user fix "Group from this page on" [^p-config][^p-faq] | **Panels View**: ML panel detection, experimental [^p-view] | Fit / Fill [^p-config] | Yes [^p-app] | Vertical / continuous [^p-config] | ? | Download series in one tap; iCloud "Keep Downloaded" [^p-komga][^p-faq] | Panels account sync; writes to Komga's progress API [^p-komga] | OPDS client (Komga, Kavita, Codex, Stump, Calibre) [^p-opds] |
| **Chunky** (iOS) | ? | ? | ? | No | None | Two-up [^ch-app] | No | ? | Yes [^ch-app] | ? | ? | Downloads from servers [^ch-app] | ? | Pro: SMB/FTP/ComicStreamer/Ubooquity. **Not working with Komga ≥1.4.0** [^k-opds]. Last App Store update 2020 [^ch-app] |
| **Marvel Unlimited** | Home "Continue Reading" shelf; pinned Reading Lists on home [^mu-howto] | **End-of-book choice: "Next In Series" or "Next In Reading Guide"**; CR shelf can advance through a guide *(secondary)* [^mu-next] | **Remove specific issues from Continue Reading** (*per search excerpt; page could not be fetched*) [^mu-notes] | Editor-curated Reading Guides by character/event/creator [^mu-howto] | "Recommended Reading Guides and series based on your current reading and favorites" [^mu-relaunch] | ? | Smart Panel *(secondary)* [^mu-next] | ? | n/a | Infinity Comics (vertical) [^mu-relaunch] | ? | Unlimited downloads [^mu-relaunch] | Account | n/a. **Service sunsets 2026-12-07**, history imports to the WEBTOON-run Marvel Comics app [^mu-sunset] |
| **DC Universe Infinite** | **"Dive Back In"** row, items removable [^dc-app] | ? | Remove from Dive Back In [^dc-app] | MyDC custom lists [^dc-app] | ? | Landscape on tablets [^dc-app] | ? | ? | n/a | ? | ? | Unlimited offline downloads [^dc-app] | Account | n/a. **Follow series → notified on new issues** [^dc-app] |
| **Kindle / ComiXology** | ? | ? | ? | Collections | Store-driven | Publisher-authored facing pages [^kdp-create] | **Guided View**: publisher-authored panels (auto-detect *assist* + manual review) [^kdp-create][^cx-guided] | ? | Per-book [^kdp-create] | ? | ? | Yes | Whispersync "furthest page read" [^kindle-sync] | n/a |
| **Plex** (video; conceptual) | "Continue Watching" (merged On Deck + in-progress) | Next unwatched episode | **Look-back window (16 weeks default)**, max-items cap, **season premieres re-surface** past the window; remove from CW *(secondary)*; played threshold 90% [^plex-lib][^plex-forum] | Playlists | Separate recs hubs | n/a | n/a | n/a | n/a | n/a | n/a | Downloads | Server | Plex API |
| **Paperback** (iOS) | ? | ? | ? | ? | None | Double-page [^pb-app] | No | ? | Yes [^pb-app] | Yes [^pb-app] | ? | Downloads [^pb-app] | Writes read chapters to Komga (app→server) [^k-pb] | Extension API (Komga built in; Kavita) [^k-pb][^pb-app] |
| **Mihon / Tachiyomi** (Android) | History tab with per-entry remove (clears that chapter's read date) [^mh-hist]; Updates tab | Next chapter, with **skip read / filtered / duplicate chapters** options [^mh-strings] | Remove from History [^mh-hist]; Incognito pauses history [^mh-lib] | Categories (no CBL) [^mh-readme] | None | Split wide pages / invert / rotate [^mh-reader][^mh-strings] | No | Several scale types [^mh-reader] | Yes | Long strip (± gaps) [^mh-reader] | ? | Downloads | Trackers one-way. **Komga = two-way, chapter-level** [^mh-track] | Komga/Kavita via extensions + enhanced trackers [^mh-track] |
| **YACReader** (+ Library Server) | "Continue Reading" in remote and local browsers [^yac-2] | ? | ? | Reading lists, "Favorites", "being read" lists, labels [^yac-8][^yac-home] | None | Single / double / **double auto (landscape)**, **spread detection** incl. manga [^yac-8] | Panel-by-panel on iOS/Android [^yac-home] | ? | Yes [^yac-home] | Continuous scroll [^yac-home] | ? | Download for offline [^yac-home] | iOS **syncs back** to desktop library [^yac-2] | Custom REST API v2 (incl. sync controller); no OPDS in source [^yac-src] |
| **Apple Books** | Home "Continue", **"Remove from Continue"** keeps book in library [^ab-remove] | ? | Remove from Continue; Hide Book; **"My Samples"** collection [^ab-read] | Collections, Want to Read [^ab-read] | Personalised suggestions on Home [^ab-read] | Two-page view for fixed layout [^ab-read] | No | ? | Per-book | n/a | ? | Download/remove | iCloud | n/a |
| **KMReader** (Komga iOS client) | Customisable dashboard sections [^km] | Komga's | Incognito (progress kept off server) [^km] | Read lists offline [^km] | ? | Spreads [^km] | ? | ? | Yes | Yes | ? | **Per-series offline policy: manual / latest / all** [^km] | Real-time sync; offline progress queued, synced on reconnect [^km] | Komga API [^km] |
| **Komic** (iOS) | ? | ? | ? | Collections + read lists mgmt [^komic] | ? | ? | ? | ? | Yes | ? | ? | Downloads [^komic] | Yes [^komic] | Komga + Kavita APIs [^komic] |
| **Codex** (server) | ? | ? | ? | Story-arc browsing [^codex] | ? | ? | ? | ? | Yes [^codex] | ? | ? | n/a | Server | OPDS 1 & 2 with streaming [^codex]. PSE 1.2 server [^pse] |

---

## 2. Patterns worth stealing

### Currently reading

1. **Separate "in progress" from "next up".** Komga splits the home screen into *Keep Reading*, which holds books with partial progress sorted by last read, and *On Deck*, which holds the next unread book in series where you've finished at least one book and have nothing in progress [^k-dash][^k-ondeck]. Plex used to work the same way and now merges both into one Continue Watching row [^plex-forum] *(secondary)*. **Why it fits:** a #1 abandoned on page 3 is "in progress" but means something different from "finished #4, #5 is waiting". Keeping the rows apart stops samples from crowding out the monthlies.
2. **Automatic expiry with a "new content" exception.** Kavita drops a series from On Deck after `OnDeckProgressDays` (default 30) with no progress, *unless* a chapter was added within `OnDeckUpdateDays` (default 7). The setting is per user [^kv-ondeck]. Plex uses a look-back window (default 16 weeks) and brings a season premiere back after the window has passed [^plex-lib]. **Why it fits:** stale samples age out with no effort from the reader, and a monthly comes back when its new issue lands. That is exactly the "keep up with monthlies" case.
3. **Explicit, reversible dismissal.** Kavita keeps an `AppUserOnDeckRemoval` row per user and series and clears it automatically the next time progress is saved on that series [^kv-ondeck][^kv-clear]. Apple Books has "Remove from Continue", which keeps the book in the library [^ab-remove]. DCUI lets you remove items from "Dive Back In" [^dc-app]. Marvel Unlimited added per-issue removal from Continue Reading [^mu-notes]. **Why it fits:** dismissal is a cheap "not now" that doesn't lie about read state, and it undoes itself if you come back to the book.
4. **Sample-aware states.** Apple Books keeps a separate "My Samples" collection [^ab-read]. Komga, KMReader and Mihon offer *incognito* reading that writes no progress [^k-incog][^km][^mh-lib]. **Why it fits:** "try a #1" can be its own state, or a reading-mode choice, instead of becoming dirty progress.
5. **A completion threshold.** Plex marks an item played at 90% by default [^plex-lib]. Comics end in ads, letters pages and variant galleries, so a page-based threshold avoids books that are effectively finished but never marked complete. *(No surveyed comic reader documents a threshold; the comic servers checked track an exact page.)*

### Next / coming soon

6. **Next = first unread by sort order, not "number + 1".** Komga takes the lowest-`numberSort` unread book [^k-ondeck]. Kavita walks volumes, then chapters, then specials in logical order [^kv-reader]. **Why it fits:** Kometa already owns issue numbering and knows when Komga's numbering is wrong, so the "next" rule can use Kometa's own sort key.
7. **The reading context travels with the reader.** Komga's reader URL carries `context=READLIST&contextId=…`, so its next and previous books follow the list rather than the series. A first page-turn past the last page shows a "move to next" snackbar and a second one goes there [^k-ctx][^k-reader-src]. Marvel Unlimited's end-of-book screen offers **Next In Series *or* Next In Reading Guide**, and its Continue Reading shelf can move forward through a guide *(secondary)* [^mu-next]. **Why it fits:** arcs become reading lists in Kometa, and an arc-ordered read through crossovers is the main reason to have lists at all.
8. **Release-aware "coming soon".** DCUI lets you follow a series and get notified of new issues [^dc-app]. Mihon's Upcoming page plots *predicted* releases from past release gaps and says plainly that they are estimates [^mh-upcoming]. Mihon's smart update only checks series that have been started, aren't completed and have no unread chapters [^mh-smart]. **Why it fits:** Kometa already has real release dates (store dates) and acquisition state, so "next issue out Wednesday", "downloading" and "not owned yet" can be shown truthfully. That beats Mihon's estimate.
9. **Skip rules when advancing.** Mihon can skip chapters already marked read, filtered chapters and duplicates [^mh-strings]. **Why it fits:** the shelf mixes trades with singles, so "next" must not land on a trade that reprints issues already read, or on a duplicate edition.

### Suggestions

10. **Curated or behavioural, and kept separate from progress.** Marvel Unlimited recommends "Reading Guides and series based on your current reading and favorites" and runs creator-curated "Creator Spotlights" [^mu-relaunch][^mu-howto]. Apple Books shows personalised suggestions on Home [^ab-read]. Kavita puts recommendations behind Kavita+, sourced from external services [^kv-plus]. Komga, Panels, Mihon and Paperback have none. **Why it fits:** the self-hosted field has almost nothing here. A creator-based engine running over the owned shelf has no direct competitor among self-hosted servers.
11. **Feed sampling behaviour into suggestions.** No surveyed app documents using abandonment as a negative signal. *(Gap observed, not a sourced pattern.)*

### Reading lists

12. **CBL import with a tiered matcher and saved remaps.** Kavita matches in order: remap rules, then ComicVine/Metron IDs, exact name plus year, "Name (Volume Year)", article-stripped name, reprint-stripped name ("Deluxe/Omnibus/TPB"), and alternate series. It reports *Series / Volume / Issue Missing* separately [^kv-cbl]. Komga matches "Series" or "Series (Volume)" plus number, and lets you assign misses by hand [^k-rl]. **Why it fits:** CBL entries carry ComicVine series and issue IDs (`<Database Name="cv" Series=… Issue=…/>`) [^cbl-sample]. Kometa can turn "Issue Missing" into "acquire it", which a pure reader can't do.
13. **A community list source.** The DieselTech CBL-ReadingLists repo holds about 1,700 `.cbl` files across Marvel, DC, Image, Dark Horse, IDW and others, verified against ComicVine with nightly integrity reports. Kavita, Komga, ComicRack CE and Mylar consume it [^cbl-repo][^cbl-count]. **Why it fits:** it is a ready-made catalogue for the Reading lists section.
14. **Subscribed lists.** Kavita re-syncs URL-based lists every 3 days [^kv-rl]. **Why it fits:** an imported event list stays current as the curators fix it.
15. **Editor lists pinned to Home.** Marvel Unlimited lets you pin a Reading List to the home page [^mu-howto].

### Reader view

16. **Spreads.** Komga's rule: in double mode, show the first page, the last page and any landscape page as singles [^k-divina]. YACReader offers "double page auto" (two-up in landscape only) with spread detection, including manga order [^yac-8]. Panels offers an "Automatic" layout and lets you fix the pairing offset with "Group from this page on" [^p-config][^p-faq]. Kavita and Mihon offer splitting or rotating wide images [^kv-reader-doc][^mh-strings]. PSE says the server **must not** split spreads; splitting is the client's job [^pse]. **Why it fits:** the Komga aspect-ratio rule plus a manual offset fix is cheap. Phone portrait and iPad landscape need different defaults.
17. **Panel view is either authored or ML-detected, never perfect.** ComiXology and Kindle Guided View uses panel data authored by the publisher; Kindle Create's auto-detect needs manual review [^kdp-create][^cx-guided]. Panels View uses an ML model and is still labelled experimental [^p-view]. YACReader ships panel-by-panel on mobile [^yac-home]. **Why it fits:** this is phone-only polish for a later phase. If attempted, it needs a fallback to fit-width.
18. **A small, standard set of fit modes.** Komga offers screen, width, height and original [^k-divina]. Kavita offers height, width, original and a width override [^kv-reader-doc]. Panels offers Fit and Fill [^p-config].
19. **Tap zones are configurable.** Mihon offers L-shaped, Kindle-ish, Edge and Right-and-Left layouts [^mh-reader]. KMReader has customisable tap zones [^km]. Komga lets you turn touch gestures off [^k-divina].
20. **Background options.** Komga offers white, gray or black [^k-divina]. Panels adds a *dynamic* background taken from the page colours [^p-config]. Kavita has brightness control and an "Emulate comic book" spine shadow [^kv-reader-doc].
21. **Thumbnail scrubber / jump.** Komga has a thumbnails explorer [^k-divina]. Kavita has a "G" jump-to-page modal [^kv-reader-doc]. Komga's API serves page thumbnails at 300 px max [^k-api].
22. **Image clean-up.** Panels has noise, sharpness, moiré and border-crop filters [^p-app]. Chunky has upscaling and auto-contrast for yellowed scans [^ch-app]. YACReader has margin trimming [^yac-home]. Mihon has crop borders [^mh-reader].

### Sync & offline / server API

23. **Page-image endpoint with resizing.** PSE uses a URL template with `{pageNumber}` (0-based) and an optional `{maxWidth}`, and declares `pse:count`. `pse:lastRead` is 1-based and comes with `pse:lastReadDate` [^pse]. Komga serves `/api/v1/books/{id}/pages/{n}` (1-based by default, `zero_based` param, `convert=jpeg|png`), a `/raw` variant and `/thumbnail` [^k-api][^k-cbc]. Kavita serves a page from its extracted-chapter cache with a 1-hour max-age [^kv-reader]. **Why it fits:** `maxWidth` matters for 10 MB pages on phones. Pre-extracting or caching per book is the pattern servers use.
24. **Progress writes: explicit or implicit.** Komga uses an explicit `PATCH /api/v1/books/{id}/read-progress {page, completed}` [^k-api]. Kavita *infers* progress when a PSE page is fetched [^kv-implicit] and also has a dedicated `POST /api/panels/save-progress` for Panels [^kv-panels]. **Why it fits:** implicit progress makes simple OPDS clients "just work", but prefetching then counts as reading. A native client should write progress explicitly.
25. **Last-write-wins by client timestamp.** Komga's Readium Progression API (`GET/PUT …/progression`, `application/vnd.readium.progression+json`) rejects an update whose `modified` time is older than the saved read date [^k-prog]. KMReader queues progress made offline and syncs it on reconnect [^km]. **Why it fits:** this is the minimum conflict rule the Phase B client needs.
26. **Per-series offline policy.** KMReader lets you set manual, latest or all per series, with read lists and collections available offline [^km]. Panels downloads a series in one tap [^p-komga]. **Why it fits:** "keep the latest N of my monthlies downloaded" fits the monthlies habit and keeps phone storage in check.
27. **Third-party iOS readers depend on the server's API, not just OPDS.** Panels uses Komga's progress API [^p-komga]. Kavita added a Panels-only endpoint [^kv-panels]. Mihon uses Komga as a two-way "enhanced tracker" at chapter level [^mh-track]. Kavita says outright that "clients that use the API have a richer experience" than OPDS-only ones [^kv-opds]. Komga lists Panels (PSE 1.0/1.1 with progress), KyBook 3 and JustRead as working OPDS 1 clients, ComicVerse for OPDS 2, and Chunky as broken [^k-opds]. **Why it fits:** offering OPDS 1.2 + PSE would make Panels and other iOS readers usable before Phase B ships.

---

## 3. Decisions this raises for Kometa

1. **What counts as "currently reading"?** Any progress (Komga Keep Reading) · progress plus a recency window (Kavita 30 days, Plex 16 weeks) · a minimum page or percentage before it counts · samples as a separate state or collection (Apple "My Samples").
2. **How does something leave Currently reading?** Manual dismiss that undoes itself on resume (Kavita, Apple, DCUI, MU) · time-based expiry · mark unread (which loses the history) · all three.
3. **Is "next up" a separate row or merged in?** Separate rows (Komga) · merged (Plex) · merged with a visible label.
4. **What qualifies a series for "Next"?** ≥1 read and none in progress (Komga) · recent activity or new content (Kavita) · followed or monthly flag (DCUI follow) · only owned issues, or also known-but-unowned upcoming issues with release dates.
5. **How is the next book chosen?** Series sort order (Komga/Kavita) · reading-list order when entered from a list (Komga context, MU) · skip read, duplicate and reprint editions (Mihon) · what happens when a trade overlaps singles already read.
6. **What is the end-of-book behaviour?** Snackbar, then a second turn advances (Komga) · explicit choice between series and list (MU) · auto-advance.
7. **When is a book complete?** Last page only · a percentage threshold (Plex 90%) · last page minus trailing ads or backmatter.
8. **Where do reading lists come from?** Kometa arcs only · CBL import (file or subscribed URL, Kavita) · a community repo browser (DieselTech) · editor-style pinned lists (MU). What happens to unmatched entries: remap rules (Kavita), manual assign (Komga), or queue acquisition.
9. **Should Suggestions use negative signals?** Abandoned samples as negatives · creator affinity only · suggest only within the owned shelf, or also unowned books.
10. **What is the spread default?** Two-up only in landscape (YACReader, Panels) · Komga's aspect-ratio rule · manual pairing-offset fix (Panels) · split wide pages on phones (Kavita, Mihon).
11. **Is panel view in or out?** Out for Phase A · ML detection later (Panels) · not at all (Komga, Kavita).
12. **How are pages served?** Stream from the archive per request · pre-extract or cache per book (Kavita) · server-side resize via `maxWidth` (PSE) · format conversion (Komga `convert`).
13. **How is progress written and merged?** Explicit write (Komga PATCH) · implicit on page fetch (Kavita OPDS) · last-write-wins by client timestamp (Komga progression) · offline queue (KMReader).
14. **Which external protocol, if any?** None (Kometa iOS only) · OPDS 1.2 + PSE (Panels and other iOS readers today) · OPDS 2 · Komga-compatible API surface (Mihon, Paperback, Komic, KMReader).
15. **What is the offline policy for Phase B?** Manual per book · per series manual / latest N / all (KMReader) · whole reading list.
16. **How is multi-reader readiness handled?** Every server checked keys progress, dismissals and preferences per user (Komga, Kavita), and Kavita moved its On Deck windows from server settings to per-user preferences [^kv-ondeck]. Decide whether dismissals, windows and thresholds live per user or per server.

---

## 4. Sources

[^k-dash]: Komga web UI dashboard loaders (Keep Reading = IN_PROGRESS sorted `readProgress.readDate desc`; On Deck; Recently Released = last month; Recently Added/Updated; Recently Read). https://github.com/gotson/komga/blob/master/komga-webui/src/views/DashboardView.vue
[^k-ondeck]: Komga `getBooksOnDeckQuery`: series with `IN_PROGRESS_COUNT = 0` and `READ_COUNT != BOOK_COUNT`, first unread by `NUMBER_SORT`, sorted by most recent read date, no time limit. https://github.com/gotson/komga/blob/master/komga/src/main/kotlin/org/gotson/komga/infrastructure/jooq/main/BookCommonDao.kt
[^k-incog]: Komga reader incognito query param and "read incognito" action. https://github.com/gotson/komga/blob/master/komga-webui/src/views/DivinaReader.vue · https://github.com/gotson/komga/blob/master/next-ui/src/api/links.ts
[^k-rl]: Komga docs, Read lists (manual vs release-date ordering, CBL import matching). https://komga.org/docs/guides/readlists
[^k-divina]: Komga docs, Webreader (DIVINA): reading modes, scale types, double-page rules, background, thumbnails explorer, gestures. https://komga.org/docs/guides/webreader-divina
[^k-opds]: Komga docs, OPDS (v1.2 + PSE 1.2, v2; tested clients; Chunky broken on 1.4.0+). https://komga.org/docs/guides/opds
[^k-prog]: Komga `CommonBookController` (Progression API GET/PUT, `/raw` pages, file download role) and `BookLifecycle` ("Progression is older than existing" check). https://github.com/gotson/komga/blob/master/komga/src/main/kotlin/org/gotson/komga/interfaces/api/CommonBookController.kt · https://github.com/gotson/komga/blob/master/komga/src/main/kotlin/org/gotson/komga/domain/service/BookLifecycle.kt
[^k-cbc]: Same as above, `CommonBookController.kt`.
[^k-api]: Komga `BookController` (read-progress PATCH/DELETE, pages, page thumbnail 300px, next/previous, ondeck). https://github.com/gotson/komga/blob/master/komga/src/main/kotlin/org/gotson/komga/interfaces/api/rest/BookController.kt
[^k-ctx]: Komga read-list navigation endpoints and Mihon read-progress endpoints. https://github.com/gotson/komga/blob/master/komga/src/main/kotlin/org/gotson/komga/interfaces/api/rest/ReadListController.kt
[^k-reader-src]: Komga reader end-of-book snackbar ("move_next") and read-list sibling lookup. https://github.com/gotson/komga/blob/master/komga-webui/src/views/DivinaReader.vue
[^k-pb]: Komga docs, Read with Paperback. https://komga.org/docs/guides/paperback
[^km]: Komga docs, Read with KMReader. https://komga.org/docs/guides/kmreader
[^kv-streams]: Kavita `DashboardStreamType` enum (OnDeck, RecentlyUpdated, NewlyAdded, SmartFilter). https://github.com/Kareadita/Kavita/blob/develop/Kavita.Models/Entities/Enums/DashboardStreamType.cs
[^kv-ondeck]: Kavita `SeriesRepository.GetOnDeckAsync` (per-user progress/update day windows, removals excluded, ordering) and seed defaults 30/7. https://github.com/Kareadita/Kavita/blob/develop/Kavita.Database/Repositories/SeriesRepository.cs · https://github.com/Kareadita/Kavita/blob/develop/Kavita.Database/Seed.cs
[^kv-clear]: Kavita `ReaderService` / `ReaderController` call `ClearOnDeckRemovalAsync` when progress is saved or marked. https://github.com/Kareadita/Kavita/blob/develop/Kavita.Server/Controllers/ReaderController.cs
[^kv-reader]: Kavita `ReaderController` (image from chapter cache, progress, continue-point, next-chapter logical order, prompt-reread). https://github.com/Kareadita/Kavita/blob/develop/Kavita.Server/Controllers/ReaderController.cs
[^kv-reader-doc]: Kavita wiki, Comic/Manga reader. https://wiki.kavitareader.com/guides/readers/comic-manga/
[^kv-rl]: Kavita wiki, Reading Lists. https://wiki.kavitareader.com/guides/features/readinglists/
[^kv-cbl]: Kavita wiki, CBL Import. https://wiki.kavitareader.com/guides/features/cbl-import/
[^kv-opds]: Kavita wiki, OPDS. https://wiki.kavitareader.com/guides/features/opds/
[^kv-implicit]: Kavita `OPDSController.GetPageStreamedImage` calls `SaveReadingProgress`. https://github.com/Kareadita/Kavita/blob/develop/Kavita.Server/Controllers/OPDSController.cs
[^kv-panels]: Kavita `PanelsController` ("For the Panels app explicitly"; save-progress / get-progress). https://github.com/Kareadita/Kavita/blob/develop/Kavita.Server/Controllers/PanelsController.cs
[^kv-plus]: Kavita wiki, Kavita+. https://wiki.kavitareader.com/kavita+/
[^p-app]: Panels, App Store listing. https://apps.apple.com/us/app/panels-comic-reader/id1236567663
[^p-sessions]: Panels guides, Reading sessions ("Reading Now" tab). https://guides.panels.app/read-content/reading-sessions
[^p-config]: Panels guides, Reader configurations. https://guides.panels.app/read-content/reader-configurations
[^p-view]: Panels guides, Panels View. https://guides.panels.app/read-content/panels-view
[^p-faq]: Panels guides, FAQs ("Group from this page on", Keep Downloaded, account sync). https://guides.panels.app/faqs
[^p-komga]: Panels, Komga iOS app page. https://panels.app/komga-ios-app
[^p-opds]: Panels guides, OPDS: your own server. https://guides.panels.app/category/opds-your-own-server
[^ch-app]: Chunky Comic Reader, App Store listing (v2.5.10, Aug 2020). https://apps.apple.com/us/app/chunky-comic-reader/id663567628
[^mu-howto]: Marvel, "How to Read Comics the Marvel Unlimited Way". https://www.marvel.com/articles/comics/how-to-read-comics-the-marvel-unlimited-way
[^mu-relaunch]: Marvel, "Dive Into the All-New, All-Different Marvel Unlimited". https://www.marvel.com/articles/comics/all-new-all-different-marvel-unlimited-app-relaunch-announcement
[^mu-notes]: Marvel Help, Marvel Unlimited Release Notes and Updates (JS-rendered; content taken from a search-engine excerpt, *not directly verified*). https://help.marvel.com/hc/en-us/articles/33642908176020-Marvel-Unlimited-Release-Notes-and-Updates
[^mu-next]: *(secondary)* APKMirror release notes for Marvel Unlimited 7.65.0 / 7.66.1 (Next In Series / Next In Reading Guide; CR shelf). https://www.apkmirror.com/apk/marvel-comics/marvel-unlimited/marvel-unlimited-7-66-1-release/ . "Smart Panel" also from search excerpt only, *unverified*.
[^mu-sunset]: Marvel, Marvel Unlimited Sunset FAQ (sunset 12/7/2026; import to Marvel Comics app launching 11/16/2026). https://www.marvel.com/unlimited-sunset
[^dc-app]: DC Universe Infinite, App Store listing (Dive Back In removal, follow series, offline, landscape). https://apps.apple.com/us/app/dc-universe-infinite/id1329018000
[^kdp-create]: Amazon KDP, Prepare Comic and Kids' eBooks with Kindle Create (auto-detect + manual panels, reading direction, facing pages). https://kdp.amazon.com/en_US/help/topic/GJMRD9F78MS9F43R
[^cx-guided]: ComiXology support, "What is comiXology's Guided View technology" (legacy URL; domain no longer resolves). https://support.comixology.com/customer/portal/articles/768035-what-is-comixology-s-guided-view%E2%84%A2-technology-
[^kindle-sync]: Amazon Help, How to Sync Your Kindle Across All Devices (furthest page read). https://www.amazon.com/gp/help/customer/display.html?nodeId=GDCAMDFMC2LZP6BR
[^plex-lib]: Plex Support, Library server settings (Weeks to consider for Continue Watching = 16, max items, season premieres, played threshold 90%). https://support.plex.tv/articles/200289526-library/
[^plex-forum]: *(secondary)* Plex Forum threads on On Deck → Continue Watching merge and "Remove from Continue Watching". https://forums.plex.tv/t/on-deck-continue-watching-recently-added/666191 · https://forums.plex.tv/t/add-a-show-back-to-continue-watching/738875
[^pb-app]: Paperback, App Store listing. https://apps.apple.com/us/app/paperback-comic-manga-reader/id1626613373
[^mh-reader]: Mihon docs, Reader settings. https://mihon.app/docs/guides/reader-settings
[^mh-strings]: Mihon source, base `strings.xml` (split wide pages, skip read/filtered/duplicate chapters, chapter transition). https://github.com/mihonapp/mihon/blob/main/i18n/src/commonMain/moko-resources/base/strings.xml
[^mh-hist]: Mihon source, History dialogs (remove read date). https://github.com/mihonapp/mihon/blob/main/app/src/main/java/eu/kanade/presentation/history/components/HistoryDialogs.kt
[^mh-lib]: Mihon docs, Library FAQ (incognito pauses history). https://mihon.app/docs/faq/library
[^mh-readme]: Mihon README. https://github.com/mihonapp/mihon
[^mh-track]: Mihon docs, Tracking (enhanced trackers: Komga two-way, chapter-level). https://mihon.app/docs/guides/tracking
[^mh-upcoming]: Mihon docs, Upcoming. https://mihon.app/docs/faq/updates/upcoming
[^mh-smart]: Mihon docs, Smart updates. https://mihon.app/docs/faq/updates/smart
[^yac-home]: YACReader homepage. https://www.yacreader.com/
[^yac-2]: YACReader, "YACReader 8.0 + YACReader 2.0 for iOS". https://www.yacreader.com/39-yacreader-2-0-ios-yacreader-8-0-desktops
[^yac-8]: Same release post (reading lists, double page auto, spread detection).
[^yac-src]: YACReaderLibrary server v2 controllers (incl. `synccontroller_v2`, `readingcomicscontroller_v2`). https://github.com/YACReader/yacreader/tree/develop/YACReaderLibrary/server/controllers/v2
[^ab-read]: Apple Support, Read books in the Books app on iPhone (Home Continue, Want to Read, My Samples, Finished, suggestions). https://support.apple.com/guide/iphone/read-books-iphc1af7c57/ios
[^ab-remove]: Apple Support, Delete books or audiobooks from your Mac in Books ("Remove from Continue"). https://support.apple.com/en-asia/guide/books/ibks67184a3a/mac
[^komic]: Komic, App Store listing. https://apps.apple.com/app/id6744676973
[^codex]: Codex README. https://github.com/ajslater/codex
[^pse]: OPDS Page Streaming Extension 1.2 spec. https://github.com/anansi-project/opds-pse/blob/master/v1.2.md
[^cbl-repo]: DieselTech CBL-ReadingLists. https://github.com/DieselTech/CBL-ReadingLists
[^cbl-count]: Count of `.cbl` files (~1,704) from the repo tree via GitHub API, 2026-10.
[^cbl-sample]: Sample CBL (Boom Studios Hellraiser 2011–2014), showing `Book Series/Number/Volume/Year` + `Database Name="cv"`. https://github.com/DieselTech/CBL-ReadingLists/tree/main/Boom/Characters/Hellraiser
