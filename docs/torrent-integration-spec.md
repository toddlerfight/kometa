# Build Spec: Torrent Acquisition (qBittorrent) + Prowlarr Aggregate Search

## Goal

Add a third acquisition path to Kometa so it can grab torrents via qBittorrent,
mirroring the existing Usenet/SAB path. Kometa remains the "brain" (Radarr role):
it searches, decides, routes to a download client, and imports into Komga. This
spec adds qBittorrent as a second download client and upgrades the search layer to
Prowlarr's aggregate API so torrent results become visible.

## Acquisition cascade (target state)

For BOTH issues (`_acquire_issue`, acquisition.py:161) and trades
(`_acquire_trade`, acquisition.py:216):

```
1. GetComics        direct HTTP scrape — retention-free, primary, unchanged
2. Prowlarr search  aggregate /api/v1/search → NZB + torrent candidates
       └─ brain scores & picks by protocol:
            NZB    → SABnzbd       (existing)
            magnet → qBittorrent   (NEW)
3. not_found
```

GetComics stays primary and is NOT part of the Prowlarr/indexer layer (it is
Kometa's own scraper). Komga import is unchanged.

## Known infrastructure (verified 2026-06-25)

- **qBittorrent:** container `qbittorrent`, WebUI host port **8090**, user
  `marcusg`. API requires auth even on localhost (no localhost-bypass) and rejects
  the Tailscale hostname — drive it from the NAS via `127.0.0.1:8090`. Config:
  `/volume1/docker/config/qbittorrent/qBittorrent.conf`.
- **Shared downloads volume:** `kometa`, `sabnzbd`, AND `qbittorrent` all mount
  `/volume1/docker/media/downloads -> /downloads`. qBit completions land where
  Kometa already reads SAB output. Per-torrent path also available via the qBit
  API (`content_path` / `save_path`) — prefer that over assuming a fixed dir.
- **SAB:** container `sabnzbd`, host port 8080, api key in
  `/volume1/docker/config/sabnzbd/sabnzbd.ini`, complete dir `/downloads/complete`.
- **Prowlarr:** host port **9696**, aggregate search `GET /api/v1/search?query=…`
  returns normalized results with `protocol` (usenet|torrent), `seeders`, `grabs`,
  `size`, `age`, `indexer`, `downloadUrl`/`guid` (magnet for torrents). No download
  clients configured (and none needed — the brain owns the clients).
- **Komga:** library id `0Q5Q53JADQNMB`, root `/comics`. Scan trigger via
  `_komga_scan()` (acquisition.py:90).
- **Container-DNS rule:** services on the NAS address each other by LAN IP
  `192.168.1.166` or `127.0.0.1`, never the Tailscale MagicDNS name (containers
  can't resolve it). Kometa's indexer/SAB URLs already use the LAN IP.

## Templates to copy

- Usenet client: `usenet_client.py` (`search_usenet`, `search_usenet_pack`:192).
- SAB client: `sabnzbd_client.py` (`add_nzb_url`, status polling).
- Poller: `_poll_usenet_jobs` (acquisition.py:316) + `_finalize_usenet_download`
  (acquisition.py:355). Note the pack sentinel `issue_number == -1` and the
  `kind == "trade"` branch — torrent finalize follows the same shape.
- Queue states: `db.queue_issue` (db.py:551), `db.queue_trade` (db.py:564),
  `pending_usenet` state handling, `set_sab_nzo_id`.
- Scheduler wiring: `start_scheduler(... _poll_usenet_jobs)` (main.py:220).

---

## Phases

Each phase is an independently testable/deployable slice. Risky qBittorrent
integration is front-loaded as a tracer before any search/decision work.

### Phase 1 — qBittorrent client + config (foundation)

- New `kometa/qbittorrent_client.py`: session login (`/api/v2/auth/login` with
  Referer header), `add_magnet(magnet, category, savepath)`, `get_torrent(hash)`
  (progress %, state, seeders, num_peers, dlspeed, content_path), `delete(hash)`.
- Config keys: `qbit_url`, `qbit_user`, `qbit_pass`; settings UI test button
  mirroring the SAB test (`POST /api/test/sab` → add `/api/test/qbit`).
- **Test:** against live qBit (127.0.0.1:8090) — auth succeeds, add a known healthy
  magnet, read its status, delete it. No pipeline changes yet.

### Phase 2 — Torrent tracer (prove download → import end to end)

- Temporary forced path: queue a torrent with a known magnet (the 1073-seeder
  Knightfall Vol 1–3 pack) on a FIXTURE series, push to qBit, new `pending_torrent`
  state, `_poll_torrent_jobs()` polls qBit, on complete calls a torrent finalize
  that reuses `_finalize_usenet_download` logic (find comics in `content_path` →
  move to `/comics` → `_komga_scan()`).
- Handle terminal torrent failures: stalled / 0 seeders / metadata-timeout → mark
  `failed` with a torrent-specific message.
- **Test:** fixture series, real magnet → file lands in Komga. This de-risks the
  newest integration before touching search/decision.

### Phase 3 — Prowlarr aggregate search (unlock torrent visibility)

- New `prowlarr_client.py` (or extend `usenet_client.py`): `search(query)` against
  `/api/v1/search`, returns normalized candidate dicts incl. `protocol`,
  `seeders`, `grabs`, `age`, `size`, `title`, `downloadUrl`/magnet.
- Keep existing behavior intact: callers can still filter to NZB-only until Phase 4
  flips on cross-protocol selection.
- **Test:** known queries return BOTH protocols; compare against direct Prowlarr
  probes. Confirm magnets present for torrent results.

### Phase 4 — Protocol-aware decision + torrent branch

- Selection function `pick_release(candidates, store_date/year)`:
  - Fresh content → prefer NZB (faster, no ratio).
  - Vintage / NZB retention-risky (old age) → prefer high-seeder torrent.
  - Tunable thresholds; default conservative.
- Wire as the 3rd branch in `_acquire_issue` and `_acquire_trade`: after GetComics
  miss, run Prowlarr search → `pick_release` → route NZB→SAB (existing) or
  magnet→qBit (Phase 1/2). Remove the Phase-2 forced path.
- **Test:** synthetic candidate sets assert the pick; live cascade on a vintage
  series chooses the torrent where Usenet would retention-fail.

### Phase 5 — Activity view surfacing (app.js)

- Add chip: `pending_torrent: ['chip chip-active', 'Torrent']` (chip map ~1560).
- **Add `pending_torrent` to BOTH active-state arrays (1547 `hasActive`, 1610
  `inProgress`).** Non-negotiable — omitting it re-creates the poll-parking bug,
  worse here because torrents run for minutes (ref: prior Activity poll-park fix).
- Torrent detail line (parallel to `pending_usenet` detail ~1631):
  `{pct}% · {seeders} seeders · {speed}`.
- Richer `search_status` strings from backend (no UI structural change): e.g.
  "Searching Prowlarr…", "Torrent: {title} · {seeders} seeders". Surfaces the
  brain's choice and reason.
- Bump `app.js?v=` and `style.css?v=` per deploy rules.
- **Test:** watch a live torrent grab in Activity — no parking, chip + detail
  render, search_status shows the chosen path.

### Phase 6 — (optional, separable) GetComics trade-gate fix

- GetComics returned no hit for "Batman Knightfall" despite hosting it — the trade
  match gate is the suspect. Loosen/fix so the retention-free primary works for
  trades. Independent of the torrent work but related (a weak primary makes the
  torrent fallback matter more).

---

## Testing & deploy discipline

- Test each phase against a FIXTURE series / test copy before live data.
- Deploy per repo `INSTRUCTIONS.md`: commit + push to Gitea (rollback point) BEFORE
  deploying; code is bind-mounted → deploy = tar-sync changed files + `compose
  restart` (seconds, not a rebuild); static files sync live. Bump BOTH
  `app.js?v=` and `style.css?v=` on any frontend change. Never `rm` the mount dir.

## Policy decisions (confirmed with user 2026-06-25)

- **Seeding:** managed by qBittorrent itself (its own ratio/seeding rules). Kometa
  does NOT control seeding — it only imports and, later, cleans up.
- **VPN:** none on the torrent client (user-accepted). No bind/VPN gating in scope.
- **Cleanup:** import first, then remove the torrent + its data on a grace delay
  (~1–2 days after the torrent reports complete). Configurable
  `torrent_cleanup_days` (default 2). NEVER delete before a confirmed import.

## Risks / open items

- qBit save path vs category: confirm where completed torrents land (use API
  `content_path` rather than assuming `/downloads/complete`).
- Kometa container → qBit: reach the WebUI via the host LAN IP
  `http://192.168.1.166:8090` (container-to-host), per the container-DNS rule.
