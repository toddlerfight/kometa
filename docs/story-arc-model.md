# Story Arc model (design — not yet built)

The abstraction that resolves event/collection content (Knightfall, Knightquest,
KnightsEnd, Metal): a **Story Arc** as a first-class trackable entity, parallel to
Series. Arrived at after the torrent + ComicVine work exposed that the
acquisition-needing content (vintage events) is exactly what the series-centric UI
couldn't add cleanly.

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
