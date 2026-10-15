# Kometa — Working Instructions

Process notes for developing Kometa locally. Read this before touching the code.

## Local environment

- Python venv: `.venv/` (use `.venv/bin/python`, `.venv/bin/ruff`, etc.)
- The app is a FastAPI service; entry point is `kometa.main:app`.
- Config and state live in a SQLite DB at `$KOMETA_DB` (default `/data/kometa.db`
  in the container). **Locally, always point it at a throwaway temp file** — never
  the live DB.

## Run the app locally (no NAS contact)

```bash
export KOMETA_DB=/tmp/kometa_dev.db && rm -f "$KOMETA_DB"
.venv/bin/python -m uvicorn kometa.main:app --host 127.0.0.1 --port 6971 --log-level warning
# smoke test in another shell:
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:6971/api/series   # expect 200
```

`init_db` runs on startup and builds a fresh schema cleanly (migrations are idempotent).

Port 6971 on purpose — 6969 is the NAS, **6970 is reserved for the fresh-install
Docker rig**. Never run a dev server on 6970: a long-lived dev uvicorn there once
impersonated the fresh rig for five days (the scheduler starts with every instance
and will sweep/search whatever DB it was given). Kill dev servers when done:
`lsof -nP -i :6971`.

## Fresh-install test rig (local Docker)

Simulates a brand-new user's day-zero install: same image as production, empty DB,
empty comics dir, no credentials, no env seeding. Isolated from the NAS instance
(port 6970, container `kometa-local`, compose project `kometa-local`).

```bash
./local-fresh.sh          # build + start; state persists in ./local/ between runs
./local-fresh.sh --wipe   # delete ./local/ first — true day-zero on next start
./local-fresh.sh --down   # stop and remove the container (state survives)
```

UI at http://localhost:6970. Drop test CBZ/CBR files into `./local/comics/` to
exercise import flows. `./local/` is gitignored.

## Distributable package (send to someone else)

`./build-package.sh` regenerates `~/Desktop/kometa-fresh-install.tar.gz` (or pass
an output path) — a self-contained bundle: current `kometa/` source + Dockerfile
+ `packaging/` wrappers (run.sh, compose, README). Recipient untars and runs
`./run.sh` (builds the image locally, runs as the host user so files aren't
root-owned on Linux). Rebuild whenever `main` moves; it refuses to ship if it
finds private data or dead code. Edit the recipient-facing files in `packaging/`.

## Static analysis (all run from the venv)

```bash
.venv/bin/ruff check kometa/                      # lint (F=bugs, E=style)
.venv/bin/ruff check kometa/ --select B,SIM,PERF,TRY,S   # bug patterns, simplify, security subset
.venv/bin/mypy kometa/ --ignore-missing-imports --check-untyped-defs   # type/None bugs
.venv/bin/vulture kometa/ --min-confidence 80     # dead code
.venv/bin/bandit -r kometa/ -ll -ii               # security
.venv/bin/pip-audit -r requirements.txt           # dependency CVEs
```

Note: most remaining mypy output is bs4 typing noise (`str | AttributeValueList`)
— not real bugs.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt   # one-time: pulls in pytest
.venv/bin/python -m pytest                       # runs tests/ (see pytest.ini)
.venv/bin/python -m pytest tests/test_acquisition.py -q   # one file
```

The suite (`tests/`) runs against a throwaway SQLite DB per test (`db_path` fixture
in `conftest.py`) and injects fakes for every external source — no GetComics,
SABnzbd, or network. Coverage: `naming` pure parsers, `db` (atomic
`complete_download`, queue requeue rules, the fresh-install migration), and
`acquisition` (`_process_queue` happy/not-found paths, `_finalize_usenet_download`
file moves). Dev-only deps live in `requirements-dev.txt` and never ship to the
container.

Pattern for new tests: use the `db_path`/`series` fixtures, `monkeypatch.setattr`
the acquisition module's `DB_PATH` + source accessors (`acq._sabnzbd`,
`acq.GetComicsClient`), call the function, assert DB/disk state.

## Deploy to the NAS (commit-then-deploy — no per-change approval needed)

Deploys do not require per-change approval while the NAS is the active test
environment (rule changed 2026-06-10). The non-negotiable safety net: **every
deploy is preceded by a commit pushed to the canonical remote**, so there is
always a point-in-time to roll back to. Remotes (set 2026-06-18):
- `origin` → `https://github.com/toddlerfight/kometa.git` (GitHub, **PUBLIC**, canonical) — `git push origin main` is the pre-deploy rollback point.
  **This repo is public. Never commit hosts, ports, usernames, keys, or IDs** — they
  go in `.env` (gitignored). Placeholders like `$KOMETA_HOST` / `$NAS_HOST` in these
  docs are deliberate.

  A pre-commit hook enforces this (`.githooks/pre-commit`). It is NOT active until you
  point git at it — `core.hooksPath` is local config and does not survive a clone:
  ```bash
  git config core.hooksPath .githooks
  ```
  Run that once per clone. It blocks staged changes containing the NAS IP, tailnet
  host, SSH port, username, library ID, iCloud aliases, private-key blocks, 32-hex API
  keys or inline credentials. Verified zero false positives against the current tree.
  `--no-verify` bypasses it; that is almost never the right answer.
- `gitea` → `ssh://git@$KOMETA_HOST:2222/<you>/kometa.git` (Gitea on ginbako, fast local mirror). Push here too when convenient: `git push gitea main`.

Rollback = `git checkout <commit> -- <files>`, re-sync, restart. Destructive
operations (anything that touches library files or the DB schema) still need
explicit approval.

**The app moved off the Synology NAS to ginbako (M4 Mac mini) in Aug 2026.** Any
`/volume1/...` path or `/var/packages/ContainerManager/...` docker binary you find
in older notes or git history is dead. The NAS still hosts SABnzbd, qBittorrent and
Prowlarr — those references are still live.

Live runs as a Docker container on ginbako (container name `kometa`, port 6969).
Host access: `ssh -o IdentitiesOnly=yes -p $KOMETA_PORT -i $KOMETA_KEY $KOMETA_USER@$KOMETA_HOST`.
Docker is OrbStack's at `$KOMETA_DOCKER` (`/usr/local/bin/docker` — NOT on `PATH`
over a non-interactive ssh, so always use the absolute path). No rsync/scp — use
`tar`-pipe.

Layout on ginbako:
- `$KOMETA_COMPOSE_DIR` (`~/docker`) — holds `compose.yml` for every app on the box
  (komga [retired from Kometa 2026-10-12], seerr, gitea, backlog, ...). `docker compose` commands must run from here.
- `$KOMETA_APP_DIR` (`~/docker/apps/kometa`) — the deploy target. The bind mount is
  `./apps/kometa/kometa:/app/kometa`, so the tar extracts into `$KOMETA_APP_DIR` and
  the `kometa/` dir inside it *is* the live code.
- The DB is at `/data/kometa.db` inside the container.

**Where those `$KOMETA_*` values live:** a gitignored `.env` in the repo root, read by
`deploy.sh` (see `.env.example`). They are NOT in the repo and must never be — this
is a public GitHub repo. The host has its own separate `.env` under `$KOMETA_APP_DIR`
holding the app's `METRON_*` / `CV_API_KEY` (any `KOMGA_*` left there is dead — Komga
is retired); the two files are unrelated,
don't confuse them. (`deploy.sh` still honours the legacy `NAS_*` names as a fallback
so an old `.env` keeps working, but `KOMETA_*` wins.)

**Deploying off the home network (Tailscale):** `$KOMETA_HOST` is LAN-only. When
remote, swap every SSH/tar host to `$KOMETA_TS_HOST` (same port, same key);
add `-o StrictHostKeyChecking=accept-new` on first connect. Also: the pre-deploy
`git push gitea main` rollback push FAILS off-LAN (the `gitea` remote is the LAN IP)
— only `git push origin main` (GitHub) works remote, and that's the one that matters.
Push to `gitea` later from home.

**ALWAYS run `./stamp.sh` first**, whichever path you take. It writes
`kometa/_build.json` (gitignored) with the commit SHA, branch and a dirty flag, and
warns if you're shipping uncommitted work. `deploy.sh` calls it automatically; the
tar-sync path below does NOT, so run it by hand and include `kometa/_build.json` in
the tar. Skip it and `GET /api/version` reports a stale commit — worse than no answer,
because you'll believe it.

Check what's actually live any time: `curl http://$KOMETA_HOST:6969/api/version`. If
`restart_may_be_needed` is true, files were synced but the process wasn't restarted —
the running Python is not the Python on disk.

**The source is BIND-MOUNTED** (`./apps/kometa/kometa:/app/kometa` in `compose.yml`),
so code is NOT baked into the image. This makes deploys a **few-second restart, not a
rebuild** (the old rebuild caused ~1-2 min downtime — see the 2026-06-09 git history
of frustration). Pick the path by what changed:

- **Python change** → `./deploy.sh`. It stamps, tar-syncs the whole `kometa/` package
  into the live mount, and restarts. Do NOT hand-enumerate files: an earlier version
  of `deploy.sh` piped a per-file list, a module got renamed, and `set -e` aborted it
  *after* it had already overwritten `main.py` — a half-deploy that never restarted.
  The equivalent by hand, if you need it:
  ```bash
  ./stamp.sh
  COPYFILE_DISABLE=1 tar czf - --exclude '__pycache__' --exclude '.DS_Store' kometa | \
    ssh -o IdentitiesOnly=yes -p $KOMETA_PORT -i $KOMETA_KEY \
    $KOMETA_USER@$KOMETA_HOST "cd $KOMETA_APP_DIR && tar xzf -"
  ssh -o IdentitiesOnly=yes -p $KOMETA_PORT -i $KOMETA_KEY $KOMETA_USER@$KOMETA_HOST \
    "cd $KOMETA_COMPOSE_DIR && $KOMETA_DOCKER compose restart kometa"
  ```
  Excludes matter: `__pycache__` .pyc files are root-owned (written by the container)
  and the extract dies on them; `.DS_Store`/`COPYFILE_DISABLE` is macOS being macOS.
- **Static change** (`static/app.js`, `style.css`, `index.html`) → tar-sync only;
  it's served live, **no restart needed**. Bump BOTH `app.js?v=` and `style.css?v=` in
  `index.html`. Tell the user to hard-refresh (Cmd+Shift+R) — the SPA is browser-cached
  (`Cache-Control` on assets), so a stale cached app.js is what makes "old UI / wrong
  behaviour persists after a fix" appear.
- **requirements.txt / Dockerfile / compose.yml change** → still a real recreate:
  `docker compose up -d` (compose change) or `... build && ... up -d` (deps). Deps are
  baked into the image — a requirements change does NOT land via a restart.
- **NEVER** `rm -rf $KOMETA_APP_DIR/kometa` while the container is up — it's the live
  mount; deleting it yanks the code out from under the running app. Sync files in
  place (tar extract overwrites). `compose restart` needs `cd $KOMETA_COMPOSE_DIR`
  first (or `-f`), or you get "no configuration file provided".

Library mounts (2026-10-11): `/comics` and `/downloads` are **NFS 4.1 volumes straight
from the NAS** (top-level `volumes:` in the live compose, `driver_opts: type: nfs`), not a
bind of the host's SMB mount. Through OrbStack the bind read at 0.1–3 MB/s (a new book took
minutes to open); NFS does a page in ~0.3 s. The NAS export needs non-privileged ports
allowed and squash = map all users to admin. Changing a mount means recreating the
container (`compose up -d kometa`), not a restart. If the recreate fails with "container is
running", the old process is stuck on the share: `docker stop -t 30 kometa`, `compose up -d
kometa` again, and if it comes back named `<id>_kometa`, `docker rename <id>_kometa kometa`.

Always: after, `curl http://$KOMETA_HOST:6969/api/series` → 200, and confirm the new
code is actually live, e.g. `docker exec kometa grep -c <new-symbol> /app/kometa/<file>`.
Note: app-level Python loggers do NOT reach `docker logs` (only uvicorn access logs are
wired up) — verify behaviour through the API/DB, not by grepping logs for logger.info
lines. Validate locally first (`ruff check kometa/`, `pytest -q` = 196 unit tests,
`node --check kometa/static/app.js` for JS). **For any frontend or route change,
also run the browser smoke suite:** `.venv/bin/python -m pytest -m e2e -q`
(11 tests, ~11s, headless Chromium against a real in-process server on a fixture
DB — zero external network; see tests/e2e/conftest.py). First-time setup:
`.venv/bin/python -m pip install -r requirements-dev.txt` then
`.venv/bin/python -m playwright install chromium`. The `.venv` has no git →
snapshot files to `/tmp` before risky edits. NB: the venv's bin/pip shebang is
stale (pre-rename cleancomics path) — always use `.venv/bin/python -m pip`.

Dependency/security audit (external tools, added 2026-07-03; deps in
requirements-dev.txt, config in pyproject.toml). Run before a dependency bump or
periodically — all three are green as of 2026-07-03:
- `.venv/bin/python -m pip_audit --skip-editable` — CVEs in the tree. Transitive
  fixes go in requirements.txt as `>=` FLOORS (not `==`), so they don't fight
  fastapi/requests version ranges; see the security-floor block there.
- `.venv/bin/python -m bandit -r kometa/ -c pyproject.toml -ll` — security lint.
- `.venv/bin/python -m deptry kometa/ --requirements-files requirements.txt` —
  dead/missing deps (first-party + indirect-runtime deps allow-listed in pyproject).

Scheduler note (post-2026-07-02): a deploy restart that straddles a 5/12/17 sync fire
no longer loses it — `lifespan` compares the `last_full_sync` config stamp against the
most recent scheduled slot and runs a catch-up sync at boot if one was missed. Expect
a full sync (and its auto-grabs) right after any deploy that crossed a sync hour.

## Runtime integrations

Split across two boxes since the Aug 2026 move. Kometa runs on ginbako
(`$KOMETA_HOST`); **SABnzbd, qBittorrent and Prowlarr are still on the NAS**
(`$NAS_HOST`) — the `/volume1/...` paths below are correct for those.

- **Komga — RETIRED 2026-10-12** (docs/library-enrichment-spec.md, K1–K5). Kometa
  no longer talks to it: no client, no Settings card, no config keys, no series
  linking, no covers, no imports. Do NOT configure or reintroduce it. What it held
  was pulled in first (read history, reading lists/collections, a last metadata
  harvest); those rows stay as data — `issue_record` rows with source `komga` are
  stopgaps the catalogues overwrite, `reading_lists.source = 'komga'` is provenance,
  and the `komga_series_id` / `komga_book_id` columns are kept but nothing reads or
  writes them. Ownership is the folder on disk, the reader is Kometa's own.
- **Push to phones (kometa/push.py)** — two platforms, either may be set up alone; devices say
  which they speak when they register (`POST /api/push/devices`, `platform: apns|fcm`).
  - **iOS (APNs):** the Apple `.p8` key at `data/apns/AuthKey.p8` on ginbako (beside the DB, never in
    the repo) plus `apns_key_id` / `apns_team_id` in config (`PATCH /api/config`).
  - **Android (FCM, HTTP v1):** a Firebase service-account JSON at `data/fcm/service-account.json` —
    on ginbako that is `~/docker/apps/kometa/data/fcm/service-account.json` (mode 600; container path
    `/data/fcm/service-account.json`). Firebase console › Project settings › Service accounts › Generate
    new private key; the project id is read from the file, nothing else to configure. Never in the repo —
    `data/` is the mounted volume, same place as the APNs key. The app swaps an RS256 JWT for a one-hour
    OAuth2 token and caches it; no Firebase SDK, just `cryptography` + `curl_cffi`, both already in the image.
  - **Check:** `GET /api/push/status` → `platforms: {apns: bool, fcm: bool}` says what the file/config
    state lets it send to; `configured` is the OR of the two. `devices` lists each token's platform.
- **Metron API — READ THIS BEFORE TOUCHING IT** (source: Metron-Project/metron
  `api/RATELIMIT.md`, `fail2ban/jail.d/metron.conf`, `api/throttle.py`; verified 2026-10-10
  after Kometa got the house IP banned for 24h):
  - Auth is `Authorization: Bearer <token>` (token from metron.cloud account page). Basic
    auth is legacy (Mokkari 5 removed it). NEVER probe auth schemes against the live API:
    **3 × HTTP 401 within 5 minutes = 24-hour IP ban** (fail2ban `metron-nginx-401`).
  - Other bans: 10 × 403 / 5 min → 24h; 20 × 404 / 2 min → 1h; 20 dropped connections
    (499) / 60s → 1h; 5 requests without a User-Agent / 60s → 24h; any UA containing
    `comictagger` → 24h on the first request. Always send a User-Agent.
  - Limits per authenticated user: burst 20/min, sustained 5,000/day (donors get a higher
    sustained limit). Don't hardcode: read `X-RateLimit-{Burst,Sustained}-{Limit,Remaining,Reset}`
    on every response and pause until `Reset` when `Remaining` hits 0. On 429 honour
    `Retry-After` exactly; exponential backoff is for network/5xx only.
  - Repeated 429 days trigger a warning email to the account, then token revocation /
    account disable. One client, ONE throttle: never run a second process (sweep, script,
    `docker exec`) with its own pacing against Metron — route everything through
    `kometa/metron_client.py` in the app process.
  - Reduce calls: use filters and `modified_gt`; `If-Modified-Since` saves bandwidth, not
    requests. Admin contact: admin@metron.cloud; Matrix `#metrondb:matrix.org`.
- **SABnzbd**: `http://$NAS_HOST:8080`, api_key in `/volume1/docker/config/sabnzbd/sabnzbd.ini`.
- **Prowlarr** (indexer proxy): `http://$NAS_HOST:9696`, ApiKey in
  `/volume1/docker/config/prowlarr/config.xml`. Usenet indexers are NZBFinder (id 9) +
  NZBGeek (id 18). Kometa's `newznab_indexers` config = `[{name, host:"$NAS_HOST:9696/<id>",
  apikey:<prowlarr key>, ssl:false}]` (the client appends `/api`).
- **Shared downloads (arr-stack convention)**: Kometa's `/downloads` MUST mount the SAME
  host folder SAB uses (`/volume1/docker/media/downloads`), so SAB's reported
  `/downloads/complete/…` path resolves in Kometa for usenet finalize. SAB reports a
  single grab's `storage` as the FILE path (not a dir), so finalize scans
  `dirname(storage)` when it's a file.

## Browser passes and source health (2026-10-13)

- **Fandom images** (`static.wikia.nocookie.net`) sit behind a Cloudflare challenge for this IP. Chrome is waved
  through and never holds a pass; **Safari** is challenged and earns `cf_clearance`. Paste it in Settings → Fandom
  (or in chat: `PATCH /api/config {"fandom_cf_clearance", "fandom_user_agent": <Safari's exact UA>}` then
  `POST /api/test/fandom`). A pass lives **30 minutes**; the lane runs at 1/s while one is live, and a 403 forgets it.
- **LOCG**: same routine, `locg_cf_clearance` + `locg_user_agent`, then `POST /api/test/locg`.
- **Image downloads** run one lane per host (`kometa/images.py LANES`): own gap, daily cap, an hour's rest on
  403/429/503. LOCG's S3 bucket says 403 for a missing key, so its lane only rests on 429/503.
- **Source health** (`kometa/health.py`): every outside request reports ok/refused/error; 5+ failures in a row for
  30+ min, or nothing working for a day, lights that source's Settings card. Watching logs from the Mac: give
  `docker logs --since` a UTC time with a trailing `Z` — without it the host reads it as local time.

## Conventions

- Extract a cohesive cluster, alias-import it back into `main` (`from kometa.x import
  foo as _foo`) so call sites don't change, then verify (compile + boot + a functional
  test) before committing. One slice per commit.
- Architecture/module map lives in `CONTEXT.md`. Keep it current.

## New features: research → mock-up → build

For anything with real UI or product decisions (not bug fixes), in this order:

1. **Decide** — work out what's being built one question at a time, each with a
   recommendation, until there's a shared understanding. Look up facts (DB, catalogues,
   files) rather than asking for them; put the decisions to the owner.
2. **Research** prior art from primary sources → `docs/<feature>-research.md`, cited.
3. **Spec** the agreed decisions → `docs/<feature>-spec.md`. Layout is NOT decided
   here — it's left open for step 4.
4. **Mock up** in Pencil (`kometa.pen`, the Pencil desktop app — not VS Code) and get
   sign-off on how it looks.
5. **Build**, then the usual test → commit → deploy.

Example: the reader — `docs/reader-research.md`, `docs/reader-spec.md`.
`docs/` is in a PUBLIC repo: no hosts, IPs, usernames or personal details.

## Styling / design tokens

- All design tokens are CSS variables in `:root` at the top of
  `kometa/static/style.css` — colours, motion (durations `--t-*`, easing
  `--ease-*`, magnitudes `--blur/--zoom/--rise/...`), and type (`--font`, currently
  Victor Mono — one family everywhere). Use the variables everywhere; never hardcode
  a hex, duration, easing curve, or font-family (this applies to inline styles set
  from `app.js` too).
- `palette.html` (repo root) is a living visual reference of every token — colours
  (hex, role, usage count), the overlay/effects, and a Motion section with live
  duration/easing demos. **Whenever a token changes in `style.css`, update
  `palette.html` in the same pass** so the reference never drifts. Open it with
  `open palette.html`.
- Motion convention: hover = one step lighter; `--t-fast` hovers, `--t-slow`
  entrances, `--t-mid` exits (snappier than entrances), `--t-scrim` backdrop.
- Don't change existing styles/tokens without asking first.


## Backups (2026-10-11)

**Never open the live database from the Mac side.** No `sqlite3 ~/docker/apps/kometa/data/kometa.db …` on ginbako — not even a read, not even `.backup`. macOS sqlite and the container share the WAL lock files (`-shm`) through OrbStack and don't agree on them: on 2026-10-13 a host-side `.backup` left every container read failing with `disk I/O error`, surviving a restart. Copies (pre-deploy safety copies included) are made inside the container:
`docker exec kometa python -c "import sqlite3; s=sqlite3.connect('/data/kometa.db'); d=sqlite3.connect('/data/kometa.db.pre-<what>-<date>'); s.backup(d)"`. The host may open a finished copy freely.
Recovery if it happens again (WAL must be 0 bytes, else stop and ask): `docker compose stop kometa`, move `kometa.db-shm` + `kometa.db-wal` aside, `docker compose start kometa`, then `pragma quick_check` from inside.

Three copies of everything Kometa needs:

1. **Kometa's own nightly** (03:30, `kometa/backup.py`): the database and a cover mirror into `_kometa-backup/` inside the comics folder on the NAS. Keeps 7 nights.
2. **Voot II** (02:00, `ops/ginbako-backup.sh`, run by launchd from `~/Library/LaunchAgents/dev.kometa.backup.plist` on ginbako): onto the external drive Voot II at `/Volumes/Voot II/Kometa-Backup/`:
   - `db/` — a copy of the live DB made inside the container (`docker exec kometa python … backup()`), integrity-checked on the host, 14 kept
   - `covers/`, `docker/` (all of `~/docker` except the page cache — which now lives on Catbus anyway), `comics/` (mirror of `/Volumes/🧳/Comics`)
   - `deleted/YYYY-mm-dd/` — whatever the mirrors removed or replaced, kept 30 days (a bad delete in the library can't wipe the backup)
   - `status.json` (last run) and `backup.log` (every run)
   - It refuses to run if Voot II isn't mounted. `COMICS=0` skips the comics mirror for a quick test.
   - **Needs GNU rsync** (`brew install rsync` → `/opt/homebrew/bin/rsync`). macOS's `/usr/bin/rsync` is Apple's openrsync,
     which with `--backup` silently skips `--delete`: files gone from the library stay on Voot II forever and nothing
     reaches `deleted/` (found 2026-10-14, 731 stale CBRs). Without GNU rsync the script skips each mirror and says so.
   - Install / update: copy `ops/ginbako-backup.sh` to `~/bin/kometa-backup.sh`; the plist is a template — `sed "s#__HOME__#$HOME#g" ops/dev.kometa.backup.plist > ~/Library/LaunchAgents/dev.kometa.backup.plist`; then `launchctl bootout gui/$(id -u)/dev.kometa.backup; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.kometa.backup.plist`. Run now: `launchctl kickstart gui/$(id -u)/dev.kometa.backup`.
   - **macOS privacy:** a launchd job can't write to an external drive until `/bin/bash` has Full Disk Access (System Settings › Privacy & Security › Full Disk Access › +, Cmd-Shift-G `/bin/bash`). Without it the run fails with "No such file or directory" on the drive.
3. **Code**: both repos pushed to GitHub (off ginbako) as well as Gitea.

## Reader caches on Catbus (2026-10-12)

`data/page-cache` (~21 GB) and `data/cover-cache` (~216 MB) live on Catbus, the 2 TB USB SSD on ginbako, to free its internal disk. They're bind-mounted over the same container paths, so the code doesn't know they moved:

- Host: `/Volumes/Catbus/kometa/page-cache` and `/Volumes/Catbus/kometa/cover-cache` → container `/data/page-cache`, `/data/cover-cache` (long-syntax binds in `~/docker/compose.yml`).
- Both binds set `create_host_path: false`. If Catbus isn't mounted, kometa refuses to start with "bind source path does not exist" rather than Docker creating an empty folder under `/Volumes/Catbus` that squats the mount point. **Never** pre-create anything under `/Volumes`, and never start kometa before Catbus is up. Fix a refused start by mounting Catbus and then `docker compose up -d kometa`.
- What stays on the internal disk: `kometa.db` only.
- **The record cover store `data/covers` moved to Catbus too (2026-10-13)**: `/Volumes/Catbus/kometa/covers` → `/data/covers`, same `create_host_path: false` bind. It is NOT regenerable (Marvel.com/PRH/CV originals), so it has three copies: Catbus, Voot II's `covers/` (02:00 — the script checks Catbus is mounted and non-empty before a `--delete` mirror, and skips loudly otherwise) and kometa's own 03:30 NAS backup. The pre-move internal copy sits at `data/covers.moved-20261013` until a backup has passed; then delete it.
- Both caches rebuild themselves, so if Catbus dies, kometa runs again once the two folders exist on a mounted drive.
- Speed check: `ops/bench-cache.py` (run in the container, see its docstring). Drop the VM cache first: `docker run --rm --privileged alpine sh -c "sync; echo 3 > /proc/sys/vm/drop_caches"`. The 2026-10-12 before/after results are in `data/bench/`.

## Build progress page (2026-10-12)

`/progress.html` shows the library-enrichment build: live harvest numbers (from `/api/harvest/progress`), the checklist, and the user's task list. The checklist and tasks come from `kometa/static/build-status.json`.
- Update `build-status.json` at every milestone: an item changes state (`todo` → `building` → `done`, or `waiting-you`), a task for the user is added or answered, or the "now" line changes. Bump `updated`.
- It's a static file: sync it (deploy) and it's live; no restart needed.
- The user reads this page instead of asking for status. Keep tasks self-contained: what, why, how, blocking or not.

## Full ComicInfo rewrite — batch run (Thursday)

Step 3f of docs/library-enrichment-spec.md, done as three hand-started phases in
`kometa/retag_batch.py`, on top of the tested engine in `kometa/retag.py` (its swap
safety is unchanged). Every file is read from a snapshot of the library on Catbus
(falling back to the NAS only when the copy isn't the NAS file), built and verified on
Catbus, checked by the user, and only then swapped onto the NAS.

- Arming: config `retag_batch_armed = "1"` (PATCH /api/config). When unarmed, the queue/build/swap
  routes answer 403. Stop (`POST /api/retag/batch/stop`) and the read-only routes always answer.
- While armed, the scheduler's `retag_tick` stands down. Otherwise it would swap verified rows
  between the phases, before the user's OK. One phase runs at a time; a second start answers 409.
- Build refuses (409) when `/retag/source` is missing or empty. It stops itself when more than half
  of its first 25 builds fall back to the NAS (the snapshot doesn't match), or when Catbus drops below 20 GB free.
- A snapshot copy counts when its size matches, its mtime is within 2 s, and (for a CBZ) its central
  directory lists the same pages as the NAS file's.
- Space: the snapshot is ~470 GB and the builds are about the same again (~940 GB). Catbus has 1.7 TB free.
- The build phase journals `build_source` (`snapshot`/`nas`) for each row. The swap phase marks an original that
  changed since it was stamped as `stale` and never touches it; a re-queue picks up stale and failed rows again.

`$K` below = `http://$KOMETA_HOST:6969`. Run host commands on ginbako; `docker` is `/usr/local/bin/docker`.

0. **Before Thursday:** deploy the code (`./deploy.sh`) and install the backup script
   (`cp ops/ginbako-backup.sh ~/bin/kometa-backup.sh` on ginbako). Check the last nightly ran clean:
   `cat "/Volumes/Voot II/Kometa-Backup/status.json"`. The snapshot is only as fresh as that run.
1. **Pause the comics backup:** `touch ~/docker/apps/kometa/data/.pause-comics-backup`.
   The 02:00 run then logs `comics PAUSED (retag batch)` and still copies the DB, covers and docker.
   Make sure no mirror is running now: `pgrep -fl rsync` returns nothing.
2. **Snapshot** Voot II's mirror onto Catbus (`-a` keeps the mtimes, which the build compares):
   ```bash
   mount | grep -q " on /Volumes/Catbus " && mount | grep -q " on /Volumes/Voot II " && \
   mkdir -p /Volumes/Catbus/kometa/retag/source /Volumes/Catbus/kometa/retag/build && \
   /usr/bin/rsync -a --exclude "_trash/" --exclude "_kometa-backup/" --exclude "@eaDir/" \
     --exclude "#recycle/" --exclude ".DS_Store" \
     "/Volumes/Voot II/Kometa-Backup/comics/" /Volumes/Catbus/kometa/retag/source/
   du -sh /Volumes/Catbus/kometa/retag/source; df -h /Volumes/Catbus
   ```
3. **Bind it into the container.** In `~/docker/compose.yml`, under the kometa service's
   `volumes:`, beside the other Catbus binds:
   ```yaml
      - type: bind
        source: /Volumes/Catbus/kometa/retag
        target: /retag
        bind:
          create_host_path: false
   ```
   Then run `cd ~/docker && docker compose up -d kometa`. This is a recreate; see the NFS note above if it
   complains. Check: `docker exec kometa ls /retag/source | head`.
4. **Arm, queue, build:**
   ```bash
   curl -s -X PATCH -H 'Content-Type: application/json' -d '{"retag_batch_armed":"1"}' $K/api/config >/dev/null
   curl -s $K/api/retag/batch/dry-count            # would_queue, would_requeue_changed, catalogue vs stopgap
   curl -s -X POST $K/api/retag/batch/queue
   curl -s $K/api/retag/batch-report | jq .progress  # wait for finished_at
   curl -s -X POST $K/api/retag/batch/build
   curl -s $K/api/retag/batch-report | jq '.progress | {done, total, from_snapshot, from_nas, counts, stopped, error}'
   ```
   In the first minutes, `from_nas` should be close to 0. If it isn't, stop
   (`curl -s -X POST $K/api/retag/batch/stop`) and check the snapshot.
   A re-run of build skips what's already verified, so stopping costs nothing.
5. **Check the report and spot-check:**
   ```bash
   curl -s "$K/api/retag/batch-report?sample=10" | jq '{states, build_source, problems, cbr_to_cbz: .cbr_to_cbz.total, staged, disk, sample}'
   curl -s $K/api/retag/batch/staged/<book_id>/comicinfo        # what will be written (X-Journal-Digest must match)
   curl -s $K/api/books/<book_id>/comicinfo-preview             # what the record says today
   ```
   Copy the sampled builds off Catbus (`/Volumes/Catbus/kometa/retag/build/<book_id>.cbz`) with a
   tar pipe and open them in a second reader. Check that every page is there, in order, and that the new details show.
   Open the same books in Kometa's reader (still the NAS originals at this point) to compare.
6. **User's OK** before anything is written to the NAS.
7. **Swap:** `curl -s -X POST $K/api/retag/batch/swap`. It does one file at a time and pauses while anyone is
   reading. Progress is in the same report. Stopping and restarting is safe at any point.
8. **Verify the NAS:** the report's `states` holds only `swapped`, `skipped`, `stale` and `failed`
   (no `verified` or `pending`), and `staged.files` is 0. Read the reasons in `problems`. Spot-check: open the
   sampled books in Kometa's reader and in a second reader straight off the NAS, and read a tag back:
   `docker exec kometa python -c "import zipfile,sys; print(zipfile.ZipFile(sys.argv[1]).read('ComicInfo.xml').decode())" "/comics/<path>"`.
9. **User's OK.**
10. **Unpause the backup and run it once by hand.** It re-copies about 455 GB, which takes about 5 h:
    `rm ~/docker/apps/kometa/data/.pause-comics-backup && launchctl kickstart gui/$(id -u)/dev.kometa.backup`,
    then `tail -f "/Volumes/Voot II/Kometa-Backup/backup.log"` until `--- done ok=1`.
11. **Confirm Voot II:** a sampled file under `Kometa-Backup/comics/` now holds the new ComicInfo (run the unzip
    from step 8 against the Voot II path), and `Kometa-Backup/deleted/<day>/comics/` holds the originals
    (kept 30 days). The CBR originals are also in the NAS `_trash/<publisher>/retag-cbr-<date>/` (7 days).
12. **User's OK.**
13. **Clean up**, in this order: disarm
    (`curl -s -X PATCH -H 'Content-Type: application/json' -d '{"retag_batch_armed":"0"}' $K/api/config`),
    remove the `/retag` bind from `compose.yml` and run `docker compose up -d kometa`, then
    `rm -rf /Volumes/Catbus/kometa/retag`. Remove the bind before deleting the folder, so the container never
    holds a deleted mount.

## Long jobs beside Kometa (2026-10-15)

A job started with `docker exec -d kometa …` dies with every `compose restart` (every deploy) and can't be
killed from inside the container (its own `/proc` doesn't show it; the image has no `pkill`). Run long
read-only jobs (the library page check, scans) as a sibling container that shares Kometa's volumes instead:

    docker run -d --name libcheck --volumes-from kometa --entrypoint sh docker-kometa \
      -c "cd /app && nice -n 15 python /data/libcheck.py >> /data/libcheck.log 2>&1"

`--volumes-from` carries `/comics`, `/downloads` and `/data`; `cd /app` (or `PYTHONPATH=/app`) is what makes
`import kometa` work for a script living in `/data`. One-offs: the same with `--rm` and no `-d`.

## Accounts and login (2026-10-14)

Spec: docs/accounts-spec.md. Code: kometa/auth.py (gatekeeper, routes, CLI).

- **Switch:** config `auth_enabled` ("0" = everyone is the admin, the old world; "1" = every request shows a credential).
  `docker exec kometa python -m kometa.auth enable on|off` (refuses `on` with no admin account).
- **Accounts:** `docker exec -it kometa python -m kometa.auth add <username> [--admin]` (asks the password, no echo),
  `… list`, `… password <username>` (signs every device out), `… revoke-all <username>`. The first admin claims
  the existing `me` reading state. Run these from a shell ON ginbako: a multi-line `ssh … '…'` pasted from chat
  wraps and runs the wrong thing.
- **Locked out:** the CLI needs no login — `password` or `enable off` from ginbako.
- **Claude's token:** `~/.kometa-claude-token` on ginbako (600), made with `… token marcus claude-code`. With login
  on, every API call from automation sends `Authorization: Bearer $(cat ~/.kometa-claude-token)` — run curls on
  ginbako (`ssh … 'T=$(cat ~/.kometa-claude-token); curl -H "Authorization: Bearer $T" http://127.0.0.1:6969/…'`).
  `… password marcus` signs out every device of marcus, this token included: reissue it straight after
  (`umask 077; docker exec kometa python -m kometa.auth token marcus claude > ~/.kometa-claude-token`).
- **Clients:** web = the /login page (cookie, a year); YondeB = Settings › Server (token in the Keychain);
  OPDS apps = Basic (username + password). A reader account gets the shelf and its own reading state only; the
  web app is admin only.
- **Turn-on order:** install the current YondeB everywhere → admin account → sign in on web, YondeB (each device),
  Panels → `enable on` → sign in on the iPad home-screen web app (it shows the sign-in page) → only then expose
  it (Cloudflare tunnel hostname + Access on the web app, never /api or /opds).

## Public access: Cloudflare Tunnel + Access (2026-10-14)

Kometa is reachable away from home at `https://$KOMETA_PUBLIC_HOST` (real name in `.env`, never here).
- **Tunnel:** the `kometa-tunnel` service in ginbako's `~/docker/compose.yml` (`cloudflare/cloudflared`, `tunnel --no-autoupdate run`).
  Its token is `KOMETA_TUNNEL_TOKEN` in `~/docker/.env` (mode 600), never in compose or this repo. Same compose
  network as Kometa, so the dashboard route is **Published application** `$KOMETA_PUBLIC_HOST` → `http://kometa:6969`.
  Token rotation: dashboard › Tunnels › Kometa › Refresh token, then update `.env` and `docker compose up -d kometa-tunnel`.
- **The public address carries the apps, nothing else** (kometa/auth.py `AuthGate`, `via_tunnel`): a request with
  `Cf-Connecting-IP` (Cloudflare stamps it, a client can't strip it) that isn't `/api/…` or `/opds/…` gets
  `FRONT_PAGE`, a green コメタ and nowhere to go. That's 200 at `/` and 404 elsewhere, even with an admin token.
  The web app is home or tailnet only. With login OFF, the tunnel gets 503 for everything.
- **Access (Zero Trust › Access controls › Applications):** only `kometa apps` (paths `api` and `opds`, **Bypass**
  Everyone). YondeB and OPDS readers can't do Cloudflare's browser login. Kometa's login guards them. There's no
  Access app on the bare hostname: the front page has nothing behind it.
- **Check after any change** (from off the LAN or anywhere):
  `/` → 200 with only コメタ; `/login` and `/app.js` → 404; `/api/version` → 200; `/api/series` and `/opds` → 401;
  the same two with `Authorization: Bearer <token>` → 200.
- Never widen `_app_path` to anything that serves the web UI.
