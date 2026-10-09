# Recommendation presentation patterns — research brief for Kometa

Date: 2026-10-09. Scope: how media apps *present* recommendations (layout, row semantics, reason labelling, owned/unowned mixing, card actions, negative feedback), distilled into a proposal for Kometa's On Deck page and series page. Sources are primary where they exist (engineering blogs, help centres, release notes, App Store listings); teardowns are flagged.

## 1. Comparison table

| App | Home rows | Item-page rows | Reason labelling | Owned vs unowned | Card actions | Negative feedback |
|---|---|---|---|---|---|---|
| Netflix | Continue Watching, Top Picks, Top 10 (daily, country), Popular, "Because you watched X", personalised genre rows, My List; row *order* is personalised, not templated ([homepage](https://netflixtechblog.com/learning-a-personalized-homepage-aa8ec670359a), [Top 10](https://about.netflix.com/news/see-whats-popular-on-netflix)) | "More Like This" (similarity network) ([5 stars pt 1](https://netflixtechblog.com/netflix-recommendations-beyond-the-5-stars-part-1-55838468f429)) | Row title is the explanation; "evidence" (what you watched in that genre) is a first-class ranking feature; per-title % Match badge, "Top 10" badge, double-thumbs "Love this" marker ([thumbs](https://about.netflix.com/news/goodbye-stars-hello-thumbs), [ratings help](https://help.netflix.com/en/node/9898)) | Single catalogue, no distinction | Play, My List, thumbs up / Love / Not for me, more info | Thumbs-down removes title from homepage ("you can still search for it"); remove from Continue Watching |
| Plex | Continue Watching (always on), On Deck, Recently Added (mergeable across libraries), Recently Released, Recently Aired, Rediscover, per-library rows; admin toggles rows per surface (Library Recommended / Home / Shared Users' Home) and drags to reorder ([manage recs](https://support.plex.tv/articles/manage-recommendations/), [big-screen apps](https://support.plex.tv/articles/navigating-the-big-screen-apps/)) | Collections hubs, Cast & Crew, friends' Activity module on the detail page ([activity feed](https://support.plex.tv/?p=46621), [forum](https://forums.plex.tv/t/collections-1-4-1/179876)) | Row names are descriptive, not causal ("Rediscover: shows you started but haven't recently watched") | Discover tab mixes streaming services + server libraries; server rows stay per-library and are grouped by pin order | Play, Add to Watchlist, mark watched/unwatched, share to friend, emoji react | Mute / block friends; hide rows per library. No per-title "not interested" |
| Spotify | Shelves of cards: recently played, "Made for you" (Discover Weekly, Release Radar, Daily Mix), "More like …", "Recommended for you", "New Releases for You"; bandit-ranked ([Home ML](https://engineering.atspotify.com/2020/1/for-your-ears-only-personalizing-spotify-home-with-machine-learning)); 2026 paper calls the shelf title "a promise about why items belong together" ([shelf generation](https://arxiv.org/abs/2607.25823)) | Artist page: "Fans Also Like" from fans' listening habits, artist cannot edit ([help](https://support.spotify.com/us/artists/article/fans-also-like/)) | Shelf title carries the reason; no per-card reason | Single catalogue; saved vs unsaved only via heart state | Play, Save/Like, add to playlist, Not interested (Home feed) | "Not interested" on Home cards; "Exclude from your taste profile" per playlist/track ([help](https://support.spotify.com/us/article/exclude-playlists-or-tracks-from-your-taste-profile/)) |
| Apple TV / Books / Music | TV: Continue Watching (incl. Watchlist items), top charts, new releases, "collections handpicked by experts", "personalized recommendations" rows ([guide](https://support.apple.com/guide/tv/find-things-to-watch-atvb7d7bec9f/tvos)). Books Home: current reads, "personalized suggestions for your next read", Want to Read, reading goals; Store "For You" + Weekly Top 5 built from your library ([guide](https://support.apple.com/guide/iphone/read-books-iphc1af7c57/ios), [apple.com](https://www.apple.com/apple-books/)). Music Home: recently played, playlists made for you, "genres you might like" ([guide](https://support.apple.com/guide/music/musa79bdeff/mac)) | TV item page: episodes, trailers, "related items" | Editorial labelled as such ("handpicked by experts", "staff recommendations"); algorithmic rows labelled "for you" | Library tab is purchases only; Home/Store mix freely; Watchlist is the bridge | Play, Add to Watchlist, Buy/Rent/Get, Want to Read | "Use Play History" toggle + "Clear Play History" (also empties Continue Watching) ([settings](https://support.apple.com/guide/tv/atvbae11a68b)); Music: favourite / "suggest less" |
| Goodreads / StoryGraph / Letterboxd | GR: recommendations by shelf; SG: mood/pace filters, planned "New Recommendations Sections" ([roadmap](https://roadmap.thestorygraph.com/)); LB: no labelled rec section by design, "the whole of Letterboxd is one big organic recommendation engine" ([FAQ](https://letterboxd.com/about/frequent-questions/)) | GR: "Readers Also Enjoyed" sidebar from co-shelving ([GR staff](https://www.goodreads.com/topic/show/17039493-readers-also-enjoyed---what-do-you-need-to-make-your-book-appear-ther), [teardown](https://bookriot.com/goodreads-readers-also-enjoyed-feature/)); LB: "Similar Films" expanding to themes/nanogenres, friends' activity, lists with "% watched" | Social proof as the reason (co-shelved, friends rated) | Owned is not a concept; "read/watched" state greys or hides items ("hide the films you've seen") | Want to Read / Watchlist, rate, log, add to list | Mark watched hides from browse; SG request "exclude non-books from recommendations" |
| Marvel Unlimited / DC Universe Infinite | MU Home: latest releases, curation picks, shortcuts to current reads; "recommended Reading Guides and series based on your current reading and favorites" ([Marvel](https://www.marvel.com/articles/comics/all-new-all-different-marvel-unlimited-app-relaunch-announcement), [App Store](https://apps.apple.com/us/app/marvel-unlimited/id607205403), [teardown](https://www.techradar.com/reviews/marvel-unlimited-review)). DCUI: featured, reading history, latest releases, trending series, "several recommended lists", character "Get to Know" lists ([teardown](https://www.techradar.com/reviews/dc-universe-infinite-review)) | Series page: issue list, creators; creator/character follow pages | Reading Guides *are* the reason ("start here for Krakoa"); curated by named experts | Everything is in the subscription; Library = bookmarks | Read, Add to Library, Download, Follow | None documented |
| Komga / Kavita | Komga: Keep Reading, On Deck, Recently Added/Updated/Released, pinned collections/read lists — zero recommendation logic ([README](https://github.com/gotson/komga)). Kavita: dashboard streams are user-configurable smart filters; the old Recommended tab (On Deck, Quick Reads, Highly Rated, Rediscover, More In Genre) was removed in v0.7.9 "everything can be created as a Smart Filter" ([releases](https://github.com/Kareadita/Kavita/releases)) | Kavita+: external recs on Series Detail, "pull from the server and external series", cards badged **Similar** (tag weights) vs **User-based** (co-reading) ([wiki](https://wiki.kavitareader.com/kavita+/recs-ratings-reviews/), v0.9.1.0 notes) | Kavita+ badge per card is the only per-card reason label in the whole survey | Kavita+ mixes on-server and external in one row; external card opens a preview, not a reader | Read, Want to Read, (external) preview | None |
| League of Comic Geeks | New Comics list sorted by Most Pulled / Rating / Pulled / Not Pulled; "Pick of the Week"; "Community Consensus"; followed creators/characters/publishers; community reading-order lists ([site](https://leagueofcomicgeeks.com/comics/new-comics), [App Store](https://apps.apple.com/us/app/comic-geeks/id874775221)) | Issue page: pull/collect counts, ratings graph, reviews | Raw counts as reason ("N pulled") | Pulled / Collected / Not Pulled are explicit filter states; "Fade Collected" and "Fade Read" dim owned items in lists | Pull, Collect, Rate, Review, Add to list | None (tracking app, not a recommender) |
| Steam / YouTube | Steam: Discovery Queue one-at-a-time; main capsule "based on games they own, games they play, games their friends play, top sellers" ([Steamworks](https://partner.steamgames.com/doc/marketing/visibility)). YouTube: feed | — | Steam: "Because you played", friends who own it, curators | Steam marks owned/wishlisted on the capsule | Wishlist, Follow, Ignore / Not Interested | YouTube: Not interested + "Tell us why", Don't recommend channel, remove from history ([help](https://support.google.com/youtube/answer/6342839)); Mozilla found "Not interested" cut only ~11% of bad recs vs 43% for channel blocks ([TechCrunch](https://techcrunch.com/?p=2402640)) |

## 2. Patterns worth stealing

### Home surface
- **Rows are the unit of personalisation, and the row title is the explanation.** Netflix: "providing a meaningful name for each row … members can quickly decide whether a whole set of videos in a row is likely to contain something they're interested in". Spotify's shelf title is "a promise". A row without a title that states its selection rule is a bug.
- **Task rows before discovery rows, then order the discovery rows by evidence strength.** Netflix moved from a fixed template ("Continue Watching, Top Picks, Popular, then 5 genre rows") to ranking rows by the quality of evidence behind them, with explicit diversity so the page isn't "slight variations of their interests". Kometa's equivalent: pin Continue/Next/Coming soon at the top, then rank discovery rows by how many of the user's own reads support them.
- **Dedupe across the page.** Netflix filters repeats at assembly. A series in "Next" must not reappear in "Suggestions".
- **Only show a row when it has enough items.** Plex's "Continue Watching (if any)" convention; Netflix "Top Picks (if any)". Empty or two-item rows are worse than no row.
- **Cap the page by device.** Netflix generates per-device page sizes (Recall@3-by-4 for a 3-row/4-column viewport). Phone gets fewer rows, not the same rows cropped.
- **Make rows the owner can switch off.** Plex exposes a three-column matrix (library tab / home / shared home) plus drag-to-reorder. Kavita went further and made every dashboard stream a saved filter. For a single-owner app, a per-row visibility toggle is enough.

### Item page
- **Two kinds of "related", kept separate.** Netflix "More Like This" (similarity) is a different row from the collections hub (membership). Kavita+ badges the two generation methods on the card. Kometa already splits Related (same arc / same list / same creators) from "By the same people"; keep that split and label each.
- **Social proof lives on the item page.** Plex shows friends' activity on the detail page with a per-item toggle; Goodreads puts "Readers Also Enjoyed" in the sidebar; LOCG shows pull/collect counts and a rating graph. Community numbers belong next to the thing, not on Home.
- **Reading order as recommendation.** Marvel's Reading Guides and DCUI's "Get to Know" lists are the strongest comic-specific discovery pattern: a curated path, with position. Kometa's reading lists already are this; surface "this series is #4 of 12 in *Knightfall*" on the card.

### Card anatomy
- Artwork is "visual evidence" (Netflix): the cover should be the variant most tied to the reason when possible (the arc's cover, the creator's issue), not a random first issue.
- Badges that survived at scale: a popularity badge ("Top 10"), a confidence marker (double thumbs / % Match), an ownership/progress state (Steam's owned/wishlisted, LOCG's Pulled/Collected, Komga's progress %). Three badges maximum per card.
- Netflix's % Match confused members less than stars, but a personalised score still needs a legend. For Kometa a score is unnecessary; a reason line is clearer.
- Progress belongs on owned cards only (Komga On Deck shows percentage read). Unowned cards show counts instead (issues, pulls).

### Reasons / trust
- Netflix's explicit position: explanations exist "to promote trust" and "we are not recommending it because it suits our business needs, but because it matches the information we have from you". Every discovery row needs a reason the owner can verify against their own shelf.
- Two legible grammars: **row-level** ("Because you read *Saga*") and **card-level badge** (Kavita+ "Similar" / "User-based"). Use row-level for one-signal rows and card-level when a row mixes signals.
- Label editorial and community separately from personal signals (Apple: "handpicked by experts"; LOCG: "Community Consensus"). Never let "popular" masquerade as "for you".
- Name the source when it is external: "LOCG: 1,240 pulling", "Metron credits".

### Feedback loops
- Netflix's thumbs replaced stars because people understood thumbs as "teach the system", not "review for others" (200% more rating activity). A single-purpose, binary, immediate control wins.
- "Not interested" must visibly remove the item from the surface *now* (Netflix: "it will no longer show up on your homepage"). YouTube's version is distrusted precisely because it is weak.
- Ask "why" only after the dismissal (YouTube "Tell us why": already read / don't like). Two reasons suffice for comics: *Already have it elsewhere* / *Not for me*.
- Provide an undo path: YouTube lets you clear feedback; Apple lets you clear play history. A "Hidden" list in Settings is enough.
- Offer a scope control for the dismissal: this series vs this creator (YouTube's channel block is the one that actually works).

### Cold start
- Netflix: optional "pick a few titles", otherwise "a diverse and popular set"; recent engagement supersedes initial picks ([help](https://help.netflix.com/en/node/100639)). Kometa has no cold start for owned signals (the shelf is the taste profile) but does for community data: show community rows only once the pull/collect cache is warm, and fall back to shelf-derived rows.
- Kavita's "Quick Reads" (finished, short) and "Rediscover" (owned, read long ago) are rows that need no external data and no history beyond the shelf.

## 3. Proposal for Kometa

### Owned vs unowned: one visual contract
- **Owned cards** (on the shelf): full-colour cover, progress bar or "x/y read", primary action **Read**. No reason line unless the row mixes signals.
- **Unowned cards** (catalogue only): cover at reduced saturation or with a thin dashed border and a small corner glyph (reuse the design used for "By the same people"), primary action **Track**, secondary **Get** when a specific issue/trade is resolvable. Issue count and community count replace progress.
- **Rows never mix the two by default.** A row is either shelf or catalogue; the eyebrow label above the row title says which ("ON YOUR SHELF" / "NOT ON YOUR SHELF"). Mixed rows are permitted only on the series page's Related, where the ownership glyph does the work.
- Dismissed items leave immediately with a one-line inline "Hidden — undo" that stays until the row re-renders.

### On Deck rows (priority order)

| # | Row title (wording) | Signal | Card | Actions |
|---|---|---|---|---|
| 1 | **Continue reading** | open progress | owned, progress bar | Read |
| 2 | **Next** (next unread issue in series you're current on) | ownership + read state | owned, issue number | Read |
| 3 | **Coming soon** / **Recently released** (existing) | pull list + Metron dates | owned-series, date badge | Get / Read |
| 4 | **Recently added** (existing) | filesystem | owned | Read |
| 5 | **Next on *{list name}*** — one row per reading list with recent reads, max 2 | reading-list adjacency | mixed; "#7 of 12" position badge; owned → Read, gap → Get | Read / Get / Not interested |
| 6 | **Because you read *{series}*** — picks the shelf series with the strongest recent reading; max 2 rows | shared writers/artists + shared arc; row title names the anchor, card chip names the credit ("Jeff Lemire, writer") | catalogue cards, dashed treatment, issue count | Track / Not interested |
| 7 | **Rediscover** | owned, finished or stalled > 180 days, not in rows 1–2 | owned, "last read May" | Read / Not interested |
| 8 | **Quick reads** | owned, complete, unread, ≤ 12 issues | owned, issue count | Read |
| 9 | **Pulling this week** (community) | LOCG pull counts for this week's releases, shelf excluded | catalogue, "1,240 pulling" count, source label "LOCG" | Track / Get / Not interested |
| 10 | **Trending** (community) | ICv2 / LOCG collect deltas | catalogue, source label | Track / Not interested |

Rules: rows 1–4 are task rows and always render when non-empty; rows 5–10 render only with ≥ 4 items; no series appears in more than one row (precedence = table order); phone shows rows 1–6 by default with "More" expanding 7–10; each discovery row has an overflow menu with **Hide this row**.

### Series page rows (priority order)

| # | Row title | Signal | Card | Actions |
|---|---|---|---|---|
| 1 | **Part of *{arc / list}*** (existing Related, split out) | same arc, same reading list | mixed, position badge, ownership glyph | Read / Get |
| 2 | **Also by {writer}** / **Also by {artist}** — one row per primary credit, shelf first | Metron credits | mixed; shelf cards first then dashed catalogue cards | Read / Track |
| 3 | **Readers of this also pull** (community) | LOCG co-pull / co-collect when available | catalogue, count + source | Track / Not interested |
| 4 | **Community** strip (not a row) | LOCG pulls/collects/rating, ICv2 rank | inline stats with source link | — |

### Card anatomy (both surfaces)
Cover (variant matching the reason when known) · title · one-line reason chip (credit name, list position, or community count) · state badge (progress / issue count / "On shelf") · primary action button · overflow (Not interested → why? → scope: this series / this creator).

### Open decisions for the owner
1. **Reason chip wording**: per-card chip on every discovery card, or only on mixed-signal rows (Netflix style, title-only)? Chips cost vertical space on phone.
2. **Dismissal scope**: offer "this creator" as a dismiss scope (YouTube evidence says it is the one that works), or keep dismissal to series only to avoid silently hiding a whole writer's output?
3. **Community row gating**: show "Pulling this week" / "Trending" from day one with a "Community" label, or hold them until LOCG counts cover ≥ 80% of the shelf so the rows don't look thin?
4. **Where "By the same people" lives**: keep it on the series page only (current), or let On Deck's "Because you read" subsume it with the series page keeping the per-credit split?
5. **Hidden list management**: Settings page listing hidden series/creators with undo (Apple/YouTube pattern), or a simpler "Reset suggestions" button?
