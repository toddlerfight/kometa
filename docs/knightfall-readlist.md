# Batman: Knightfall — Part 1 Readlist Manifest

> **STATUS: PART 1 DONE (2026-06-25).** Komga readlist **"Batman: Knightfall"**
> (id `0QSWSE80BDNAV`, ordered) is live with the 25th Anniversary Edition
> **v01 (Broken Bat, 282p)** → **v02 (Who Rules the Night, 312p)** = the full
> Knightfall arc. Sourced via Usenet (the `25th.Anniversary.Edition.v01-v02`
> NZBGeek repost, 1670d — old enough to have enough par2 to survive Frugal's
> retention where everything newer/older failed). Files at
> `/comics/DC Comics/Batman - Knightfall/`, Komga series `0QSWNR7RBDYJZ`.
> Individual-floppy readlist (the 24-issue interleave below) NOT pursued — the
> trade route landed clean content faster. Issue order preserved below for
> reference / future precision pass.


Working spec for building a Komga readlist that mirrors the **Knightfall** arc
(Part 1 of the 2026 animated trilogy). Acquire against this; fill the Status /
Source / Komga ID columns as issues land; build the readlist from the **Order**
column once coverage is acceptable.

- **Strategy:** Hybrid — chase individual floppies first, backfill gaps with the
  Knightfall TPB Vol 1 / Vol 2 collections.
- **Acquisition pipes:** Usenet (Prowlarr/SAB) first, GetComics fallback.
- **Komga:** `http://$NAS_TS_HOST:8585` (Tailscale) /
  `http://$NAS_HOST:8585` (LAN). Readlist built via `POST /api/v1/readlists`.
- **Status legend:** `TODO` · `GOT` (file landed) · `SCANNED` (in Komga, ID set) · `GAP` (unsourced)

## Trade fallback content map

If a floppy can't be sourced, the issue is covered by one of these collections:

- **Knightfall TPB Vol 1** — Batman #492–497, Detective Comics #659–663
- **Knightfall TPB Vol 2** — Batman #498–500, Detective Comics #664–666, Showcase '93 #7–8, Shadow of the Bat #16–18

---

## Core arc (required — the 22 issues the animation adapts)

| Order | Title / Issue | Knightfall Pt | Story | Status | Source | Komga Book ID |
|------:|---------------|:-------------:|-------|:------:|--------|---------------|
| 1  | Batman #492            | 1  | Crossed Eyes and Dotty Teas   | TODO | | |
| 2  | Detective Comics #659  | 2  | Puppets                       | TODO | | |
| 3  | Batman #493            | 3  | Redslash                      | TODO | | |
| 4  | Detective Comics #660  | 4  | Crocodile Tears               | TODO | | |
| 5  | Batman #494            | 5  | Night Terrors                 | TODO | | |
| 6  | Detective Comics #661  | 6  | City on Fire                  | TODO | | |
| 7  | Batman #495            | 7  | Strange Deadfellows           | TODO | | |
| 8  | Detective Comics #662  | 8  | Burning Questions             | TODO | | |
| 9  | Batman #496            | 9  | Die Laughing                  | TODO | | |
| 10 | Detective Comics #663  | 10 | No Rest for the Wicked        | TODO | | |
| 11 | Batman #497            | 11 | **The Broken Bat** (Bane wins)| TODO | | |
| 12 | Detective Comics #664  | 12 | Who Rules the Night           | TODO | | |
| 13 | Showcase '93 #7        | 13 | Two-Face Part 1               | TODO | | |
| 14 | Showcase '93 #8        | 14 | Two-Face Part 2: Bad Judgment | TODO | | |
| 15 | Batman #498            | 15 | Knight in Darkness            | TODO | | |
| 16 | Shadow of the Bat #16  | 16 | The God of Fear Part 1        | TODO | | |
| 17 | Shadow of the Bat #17  | 16 | The God of Fear Part 2        | TODO | | |
| 18 | Shadow of the Bat #18  | 16 | The God of Fear Part 3        | TODO | | |
| 19 | Detective Comics #665  | 17 | Lightning Changes             | TODO | | |
| 20 | Batman #499            | 18 | The Venom Connection          | TODO | | |
| 21 | Detective Comics #666  | 19 | The Devil You Know            | TODO | | |
| 22 | Batman #500            | 20 | **Dark Angel** (Azrael cowl)  | TODO | | |

## Prelude (recommended — Bane's origin + the Arkham gambit)

| Order | Title / Issue | Story | Status | Source | Komga Book ID |
|------:|---------------|-------|:------:|--------|---------------|
| P1 | Batman: Vengeance of Bane #1 | Bane's origin (essential) | TODO | | |
| P2 | Batman #491 | The Freedom of Madness (Arkham breakout) | TODO | | |

## Deep prelude (optional — completist only, animation likely skips)

Batman #484–490, Detective Comics #654–658. Not tracked unless requested.

---

## Tracer findings (2026-06-25)

Ran a one-issue tracer (Batman #497) end-to-end. Pipeline works; sourcing reality
emerged:

- **Kometa series:** Batman (1940) added as tracked id **58**, off pull-list,
  folder `/comics/DC Comics/Batman`. LOCG only populated issues **#574–713** —
  the 1993 Knightfall issues are NOT in LOCG's list, but per-issue search still
  works (queue is keyed on title+number, not the issue row).
- **Singles are a retention gamble.** Each vintage issue has ~1 ancient Usenet
  post (≈13 yrs old). #497 (27 grabs) found the right release but SAB returned
  `Aborted, cannot be completed` — articles decayed. #500 (99 grabs) / SotB #16
  (126 grabs) may complete, but it's a per-issue coin-flip. GetComics had no
  single-issue hits.
- **Trades are abundant + well-retained** (Usenet, via Prowlarr):
  - `Knightfall Part 01 - Broken Bat` — 135 grabs
  - `Knightfall Part 02 - Who Rules the Night` — 130 grabs  (these two = all 22 core issues)
  - `KNIGHTFALL Volume 1 to 3` pack — 2.7GB, **1073 grabs** (whole trilogy)

**Strategy pivot:** lead with the **trades**, not floppies. Floppies become
opportunistic backfill only where a popular issue happens to still complete.

### Usenet retention wall (2026-06-25, follow-up)

The real blocker is the **news server**, not the search:

- SAB has ONE provider: **news.frugalusenet.com** (budget, limited retention).
- Frugal completes RECENT posts fine (history shows `Feral #24`, `Chicago.Fire`
  Completed) but ABORTS old vintage posts (#497 @4868d, old KF trade @4024d).
- The "1073-grab" Vol 1–3 pack is a **torrent** (magnet), not an NZB — SAB can't
  take it. Torrent indexers are in Prowlarr but there's no torrent client wired.
- Newer NZB repost (`KF Vol.01 2012 Hybrid` @1256d) submitted directly to SAB
  hung at "Grabbing 0%" — likely an NZBGeek grab-limit / stale link, inconclusive.
- GetComics returned NO hit for "Batman Knightfall [TPB]" — unexpected; the trade
  match gate ([[project_getcomics_ogn_trade_match]]) is the prime suspect.

**Open question for next session:** to land vintage Knightfall reliably we likely
need ONE of: (a) a higher-retention Usenet provider/block account on SAB, (b) a
torrent client so the well-seeded magnet packs become usable, or (c) fix/loosen
GetComics trade matching so its direct (retention-free) downloads work. Untested.

## Notes / decisions

- Reading order interleaves four titles; do NOT sort by issue number.
- Shadow of the Bat #16–18 is one "part" (16) but three physical issues — three readlist entries.
- Komga book `number` metadata can lie (sequential counter) — verify by filename on scan.
- Trade route means the readlist may be 2 volume-entries (Broken Bat + Who Rules
  the Night) rather than 22 issue-entries — acceptable per "refine at readlist time".
