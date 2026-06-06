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
