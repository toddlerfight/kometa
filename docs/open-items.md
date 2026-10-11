# Open items

Kept here so they don't live in a chat. Newest at the top of each section.

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

- [ ] YondeB fixes and checks — `~/code/yondeb/LATER.md`.
- [ ] Tidy the older loose files in `/downloads` (Transmetropolitan, Die, Avengers Vol 5; the `.mkv`s aren't Kometa's).
- [ ] Off-site backup — declined for now (Voot II only).
- [ ] Komga — leave running; repoint or retire later.

## On the build side (no input needed)

- [ ] "Pages start at 002" guard — check whether it's too strict (Where Monsters Dwell #2–4, Death of Dracula).
- [ ] Download errors in words ("download cut off"), not a Python exception.
- [ ] Confirm "database is locked" is gone now that connections are kept per thread.
