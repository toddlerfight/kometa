# Kometa — Context

## What it is

Kometa is a comic-book acquisition, tracking and reading service. It watches the
series you follow, detects missing issues, downloads them from multiple sources, files
them on disk, and reads them in its own reader.

FastAPI backend + static single-page UI, deployed as a Docker container on a NAS.
SQLite for state. Background scheduler drives periodic work.

## Ownership model (load-bearing)

**Ownership is the disk folder, and only the disk folder.** A series is "owned" because
its files exist under a `folder_path`. Deleting/moving files on disk is the real
mutation; everything else reflects it.

## The filename is the issue number (load-bearing)

**The FILENAME is the source of truth for issue number.** `parse_issue_number`
(naming.py) derives it. (An external server's per-book sort number was an unreliable
running counter — "Monstress #062" landed at 83 — which is how this rule was learned.) A series
is identified on disk by its **folder path**, not its title — the only key that
disambiguates same-titled runs (three bare "Batman" series) and alternate printings.

## Core entities (SQLite — see db.py)

- **tracked_series** — a series under management. Bridges identities across sources:
  `metron_series_id` ↔ `cv_volume_id` ↔ `locg_series_id`. Carries
  `folder_path` (ownership), `on_pull_list`, `monitor_status`, `year_began`, `publisher`.
- **issue_status** — per-issue state for a tracked series: `number` (REAL), `store_date`,
  `owned` (on disk — the ownership flag; set by the folder scan),
  `metron_image`, `metron_issue_id`, `locg_issue_id`.
- **download_queue** — issues being acquired. State machine:
  `queued → searching → downloading → done | not_found | failed | pending_usenet`.
  Carries `retry_after` (release-day/duplicate backoff) and `sab_nzo_id` (SABnzbd handle).
- **variant_prefs** — selected variant covers per issue (queued for unowned issues,
  injected into the CBZ once owned).
- **config** — key/value store for credentials and settings.

## External sources (each has a client module)

| Source | Role | Module |
|---|---|---|
| **Metron** | Primary metadata: series, issue lists, store dates | metron_client.py |
| **ComicVine (CV)** | Alternate metadata + cover images | comicvine_client.py |
| **LOCG** (League of Comic Geeks) | Series/issue data + variant covers | locg_client.py |
| **GetComics** | Primary download source (scrapes getcomics.org) | getcomics_client.py |
| **Usenet** | Fallback download: newznab indexers (search) | usenet_client.py |
| **SABnzbd** | Usenet download client (NZB execution + polling) | sabnzbd_client.py |

## Core workflows

1. **Add** — find a series (Metron or anonymous LOCG search), derive its folder from
   publisher+title, and track it.
2. **Sync** — for tracked series, pull issue lists (Metron → CV → LOCG, anon if no
   creds), update `issue_status`, and detect missing by scanning the folder on disk.
3. **Queue + download** — missing issues enter `download_queue`. `_process_queue` tries
   GetComics first, falls back to Usenet (newznab search → SABnzbd submit → poll).
   Archives are extracted (rarfile/bsdtar) and filed under `folder_path`.
4. **Variant covers** — scrape variant covers from LOCG, inject into the issue's CBZ.
5. **Scheduling** (scheduler.py) — APScheduler: periodic full sync (which folder-gated-sweeps at
   the end), SABnzbd poller (interval configurable via `KOMETA_USENET_POLL_SECONDS`, default 5s),
   release-day retry windows. Downloads from a sweep go to GetComics→Usenet; usenet finalize needs
   Kometa's `/downloads` mounted at SAB's shared folder (arr-stack convention — see INSTRUCTIONS.md).

## Module map

- **main.py** — FastAPI app + the ~48 route handlers and lifespan/scheduler bootstrap.
  The deep orchestration that used to live here has been extracted (see below); main is
  now the web layer plus glue (`_sync_all_job`, `_summary`).
- **sources.py** — the seam to every external system. Configured-client accessors
  (`comicvine`, `sabnzbd`, `qbittorrent`, `prowlarr`, `wikipedia`) that read config
  from the DB and cache where it makes sense. Callers never touch credentials.
- **sync.py** — `sync_one`: build the issue list from metadata sources (Metron primary,
  CV/LOCG supplements; LOCG anon), reconcile ownership against the disk folder, upsert.
- **acquisition.py** — the download state machine (`_process_queue`, `_sweep_missing`,
  `_poll_usenet_jobs`, `_finalize_usenet_download`, `_release_day_retry`).
  `_sweep_missing` is **folder-gated** (only sweeps series whose folder it has actually inventoried
  — no sweeping into the void). `_finalize_usenet_download` scans `dirname(storage)` when SAB
  reports a single grab's storage as a file. Owns `_dl_progress`, the live progress map the UI polls.
- **naming.py** — pure parsing helpers (`parse_issue_number`, `scan_folder_numbers`,
  `find_issue_file`, `normalize_url`, `norm`). No state; unit-testable in isolation.
- **db.py** — all SQLite access; every query lives here behind plain functions. Owns the
  schema/migrations and atomic operations like `complete_download`.
- **downloader.py** — download, archive extraction, filename/dir resolution, duplicate
  detection, cover injection into CBZ.
- **retag.py** — the ComicInfo write-back job (enrichment 3b–3d): `retag_journal` table,
  build in staging → verify → temp beside the original → read back → atomic swap; CBR → CBZ
  with the .cbr moved to `_trash/<publisher>/retag-cbr-<date>/`; books row and page cache
  follow (same book id). `retag_tick` (behind `retag_enabled`) is the standing job; `JOB_LOCK`
  keeps it and the batch apart. Only route: read-only `GET /api/books/{id}/comicinfo-preview`.
- **retag_batch.py** — the full-library rewrite (3f) as three hand-started phases, armed by
  config `retag_batch_armed`: queue (`POST /api/retag/batch/queue`), build from the Catbus
  snapshot `/retag/source` into `/retag/build`, 4 at a time, stopping at verified
  (`.../build`), and swap through `retag.swap` (`.../swap`). Also `.../stop`, `GET /api/retag/batch-report`,
  `.../batch/dry-count` and `.../batch/staged/{id}/comicinfo`. Runbook in INSTRUCTIONS.md.
- **toprated.py** — read-only ranking over the community scores already in
  `issue_record.community_json` (CBR critic/reader, LOCG ×2, Hardcover/Open Library ×2),
  capped-vote pooled and Bayesian-shrunk. `GET /api/series/{id}/top-rated` (owned runs only;
  the series page's ★ sort) and `GET /api/ondeck/picks` (On Deck's Community picks: owned
  runs not read to the end). No network.
- **scheduler.py** — APScheduler job registration.
- **\*_client.py** — one module per external source, wrapping its API/scrape surface.

Dependency direction: `main` → `acquisition`/`sync` → `sources`/`naming`/`db` → `*_client`.
No cycles. Logic modules import the source seam, never `main`.

## Glossary

- **Pull list** — series the user actively follows (`on_pull_list = 1`).
- **Monitor status** — whether Kometa actively acquires for a series (`monitored`/unmonitored).
- **Pack** — a multi-issue NZB bundle (submitted when a series has many gaps).
- **Store date** — an issue's retail release date.
