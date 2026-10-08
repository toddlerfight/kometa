# Reader + On Deck — spec (2026-10-07)

Authoritative model, agreed in a one-question-at-a-time design session
(revised 2026-10-08: every series is a Kometa series; pull list is a switch). Prior art
and citations live in [`reader-research.md`](reader-research.md); this file is the
decisions. **No code is written against this yet.** Next step is a Pencil mock-up of
On Deck and the reader view (`kometa.pen`), then build.

## Core principle

**Kometa replaces Komga as the reader.** Today Kometa is the shopping list (98
tracked series) and Komga is the bookshelf (915 series / ~7,250 books) — and every
handoff between them (scan, series list, book list, read link) is a place for lag
and bugs. After this, Kometa owns the shelf, the reading, and the getting.

## Shape

1. **Replace Komga — side by side first.** Ship the reader with Komga still running;
   "Read" opens Kometa's reader, "Open in Komga" stays as a fallback. One-time
   import of Komga read progress (finished + in progress) on the day the reader
   becomes primary — a clean handover, not an ongoing sync. Retire Komga once
   Kometa generates its own covers and the fallback has gone unused for a while.
2. **Two clients, one server.**
   - **A — Kometa web (first).** The existing app, full-featured: acquisition,
     settings, activity **and** the reader. Used on iPad/phone as a home-screen web
     app. "Open in Komga" becomes "Read".
   - **B — native iOS reader (second).** A *standalone reader*: pulls books from the
     Kometa server, reads offline, syncs progress back. **Never does acquisition.**
3. **The server does everything.** Clients never fetch or store on their own behalf.
   The API is **device-neutral from day one**: per-page streaming (A), whole-book
   download and offline-progress sync (B), even though A needs neither of the
   latter yet.
4. **One reader now, more later.** All reading state (progress, dismissals,
   windows, thresholds) is keyed by a **reader id** that is always the single user
   for now. No login, no profile picker.
5. **Tailscale only.** No public exposure; being on the tailnet is the auth.
   HTTPS via the tailnet (needed for home-screen web-app behaviour).
6. **The whole shelf — every series is a Kometa series.** *(Revised 2026-10-08.)*
   Every series folder under the comics root becomes a full Kometa series:
   organised, matched to LOCG, issue list, trades, arcs, variant covers.
   **Adding** a series and **pulling** it are separate: the **Pull list** is an
   on/off switch inside a series and the ONLY thing that means "actively
   download new and missing issues". See "Every series is a Kometa series" below.
7. **OPDS — after the web reader.** OPDS 1.2 + Page Streaming Extension so existing
   native readers (e.g. Panels) can browse, stream and download from Kometa. A cheap
   test of whether an existing app covers what B was for; if progress sync is the
   gap, that's learned before committing to B.

## Navigation

- **Library** — one library, **all series**. Every card has its owned/total count;
  **amber only for pull-list series with gaps**, grey for everything else (a run you
  own 12/40 of isn't "missing" unless you've asked for it). **Missing** = gaps in
  pull-list series only. Filter chips: **Tracked** (= on the pull list) and
  **Reading**. Calendar sort unchanged.
- **On Deck** — a **new, separate section** (alongside Library, Pull List, Activity):
  where reading happens. Library stays the tracking/acquisition view.

## Every series is a Kometa series

Agreed 2026-10-08, superseding the step-2 "untracked shelf" tier (the plain
shelf page built then is transitional and retires).

1. **Import.** Each shelf folder without a series becomes one, **pull list off**.
   Folders that appear later are added the same way, automatically.
2. **LOCG matching — confident only.** A folder is auto-linked when the match is
   clear (title, publisher and years agree, exactly one candidate). Anything
   unclear becomes a series *without* LOCG — still listed, still readable from
   its files — and lands in a **Needs matching** list where you pick the run.
   A wrong match costs more than no match.
3. **Pull list = an on/off switch** (the Settings toggle component), not a
   one-shot button. **On:** new releases and missing issues actively searched
   and downloaded, synced 3x/day as now. **Off:** a weekly check for new issues
   and LOCG changes, spread across the week; opening a series only refreshes it
   if its data is older than a week. Turning it **off** cancels that series'
   queued searches; anything already downloading finishes.
4. **First run is throttled** in the background over a few hours so LOCG isn't
   hammered and the pull list keeps working; series appear as they're matched.
5. **Files are left as they are.** Kometa already parses issue numbers from
   messy names. Tidying existing files (canonical names, CBR→CBZ, duplicates)
   is a **real but separate task, later**: dry-run report first, then series by
   series — renames churn Komga books and Kometa's path-keyed progress.

## Metadata sources: Metron first, LOCG at a trickle

Agreed 2026-10-08, after LOCG began challenging Kometa (Cloudflare) under
~800 calls/day. Probe of the library (memory: reference_metron_coverage):
Metron found 27–28/30 pull-list series and 13/20 shelf series, had every one
of this week's releases, but lags far-ahead solicitations (Midnight X-Men: #1
only; LOCG had #2 and #3).

1. **Metron is the primary source** — a documented API built for tools like
   this (basic auth, ≤30 requests/minute, honoured with a global rate limiter
   and Retry-After backoff). Used for: **matching** shelf series, **issue
   lists** and back catalogue, the weekly **what's new** check, and (added
   2026-10-08) **issue details and variant covers** — its issue record carries
   the description, credits and every variant with an image, so the modal's
   Details and Variants tabs ask Metron first and LOCG only for issues Metron
   lacks (`kometa/issue_meta.py`; one request per issue, cached a week).
2. **LOCG becomes a trickle** for what only it has: **far-ahead
   solicitations** and community data. Never a bulk job; the 3-hour backoff
   on any refusal stays. No routing around its bot protection.
3. **Matching** tries Metron first, then LOCG. Titles are searched as written
   AND with folder-style " - " turned into ": " (shelf hits 8→13 in the probe).
   Confident-only rule unchanged: one candidate agreeing on title, publisher
   and year, else Needs match.
4. Credentials live in Settings (DB config), not the host .env.

## On Deck

Four sections. Each is its own row/area (layout is a mock-up question).

### Currently reading
- Books with partial progress, most recently read first, showing the page you're on.
- **Leaves automatically after 30 days untouched** — no read state is changed.
  Comes back if the series gets a new issue.
- **Dismiss** ("not now"): hides it without marking read or unread; **undoes itself**
  the next time you read that series.
- **No samples mode.** You sample *because* you don't know yet — don't make the
  reader predict it before opening a #1.

### Next
- The **next unread owned book** in series you're reading: you've finished at least
  one, nothing in progress. "Next" means first unread by Kometa's own issue order
  (it already owns numbering), not "number + 1".
- Must skip books already read and must not land on a trade that reprints singles
  already read, or a duplicate edition.

### Coming soon — its own row
- Series you're **reading and caught up on** whose next issue **isn't here yet**:
  not released, released-but-not-downloaded, or downloading.
- Labelled truthfully from Kometa's real data: *Out Wed*, *Downloading*, *Not
  owned*. (The Pull List remains the place for *everything* tracked with a date.)

### Suggestions — both kinds, labelled separately
- **From your shelf** — unread series you already own (~7,200 unread books), picked
  from creators/series you've finished. Readable instantly.
- **Worth getting** — new series you don't own, with **Track**. Builds on the
  `kometa-recommend` creator-based engine.
- Creator data: the shelf's `ComicInfo.xml` is near-universal on CBZs but rarely
  carries creators (sample: 23/23 had the file, 3/23 named a writer) — **creators are
  backfilled from LOCG** (Kometa already fetches issue credits there).

### Reading lists
- **First:** Kometa's **arcs/storylines** surface as reading lists, and **CBL import**
  (ComicRack format; a community repo holds ~1,700 lists with ComicVine IDs).
- **Both with "get missing"** — a list issue you don't own is an *acquisition*, not a
  dead gap. No plain reader can do this.
- **Later:** hand-made lists ("add to list").

## Reader

- **Context travels with the book.** Opened from a reading list → "next" follows the
  list; otherwise the series.
- **End of book:** an end screen with the next book's cover and a **countdown that
  auto-loads it** (Netflix-style); **any tap cancels**. The countdown only starts on
  the end screen (never pulls you off the letters pages) and only when the next book
  is downloaded — otherwise the screen states its status (*Out Wed*, *Downloading*).
  From a list: the list's next is primary, *next in series* secondary.
- **Finished** = within the **last 3 pages**, or reaching the end screen, or manual
  **Mark as read**. (Pages-from-end, not a percentage: backmatter is a roughly fixed
  number of pages, and 90% means wildly different things for a 36-page issue and a
  280-page omnibus. The motivating case: an issue stuck "in progress" at 35/36.)
- **Layout:**
  - Portrait iPad — single page.
  - Landscape iPad — **two-up**, **cover alone**, **wide pages (spreads) alone**, plus a
    **shift-by-one** control for files whose pairing is off.
  - Phone — always single; wide pages fit-to-screen, rotate for bigger.
- **No panel/guided view** for now (publisher-authored or experimental ML elsewhere).
  Revisit for B on phone if needed.

## Server (technical calls)

- **Page streaming** from the archive (nearly all CBZ — sample: 23/24), **resized to
  the requesting device's resolution** and recompressed; full-size rips run ~10 MB
  per page.
- **Page cache on the server's local disk**, so reading doesn't depend on the NAS
  share staying responsive (it times out under heavy NAS I/O).
- **Progress writes are explicit** (not inferred from page fetches) and **stale writes
  are rejected** (a write older than the stored progress loses) — the safe base for
  B's offline queue.
- **Covers generated by Kometa** from each book's first page — prerequisite for
  retiring Komga.

## Explicitly out of scope (for now)

- Panel / guided view.
- Samples mode / incognito reading.
- Hand-made reading lists (comes after arcs + CBL).
- Public internet access / authentication.
- Tidying existing shelf files (separate task, later — see above).
- RTL / manga mode and vertical webtoon scroll (Western shelf; webtoon rips are
  already rejected at acquisition).

## Open — for the mock-up stage

Layout and visual decisions are deliberately not made here: On Deck row order and
density, card design per row, reader chrome (scrubber, controls, tap zones),
end-screen design, dismiss affordance. Research patterns to draw on are in
`reader-research.md` §2.
