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
.venv/bin/python -m uvicorn kometa.main:app --host 127.0.0.1 --port 6970 --log-level warning
# smoke test in another shell:
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:6970/api/series   # expect 200
```

`init_db` runs on startup and builds a fresh schema cleanly (migrations are idempotent).

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

## Testing extracted logic

The logic modules (`naming`, `sync`, `acquisition`, `sources`, `db`) are testable in
isolation. Pattern: set `KOMETA_DB` to a temp file, `db.init_db(path)`, seed rows,
inject fakes for source accessors (e.g. `acquisition._sabnzbd = lambda: None`), call
the function, assert DB state. No live external APIs required.

## Deploy to the NAS (only when explicitly approved)

Live runs as a Docker container on the NAS. Deploy is a **full rebuild** (not the old
piecemeal `cat >` deploy.sh, which silently reverts on container recreate):

1. Safety: tag the running image as a rollback point, snapshot `data/kometa.db`.
2. `tar`-pipe the full source (`kometa/`, `requirements.txt`, `Dockerfile`,
   `docker-compose.yml`) to `/volume1/docker/kometa/` (no rsync on the NAS).
3. `docker compose build && docker compose up -d` (recreates container; the DB volume
   at `/volume1/docker/kometa/data` persists).
4. Verify: container logs show clean startup, `curl :6969` returns 200.

## Conventions

- Extract a cohesive cluster, alias-import it back into `main` (`from kometa.x import
  foo as _foo`) so call sites don't change, then verify (compile + boot + a functional
  test) before committing. One slice per commit.
- Architecture/module map lives in `CONTEXT.md`. Keep it current.
