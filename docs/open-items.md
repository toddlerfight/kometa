# Open items

Kept here so they don't live in a chat. Newest at the top of each section.

## From the 2026-10-11 second review — security still to do

Bugs from today's waves — **fixed 2026-10-11** (kept for the record):
- [x] Book rows for deleted/renamed files are never dropped (wave 1 removed the per-row exists checks assuming the scan prunes them — it doesn't). 25 ghost rows live. Mark rows not seen by a scan; drop or flag them; finalize forgets files it deletes.
- [x] `?fast=1` cover threads lose the "interactive" flag (thread pools don't copy contextvars) and wait on LOCG without a timeout — copy the context on submit (covers and the share-read lanes).
- [x] `LOAD_TRUNCATED_IMAGES` flipped process-wide during cover checks while other threads decode — detect truncation per image instead.
- [x] Startup scan skip also skips new-folder import and trash purge — run those anyway.
- [x] `_archive_verdict`'s memory cache remembers "couldn't look" until restart.
- [x] `missing` counted two ways (Library card vs series page) once trades cover gaps.

Security (LAN + tailnet; Funnel is off — checked):
- [ ] One request filter: accept changes only from Kometa's own pages / YondeB / curl and only for Kometa's own addresses (CSRF + DNS rebinding).
- [ ] Test-connection buttons only use stored keys with the stored address (today a request can name another server and get the key).
- [ ] Fence every folder path and the comics root inside the library; fix the purge depth bug (a folder directly under the root loses its 7-day Undo).
- [ ] Variant cover URLs: only known cover hosts, served as images only, `nosniff`.
- [ ] ~12 inline click handlers built from titles: escape properly (`esc(JSON.stringify(...))`).
- [ ] Scrub `apikey=` / `api_key=` / tokens out of errors, the queue and logs; Prowlarr key as a header.
- [ ] URL importer: https only, no private addresses, size cap; caps on uploads and zip entries.
- [ ] Container: non-root user, code mounted read-only, drop capabilities.
- [ ] Pre-commit hook: catch tokens inside URLs (`user:token@host`) — how the Gitea token got in.

Efficiency leftovers:
- [ ] Activity fetches the whole queue every 2 s while something's active (~6 KB gzipped) — a `?since=` merge would do; skipped as not worth the risk yet.

Personal details (low, current tree): machine/drive names (ginbako, Voot II, emoji shares), ports, Brisbane in docs/scripts — genericise if wanted. YondeB has the tailnet/LAN address and Apple Team ID hard-coded (fine while private).

## Waiting on a check (after the 2026-10-11 efficiency waves)

- [ ] Library: open, go into a series, Back — instant, scroll kept.
- [ ] Activity: updates during a download; badge counts right.
- [ ] A download end to end: downloading → processing → done; in its folder; opens.
- [ ] Trades tab on a series not opened lately: spinner, then trades.
- [ ] Remove a series; Undo restores it.
- [ ] Reader: a new book shows the progress bar, then crossfades into page 1.

## Decisions

- [ ] Two broken partial downloads in `/downloads` (Avengers Academy Vol. 2 — 252 MB of 673; FF 001–023 — 160 MB): delete or keep.
- [ ] `komga_import.py` — has the one-time Komga reading-history import run? If yes, delete it.
- [ ] Trades belong to a series: how folders like *Batman: Hush* / *Year One Deluxe* get filed under their parent run. Proposal: Kometa suggests the parent from the folder name, you confirm each; it becomes a trade on that run's Trades tab, readable, counted toward what it collects, never needs matching.
- [ ] Two failed trade downloads: *Fantastic Four: Unthinkable* (stalled in SABnzbd), *Avengers Academy Vol. 2* (GetComics cut off) — retry or drop.

## Parked

- [ ] Gitea `claude-code` token in public history (commit 3058eff) — your call: not revoking for now (Gitea is LAN/tailnet only).

- [ ] YondeB fixes and checks — `~/code/yondeb/LATER.md`.
- [ ] Tidy the older loose files in `/downloads` (Transmetropolitan, Die, Avengers Vol 5; the `.mkv`s aren't Kometa's).
- [ ] Off-site backup — declined for now (Voot II only).
- [ ] Komga — leave running; repoint or retire later.

## On the build side (no input needed)

- [ ] "Pages start at 002" guard — check whether it's too strict (Where Monsters Dwell #2–4, Death of Dracula).
- [ ] Download errors in words ("download cut off"), not a Python exception.
- [ ] Confirm "database is locked" is gone now that connections are kept per thread.
