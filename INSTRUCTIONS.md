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
and `_komga()` None paths guarded by `try/except` — not real bugs.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt   # one-time: pulls in pytest
.venv/bin/python -m pytest                       # runs tests/ (see pytest.ini)
.venv/bin/python -m pytest tests/test_acquisition.py -q   # one file
```

The suite (`tests/`) runs against a throwaway SQLite DB per test (`db_path` fixture
in `conftest.py`) and injects fakes for every external source — no GetComics,
SABnzbd, Komga, or network. Coverage: `naming` pure parsers, `db` (atomic
`complete_download`, queue requeue rules, the fresh-install migration), and
`acquisition` (`_process_queue` happy/not-found paths, `_finalize_usenet_download`
file moves). Dev-only deps live in `requirements-dev.txt` and never ship to the
container.

Pattern for new tests: use the `db_path`/`series` fixtures, `monkeypatch.setattr`
the acquisition module's `DB_PATH` + source accessors (`acq._sabnzbd`, `acq._komga_scan`,
`acq.GetComicsClient`), call the function, assert DB/disk state.

## Deploy to the NAS (commit-then-deploy — no per-change approval needed)

Deploys do not require per-change approval while the NAS is the active test
environment (rule changed 2026-06-10). The non-negotiable safety net: **every
deploy is preceded by a commit pushed to the canonical remote**, so there is
always a point-in-time to roll back to. Remotes (set 2026-06-18):
- `origin` → `https://github.com/toddlerfight/kometa.git` (GitHub, **private**, canonical) — `git push origin main` is the pre-deploy rollback point.
- `gitea` → `ssh://git@$NAS_HOST:2222/<nas-user>/kometa.git` (NAS Gitea, fast local mirror). Push here too when convenient: `git push gitea main`.

Rollback = `git checkout <commit> -- <files>`, re-sync, restart. Destructive
operations (anything that touches library files or the DB schema) still need
explicit approval.

Live runs as a Docker container on the NAS (container name `kometa`, port 6969).
NAS access: `ssh -p $NAS_PORT -i ~/.ssh/id_ed25519 <nas-user>@$NAS_HOST`. Docker binary:
`/var/packages/ContainerManager/target/usr/bin/docker`. No rsync/scp — use `tar`-pipe.

**The source is now BIND-MOUNTED** (`/volume1/docker/kometa/kometa:/app/kometa` in
`docker-compose.yml`), so code is NOT baked into the image. This makes deploys a
**few-second restart, not a rebuild** (the old rebuild caused ~1-2 min downtime — see
the 2026-06-09 git history of frustration). Pick the path by what changed:

- **Python change** → tar-sync the changed file(s) into the live mount, then restart:
  ```bash
  tar czf - kometa/sync.py kometa/main.py | ssh -p $NAS_PORT -i ~/.ssh/id_ed25519 \
    <nas-user>@$NAS_HOST 'cd /volume1/docker/kometa && tar xzf -'
  ssh -p $NAS_PORT -i ~/.ssh/id_ed25519 <nas-user>@$NAS_HOST \
    'cd /volume1/docker/kometa && /var/packages/ContainerManager/target/usr/bin/docker compose restart kometa'
  ```
- **Static change** (`static/app.js`, `style.css`, `index.html`) → tar-sync only;
  it's served live, **no restart needed**. Tell the user to hard-refresh (Cmd+Shift+R)
  — the SPA is browser-cached (`Cache-Control` on assets), so a stale cached app.js is
  what makes "old UI / wrong behaviour persists after a fix" appear.
- **requirements.txt / Dockerfile / docker-compose.yml change** → still a real
  recreate: `docker compose up -d` (compose change) or `... build && ... up -d` (deps).
- **NEVER** `rm -rf /volume1/docker/kometa/kometa` while the container is up — it's the
  live mount; deleting it yanks the code out from under the running app. Sync files in
  place (tar extract overwrites). `compose restart` needs `cd /volume1/docker/kometa`
  first (or `-f`), or you get "no configuration file provided".

Always: after, `curl http://$NAS_HOST:6969/api/series` → 200, and confirm the new
code is actually live, e.g. `docker exec kometa grep -c <new-symbol> /app/kometa/<file>`.
Validate locally first (`ruff check kometa/`, `pytest -q` = 78 tests, `node --check
kometa/static/app.js` for JS). The `.venv` has no git → snapshot files to `/tmp` before
risky edits.

## NAS runtime integrations

- **Komga** (reader + cover source): `http://$NAS_HOST:8585`. Creds in
  `/volume1/docker/kometa/.env`. Linked per-series via folder-path match (Komga's
  `series.url` == Kometa `folder_path`) — unambiguous, unlike title matching.
- **Komga numberSort lies**: Komga's own issue-number parsing is unreliable (counts
  TPBs/dupes, mis-parses mixed naming). Kometa derives the true number from the FILENAME
  and (a) keys its book map on it, (b) pushes it back to Komga on every sync via
  `KomgaClient.set_book_number` (locked `numberLock`/`numberSortLock`) so Komga's own
  labels AND ordering are correct. After re-writing a CBZ (variant cover inject), call
  `analyze_book` so Komga re-extracts the cover.
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

## Conventions

- Extract a cohesive cluster, alias-import it back into `main` (`from kometa.x import
  foo as _foo`) so call sites don't change, then verify (compile + boot + a functional
  test) before committing. One slice per commit.
- Architecture/module map lives in `CONTEXT.md`. Keep it current.

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
