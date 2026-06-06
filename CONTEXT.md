# Kometa — Context

## What it is

Kometa is a comic-book acquisition and tracking service that pairs with **Komga**
(a self-hosted comic server). Kometa watches the series you follow, detects missing
issues, downloads them from multiple sources, files them on disk, and lets Komga
index them. Komga owns reading and serving; Kometa owns finding and acquiring.

FastAPI backend + static single-page UI, deployed as a Docker container on a NAS.
SQLite for state. Background scheduler drives periodic work.

## Ownership model (load-bearing)

**Ownership is the disk folder, and only the disk folder.** A series is "owned" because
its files exist under a `folder_path`. Komga is a metadata and thumbnail source — never
the source of truth for ownership. Deleting/moving files on disk is the real mutation;
Komga merely reflects it on its next scan.

## Core entities (SQLite — see db.py)

- **tracked_series** — a series under management. Bridges identities across sources:
  `komga_series_id` ↔ `metron_series_id` ↔ `cv_volume_id` ↔ `locg_series_id`. Carries
  `folder_path` (ownership), `on_pull_list`, `monitor_status`, `year_began`, `publisher`.
- **issue_status** — per-issue state for a tracked series: `number` (REAL), `store_date`,
  `in_komga`, `komga_book_id`, `metron_image`, `metron_issue_id`, `locg_issue_id`.
- **download_queue** — issues being acquired. State machine:
  `queued → searching → downloading → done | not_found | failed | pending_usenet`.
  Carries `retry_after` (release-day/duplicate backoff) and `sab_nzo_id` (SABnzbd handle).
- **match_candidates** — output of reconciling a Komga series against Metron metadata:
  `score`, `confidence` (none/low/medium/high), `candidates_json`, `status`.
- **variant_prefs** — selected variant covers per issue (queued for unowned issues,
  injected into the CBZ once owned).
- **config** — key/value store for credentials and settings.

## External sources (each has a client module)

| Source | Role | Module |
|---|---|---|
| **Komga** | The library being mirrored; series origin; scan trigger | komga_client.py |
| **Metron** | Primary metadata: series, issue lists, store dates | metron_client.py |
| **ComicVine (CV)** | Alternate metadata + cover images | comicvine_client.py |
| **LOCG** (League of Comic Geeks) | Series/issue data + variant covers | locg_client.py |
| **GetComics** | Primary download source (scrapes getcomics.org) | getcomics_client.py |
| **Usenet** | Fallback download: newznab indexers (search) | usenet_client.py |
| **SABnzbd** | Usenet download client (NZB execution + polling) | sabnzbd_client.py |

## Core workflows

1. **Match** (matcher.py) — reconcile Komga library series with Metron metadata.
   Scores candidates, assigns confidence, corroborates against LOCG. Writes
   `match_candidates` for user review.
2. **Sync** — for tracked series, pull issue lists from Metron, update `issue_status`,
   detect which issues are missing relative to Komga.
3. **Queue + download** — missing issues enter `download_queue`. `_process_queue` tries
   GetComics first, falls back to Usenet (newznab search → SABnzbd submit → poll).
   Archives are extracted (rarfile/bsdtar), filed under `folder_path`, then Komga scans.
4. **Variant covers** — scrape variant covers from LOCG, inject into the issue's CBZ.
5. **Scheduling** (scheduler.py) — APScheduler: periodic full sync, weekly missing sweep,
   60s SABnzbd poller, release-day retry windows.

## Module map

- **main.py** — FastAPI app (48 routes), request handlers, plus orchestration logic:
  queue processing, per-series sync, scheduler bootstrap. Currently the system's monolith.
- **db.py** — all SQLite access; every query lives here behind plain functions.
- **matcher.py** — Komga↔Metron matching: normalization, scoring, confidence, corroboration.
- **downloader.py** — download, archive extraction, filename/dir resolution, duplicate
  detection, cover injection into CBZ.
- **scheduler.py** — APScheduler job registration.
- **diff.py** — small set-difference helper for issue reconciliation.
- **\*_client.py** — one module per external source, wrapping its API/scrape surface.

## Glossary

- **Pull list** — series the user actively follows (`on_pull_list = 1`).
- **Monitor status** — whether Kometa actively acquires for a series (`monitored`/unmonitored).
- **Pack** — a multi-issue NZB bundle (submitted when a series has many gaps).
- **Store date** — an issue's retail release date.
- **Corroboration** — cross-checking a Metron match against LOCG to raise confidence.
