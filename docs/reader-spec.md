# Reader + On Deck — spec (2026-10-07)

Authoritative model, agreed in a one-question-at-a-time design session. Prior art
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
6. **The whole shelf.** Kometa indexes **every series folder** under the comics
   root, not just tracked ones. *Tracked* becomes a flag meaning "watch for new
   issues and fetch them", not the ticket into the library.
7. **OPDS — after the web reader.** OPDS 1.2 + Page Streaming Extension so existing
   native readers (e.g. Panels) can browse, stream and download from Kometa. A cheap
   test of whether an existing app covers what B was for; if progress sync is the
   gap, that's learned before committing to B.

## Navigation

- **Library** — one library, **all series** (tracked + untracked). Tracked series keep
  their count / bar / release badge; untracked show cover + title. Filter chips gain
  **Tracked** and **Reading**. The calendar sort already floats current series to the
  top; untracked (no release dates) fall below.
- **On Deck** — a **new, separate section** (alongside Library, Pull List, Activity):
  where reading happens. Library stays the tracking/acquisition view.

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
- RTL / manga mode and vertical webtoon scroll (Western shelf; webtoon rips are
  already rejected at acquisition).

## Open — for the mock-up stage

Layout and visual decisions are deliberately not made here: On Deck row order and
density, card design per row, reader chrome (scrubber, controls, tap zones),
end-screen design, dismiss affordance. Research patterns to draw on are in
`reader-research.md` §2.
