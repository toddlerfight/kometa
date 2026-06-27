# Arc / Storyline model — spec (2026-06-27)

Authoritative model, agreed in conversation. **Supersedes `story-arc-model.md`**
(which documents earlier, wrong-shaped attempts). No code is written against this
yet — this is the lock-down before building.

## Core principle

**Entry is the ARC, not the series.** You search a *storyline*; the system resolves
it to the run it belongs to and backfills that series. Never series-first, never an
auto-generated flood of guessed arcs.

The inversion that earlier builds kept getting backwards:
- WRONG: add a series → auto-discover its arcs.
- RIGHT: **search an arc → backfill its series.** Search-down, not series-up.

## The flow

1. Search a storyline — "Knightfall."
2. The result shows the **main series it belongs to** — Batman (1940) — i.e. **the
   run the storyline ORIGINATES in** (its first issue).
3. **Follow it → the main series is added** as a *normal* series: Issues / Trades /
   **Arcs** tabs. Adding is **tracking only — nothing downloads.**
4. **Only the main series is added up front.** The other participating runs
   (Detective Comics, Shadow of the Bat, Showcase '93) are created **later, lazily**
   — only at the moment you actually *get* their issues.
5. The series' **Arcs tab** lists its storylines, **scoped to that run's volume** (so
   Batman 1940 shows its arcs; Batman 2025 shows its own, not the 1940 canon).
6. Click an arc → its **reading order** (the list of books). **Cross-title
   participation is highlighted** (this arc reaches into Detective, Shadow of the
   Bat, …).
7. From there: **get all, or get individual issues** — a *separate action* from
   adding.

## Rules

- **Adding ≠ getting.** Add = track. Get = download. Two different things; getting is
  its own whole feature (see below — out of scope for this spec).
- **A series created via an arc holds ONLY that arc's issues**, not the full run.
- **Issues always live under their own series** (background-created, thin, as needed).
- **Discovery is volume-scoped** — a series surfaces only *its* run's arcs.

## Surfacing (UI) — like the rest of the app

Arcs must **look and behave like everything else in the interface** — no bespoke,
clunky, or dead-end treatment.

- In a series' Arcs tab, each arc is a **clickable, navigable** element in the app's
  existing visual language — the same card / tile / row patterns used for series,
  issues, and trades. Not a special widget.
- Clicking an arc **navigates** to it — and it **just displays the arc and what's
  included**: the reading order (its books), cross-title participation highlighted,
  per-issue owned / covered / missing status.
- It reads and navigates exactly like browsing issues or trades. No scary lying
  counts, no orphan screens, no "what am I looking at."

## Ownership — two independent signals

An issue is never a single owned/not boolean. It carries two orthogonal facts:

- **owned** — the single is on disk. **Resolved by scanning the disk folder — the
  folder is the ONE and ONLY ownership source. Komga is never an ownership source
  (thumbnails/metadata only).** Untouched by anything else.
- **covered** — a trade you own (also a file on disk) collects this issue.
  **Computed, not stored.**

"Do I have this to read?" = **owned OR covered.** Per-issue display:
```
✓    own the single
◆    it's in a trade you own
✓◆   both
○    neither — genuinely missing
```
Counts read **owned / covered / missing** — never a binary that lies (no "0/23"
while you own the whole collection).

You **never lose the singles dimension**: getting Batman #491 as a floppy flips its
*own* `owned` flag, completely independent of any trade.

## Trades ↔ arcs

- **A trade COLLECTS issues; an arc IS issues.** "Does this trade cover this arc?" is
  **computed from issue overlap, not hand-tagged.**
- Surfaced both ways: on the trade → "Covers: Knightfall"; on the arc → "Collected
  in: [this trade]."
- The derivation handles the messy shapes for free:
  - an **omnibus** collecting three arcs → covers all three;
  - an arc **split across two trades** → each shows its slice;
  - your **25th-Anniversary Knightfall set** → a trade of Batman (1940) that computes
    as covering the whole arc → the arc reads "collected ✓, via this trade."
- **Vintage skews to trades.** Old arcs are far more likely owned/gotten as a
  collected edition than as singles → the *get* path should **prefer the covering
  trade for old arcs.**

## The hard work (the effort, not the model)

- **Resolving each trade's CONTENTS** (which issues it collects). ComicVine's
  collected-edition volumes carry an issue list, so it's gettable — but it's real
  work per trade. This is the data the `covered` signal + coverage computation both
  depend on.
- **The "get" / acquisition path is a separate feature** — singles vs covering trade,
  old-arc → trade default, GetComics/Usenet/Torrent cascade. Out of scope here; this
  spec is about *modelling and surfacing*, not downloading.

## Resolved (was open)

- **Main series = the run the arc ORIGINATES in.** Its first issue — ComicVine's
  `first_appeared_in_issue`. Knightfall's first issue is Batman #491 → Batman (1940).
  *That origin run* is what gets added. No "most-represented title" heuristic — it
  simply originates somewhere, and that's the series. Cross-title participation
  (Detective, Shadow of the Bat) is figured out from *within* the arc's reading order
  and grabbed only if/when you ask for it. (We already pull `first_appeared_in_issue`
  for volume-scoping, so the data's in hand.)
- **The arc lives under its main (origin) series' Arcs tab — one home**, even though
  its reading order reaches into other runs.
- **Recognising an owned collection** is not a separate decision — it folds into the
  trade-contents resolution (resolve what a trade collects → compute coverage). Same
  machinery, no extra call to make.
