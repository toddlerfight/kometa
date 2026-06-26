# Story Arc model

The abstraction that resolves event/collection content (Knightfall, Knightquest,
KnightsEnd, Metal). Arrived at after the torrent + ComicVine work exposed that the
acquisition-needing content (vintage events) is exactly what the series-centric UI
couldn't add cleanly.

---

## ★ CURRENT STATE (2026-06-26) — authoritative summary

The model below evolved through several revisions; this is where it landed and what
is LIVE on the NAS (branch `torrent-integration`, v=108). Read this first; the rest
is the historical trail.

**An arc is a LENS, not a container.** It owns no folder. It's a cross-title
reading-order overlay that references issues living in their own series, plus a
"grab the storyline" action and a Komga readlist. Arcs do not appear as library
cards — they're reached through a series' Arcs tab and their own detail page.

**Discovery — arcs arrive WITH a tracked series** (like its issues + trades); no
separate "track an arc" step:
- **PRIMARY = ComicVine.** `comicvine_client.discover_arcs(title)` = `search_arcs`
  filtered by the quoted series prefix CV bakes into arc names (`"Batman"
  Knightfall`) — structured, repeatable, precise (each carries a `cv_arc_id`), and
  noise-free without extra calls. Rich for established characters (Batman: ~28 arcs
  incl. Knightfall, which Wikipedia lacked).
- **FALLBACK = Wikipedia** (`wikipedia_client`, MediaWiki API, keyless), fires only
  when CV catalogs nothing (brand-new / indie — e.g. Absolute Batman). Parses the
  collected-editions wikitable to {name, issue range}. Messy/inconsistent by nature
  (prose tables) — hence fallback only.
- Cached per series ~7 days (`arc_discovery_cache`). Merged with already-tracked
  arcs (which carry live owned counts) in `GET /api/series/{id}/arcs`.
- **GCD scratched**: its REST API exposes NO story-arc endpoints; arcs live only in
  the full MySQL dump (heavy, uncertain). Not viable near-term.

**Click-to-populate** (`POST /api/series/{id}/arcs/populate`): a CV arc → `_add_arc`
(precise cross-title order); a Wikipedia arc → `_create_range_arc` (single-title
lens over the VIEWED series, so a re-release like TWD Deluxe resolves ownership
against what you actually own, not CV's original volume).

**Reading order + ownership** (`_resolve_arc_ownership`, Brick A/4):
- CV's `get_issues_meta` batch-resolves each issue's REAL number + exact volume
  (fixes Showcase '93 → #7/#8; Batman → vol 796 (1940), not 2016). Stored as
  `cv_volume_id` on `arc_issues`.
- Ownership two ways: singles matched cross-title vs each issue's own Komga series;
  AND collected-edition detection (`_arc_collection` — a Komga series named after
  the arc). Arc page shows a ◆ collected chip + per-row "in collection".

**Fulfill-the-arc** (Bricks B/C): adding/populating an arc auto-tracks its
participating runs (`_track_participating`) — one tracked series per distinct
`cv_volume_id`, **pull-list OFF** so broad sweeps skip them (the broad sweep gates
on `monitor_status='monitored' AND on_pull_list=1`). Each run's card shows the arc's
slice (Batman 1940 → 0/10). The arc page's **Fulfill arc** button queues ONLY the
arc's missing issues into their runs (`/fulfill`, bulk-queued). Trades route to the
arc's main series (`arc.main_series_title`). Readlist builds cross-title (singles →
collection volumes → partial).

**Key modules:** `arc.py` (title logic), `comicvine_client.py`,
`wikipedia_client.py`, plus arc paths in `main.py` / `db.py`. **New tables:**
`arc_issues` (+ `cv_volume_id`), `arc_discovery_cache`. **New `tracked_series`
cols:** `kind`, `cv_arc_id`, `cv_volume_id`.

**Known rough edges (deferred polish):** CV pads some arc issue lists with
reprints/foreign editions (cosmetic); Wikipedia includes compendiums alongside
granular arcs; first Arcs-tab load on a CV miss does a synchronous ~2s Wikipedia
fetch. Test arcs accumulated in the live DB during development (Knightfall 64,
Knightquest 63, Days Gone Bye 70, Hush 72) — legit but disposable.

---

## ⚑ REVISED MODEL — arc as a LENS, not a container (2026-06-26)

**This supersedes the "arc as a first-class container" approach below (Phases B–E
as BUILT).** After building it end-to-end and testing in the UI, the model is wrong
in one structural way: an arc was made a `kind='arc'` tracked_series with its OWN
folder that grabs the collected trade into itself. It shouldn't own anything.

**Corrected model — series-first:**

```
SERIES   the tracked, folder-owning unit. Issues → their folders;
         collected editions → the series' Trades tab (EXISTING machinery).
ARC      owns NOTHING — a cross-title reading-order OVERLAY + grab-trigger +
         readlist-builder. It only references things that live in series:
           • reading-order rows → issues in their real series' folders
           • its collected editions ARE trades of the arc's MAIN series
             (Knightfall = a trade of Batman 1940, in Batman's Trades tab —
             NOT an arc folder)
           • "grab the storyline" → routes the trade grab to the main series'
             Trades (existing flow); singles, where gettable, → their series
           • "build readlist" → as already built (arc order → Komga books)
```

- **Main-series heuristic** (the "not clean" bit, resolved): the arc's lead title +
  most-represented title in its issues. Knightfall → "Batman Knightfall", 10/23
  issues are Batman → **Batman (1940)**; both signals agree. Crossovers with no
  clear lead are fuzzier — defer or let the user pick.
- **Discovery flips series-first:** searching an event ("knightfall") offers to
  track the MAIN series if untracked, and surfaces the arc as the way to
  grab/navigate it. The series detail page gets an **Arcs tab** listing the events
  it participates in. The same arc appears under every participating series.
- **Arcs are NOT top-level library cards** — reached via a series' Arcs tab + their
  own detail page; they don't clutter the grid.

**Rework progress (2026-06-26):**
- ✅ brick 1 — `arc.py` `main_series_title()` (most-represented source title).
- ✅ brick 2 — `_add_arc` routes the trade to the MAIN series
  (`db.find_series_by_title`); arc no longer grabs into its own folder.
- ✅ brick 3 — series **Arcs tab**: `GET /api/series/{id}/arcs` (`db.get_all_arcs`
  + `arc.arc_includes_series`), `arc_count` on `get_series`, frontend tab +
  `_loadArcsPanel`/`_arcRowHtml`. Verified: Batman → Knightfall + Knightquest.
- ✅ brick 5 — `list_series` filters `kind='arc'` → arcs gone from the library grid.
- ✅ brick 4 — ownership resolution, two ways: (A) singles matched cross-title vs
  each issue's own Komga series (`_resolve_arc_ownership` → `set_arc_ownership`);
  (B) collected edition detected via `_arc_collection` (the Komga series named after
  the arc, tracked or not). Arc page shows a ◆ collected chip + banner + per-row "in
  collection"; readlist rebuilt cross-title (singles-exact → collection volumes →
  partial). Also fixed `create_or_update_readlist` (Komga `?search` unreliable →
  dupe-name 400 on rebuild). Verified: Knightfall (2 vols) + Knightquest (1 vol)
  both detect + readlist.
- brick 6 — moot: the lens model turned the old standalone test-arcs into
  legitimate, reachable arcs.

**Lens rework COMPLETE.** Deployed live (v=103), branch `torrent-integration`.
Loose ends (optional): vintage arcs show "0/N singles" + the collected banner (it's
honest — owned as a collection, not singles); a collection that isn't separately
tracked (e.g. Knightquest) shows no banner link; `build_arc_readlist` fetches the
Komga library twice (minor perf). Next big step: merge `torrent-integration` → main.

**Rework impact (mostly subtraction):**
- KEEP: `comicvine_client.search_arcs`/`get_arc_issues` (Phase A); the `arc_issues`
  table (the reading order); the arc reading-order PAGE (Phase D); the readlist
  button + `create_or_update_readlist` (Phase E).
- REWORK: the arc as a folder-owning `tracked_series` (reconsider whether it's a
  tracked_series at all vs a lighter entity referencing series); the add flow →
  series-first; acquisition → route the trade to the MAIN series' Trades, not an
  arc folder.
- NEW: series **Arcs tab** (discovery), main-series resolution, demote arcs from
  the library grid.
- **Cleanup:** the `kind='arc'` standalone test series + their folders in the live
  DB ("Batman Knightquest- The Crusade" #63, the "Batman Knightfall" arc the user
  added).

Everything below documents the as-built container model — kept for reference; the
section above is the target.

---

## Why an arc isn't "a section of a series"

Events span MULTIPLE titles — Knightfall = Batman + Detective + Shadow of the Bat
+ Showcase. There is no single parent "series" to hang it on (ComicVine literally
returns the collected editions as separate top-level results). So model the arc as
its own entity that *cuts across* series.

```
 SERIES (Batman 2016) ──► issues + trades            (one title)
 ARC    (Knightfall)  ──► issues ACROSS titles       (Batman + Detective + …)
                          = the reading order, for free
```

## Data source: ComicVine story arcs

- `/api/story_arcs/?filter=name:Knightfall` → arc id (e.g. Knightfall = 40761).
- `/api/story_arc/4045-<id>/?field_list=issues` → the cross-title issue list IN
  ORDER. VERIFIED: Knightfall arc returns 23 issues interleaving Batman + Detective
  — ≈ the manifest built by hand in [knightfall-readlist.md], generated for free.
- **Gotchas:** `count_of_isssue_appearances` is unreliable (reads 0) — use the
  `issues` array length. CV splits some events into multiple arcs ('"Batman"
  Knightquest: The Crusade' + '…The Search') — group by name prefix to present one
  logical event, or track the parts.
- CV key already in hand (found in ~/code/arr-stack/comics-*.py), set as
  `cv_api_key` in Kometa config. Client built: `comicvine_client.py`.

## Pages (mockup: docs/arc-mockup.html)

- **Series page** gains an `arcs` tab — lists the arcs this title takes part in
  (each ◆ links to the arc's own page). Same arc appears under every participating
  series.
- **Arc page**, two tabs:
  - **reading order** — the cross-title issue list; SOURCE column changes title
    row-to-row; per-row status (owned / acquiring·torrent / missing). Actions:
    Grab All Missing, Build/Rebuild Komga Readlist.
  - **trades** — collected editions that reprint the arc.

## "Build Komga Readlist" button (confirmed wanted)

The arc IS an ordered list, so this is one click and self-maintaining:
```
for each arc entry in order → resolve its Komga book id (owned single, or the
trade that covers it) → POST /api/v1/readlists {name, ordered:true, bookIds:[…]}
```
Automates exactly the manual Knightfall Part-1 readlist. Re-run after grabbing more
→ it re-syncs.

## Trades in arcs (the crux)

A trade reprints a SPAN of the arc, not the whole thing. The arc holds both layers
and maps them (trade→issue-span comes from the CV collected-edition's issue list —
no hand-mapping):

```
 Knightfall (23 issues)
  ├ 01–11  covered by ▸ Vol 1: Broken Bat
  └ 12–23  covered by ▸ Vol 2: Who Rules the Night
```

Falls out of that mapping:
1. **Ownership** is satisfied by EITHER layer — an issue counts owned if you have
   the single OR a trade that reprints it. (We currently own the Knightfall trades,
   zero singles → arc reads 23/23.)
2. **Acquisition** prefers the efficient layer — "Grab All Missing" grabs the
   trades (2 files) over 23 singles; singles only for gaps a trade misses. Same
   GetComics → Usenet → Torrent cascade.
3. **Readlist** builds at whatever granularity you own — trades (2 entries),
   singles (23), or mixed — always in arc order.
- Reading-order rows show a "▸ covered by Vol N" marker; trades tab shows each
  trade's span.

## Supersedes

- The "CV volume as a single trade" plan (ComicVine spec Phase C) — replaced by the
  arc as the organizing unit.
- The hand-built reading-order manifest — the arc generates it.
- Library fragmentation into many tiny single-trade "series".

## Phased build

Tracer-first, mirroring the torrent build. Each phase independently testable;
the acceptance test (add Knightquest → grab → torrent → readlist, all UI) is the
finish line.

**Decision to settle in Phase B (architectural fork):** new `tracked_arc` table
vs. arcs-as-special-`tracked_series` (`kind='arc'`). Special-series reuses the
acquisition/folder/trade machinery but bends "a series = one title" (arc issues
span titles). Separate table is cleaner but more new code. Lean: special-series
with a cross-title issue list, reusing the queue/cascade as-is.

### Phase A — ComicVine arc data (tracer)
`comicvine_client.search_arcs(query)` + `get_arc_issues(arc_id)` (use the `issues`
array, not the junk count; resolve each issue's source series+number).
*Test:* Knightfall (40761) → 23 cross-title issues in order. Read-only, low risk.
Proves the data before any model work.

### Phase B — Arc entity + storage
Schema (per the fork above): cv_arc_id, title, publisher, folder, the ordered
cross-title issue list, the trade list + coverage spans. DB add/get/list helpers +
migration.
*Test:* create an arc record, round-trip its issues + trades.

### Phase C — Add-Arc flow
Surface arcs in the Add search (CV arc results, source=`arc`); add → create the
arc + populate cross-title issues + trades + compute trade→issue spans from the CV
collected-edition issue lists.
*Test:* add Knightfall arc → issues + trades populate, spans computed.

### Phase D — Arc UI
Series `arcs` tab (arcs this title takes part in); arc detail page (reading-order +
trades tabs) with per-row status and "▸ covered by Vol N" markers. Bump ?v=.
*Test:* navigate the mockup's three views for real.

### Phase E — Acquisition + Komga readlist (the acceptance test)
"Grab All Missing" on an arc → prefers trades, falls through the existing
GetComics→Usenet→Torrent cascade (P1–P5, live). "Build Komga Readlist" → arc order
→ resolve Komga book ids → POST /api/v1/readlists.
*Acceptance test:* add **Knightquest** in the UI → grab → watch it torrent →
readlist appears in Komga. Fully UI-drivable — the thing that started all this.

### Phase F — Trades-in-arcs polish
Ownership satisfied by single OR covering trade; readlist granularity (trades vs
singles vs mixed); span markers in the reading order.

### Separate, small: finish Settings UI
Surface qBit / Prowlarr / ComicVine in Settings (fields + Test buttons + the
GET /api/config returns + /api/test/* + _INTEGRATION_KEYS). `ConfigRequest` already
accepts the fields. Independent of the arc work — do anytime.
