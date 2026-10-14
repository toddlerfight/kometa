# Accounts and connecting — spec (2026-10-14)

Decisions from the 2026-10-14 session. Kometa gets accounts so that more than one
person can read from one Kometa. YondeB becomes a reader first that can connect to
any OPDS server, and gets the full experience when that server is a Kometa.

## Who is who

- **Admin** — the owner. The web app (acquisition, Activity, Settings, every report and
  tool) is admin only. The admin's reading state is the existing `reader_id = "me"`.
- **Reader** — anyone else. Reads through YondeB or any OPDS app. Never touches
  acquisition. A reader who opens the web app gets a plain "not for this account" page.
- **Everyone logs in once per app** (option 1). Kometa cannot tell callers apart by
  address: every request arrives from Docker's gateway address or localhost,
  whether it is the iPad at home or a tailnet peer through Tailscale serve (:8445). So
  there is no trusted-network shortcut; one rule for every caller.

## Switch

`auth_enabled` (config, default `"0"`). Off = today: every request is the admin, no login.
On = every request must carry a credential. It is turned on once the owner has signed in
on each device. Locked out → `docker exec kometa python -m kometa.auth …` (below).

## Credentials

| Client | Credential |
|---|---|
| Web app | `kometa_session` cookie (HttpOnly, SameSite=Lax, 1 year), set by the login page |
| YondeB | `Authorization: Bearer <token>` from `POST /api/auth/login`, kept in the Keychain |
| OPDS apps (Panels, Chunky…) | HTTP Basic (username + password); `/opds` answers 401 with `WWW-Authenticate: Basic` |
| Scripts (and Claude's curl) | a long-lived token made with the CLI |

Tokens are random 32-byte strings, stored only as SHA-256. Passwords are scrypt
(stdlib `hashlib.scrypt`, n=2^14, r=8, p=1, 16-byte salt). Each token has a name
(device), a created time and a last-used time; logging out revokes the one token.

## Endpoints

- `POST /api/auth/login` `{username, password, device?}` → `{token, user}`; also sets the cookie.
- `POST /api/auth/logout` — revokes the presented token, clears the cookie.
- `GET /api/auth/me` → `{id, username, role, auth_enabled}` (works with auth off: the admin).
- `GET/PATCH /api/me/prefs` — the reader's preferences (JSON object, merged on PATCH).
- `GET /api/me/pull-list`, `PUT /api/me/pull-list/{series_id}` `{on}` — the reader's own pull list.
- Admin: `GET/POST /api/users`, `PATCH/DELETE /api/users/{id}` (role, password reset, disable),
  `GET /api/users/{id}/tokens`, `DELETE /api/users/{id}/tokens/{token_id}`.
- CLI: `python -m kometa.auth list | add <username> [--admin] | password <username> | token <username> <name> | revoke-all <username>`.
  The first admin claims the existing `"me"` reading state.

## What a reader may call

One gatekeeper middleware resolves the caller, sets the current reader for the request
(a contextvar, so it reaches the threadpool and every `reader_id` default), and checks a
**reader allow-list**. Anything not on the list is admin only, so a new route is locked
until it is listed.

- GET: `/api/version`, the shelf (`/api/series`, `/api/series/{id}` and its status, thumbnail,
  related, top-rated, trades, arcs, issue book/thumbnail/variants), `/api/shelf…`, `/api/books/{id}`
  and its cover, pages, next, community, `/api/cover-store/…`, `/api/cover-upgrade/…`, `/api/img`,
  `/api/creators/{id}`, `/api/discover/…`, `/api/ondeck…`, `/api/trending`, `/api/pull-list`,
  `/api/readlists`, `/api/readlists/{id}` and its next and item covers, `/api/search`,
  `/api/search/people`, `/api/wantread…`, `/api/push/status`, `/api/trade/{id}/details`, `/opds/…`.
- Writes, to the reader's own state only: `PUT /api/books/{id}/progress`, `PUT /api/books/{id}/mark`,
  `POST /api/books/{id}/dismiss`, `PUT /api/series/{id}/mark`, `POST/DELETE /api/wantread`,
  `POST /api/push/devices`, `POST /api/push/test`, `/api/me/…`, `/api/auth/…`.

## Per-person

- Already keyed by reader: progress (+ Not now), marks (ratings, favourites), want to read.
- Now keyed: push devices (owner column), preferences, a reader's pull list.
- **Pull list:** the admin's is `tracked_series.on_pull_list` and is the only one that downloads.
  A reader's (`reader_pull`) drives only their notifications, On Deck and YondeB's calendar.
- **Push:** a landed issue notifies the admin as today (`push_scope`); a reader is notified for
  series on their pull list, and (pref `push.reading`, default on) series they are reading or
  have favourited.
- Preferences that follow the person: library sort and chips, On Deck rows, push choices,
  Activity "last seen". Reader layout (two-up, fit, indicator) stays per device on purpose.
- Background jobs (harvests, twins, CBR people seeds) keep running as the admin.

## YondeB — connect

One "Connect a server" screen: an address, optional username and password (Keychain).

1. Try `<address>/api/version`. Answers → Kometa: log in with `POST /api/auth/login` if needed,
   the full app.
2. Otherwise fetch the address as OPDS. An OPDS root carrying
   `<link rel="https://kometa.dev/rel/api" href="/api"/>` is a Kometa: confirm with `/api/version`
   and switch to the full app. Any other OPDS catalogue: browse, stream (PSE) and download,
   progress kept on the device.
3. No server: the device is the library (already built, e60b578).

## Order

1. Kometa: `kometa/auth.py` (tables, hashing, tokens, middleware, routes, CLI), the reader
   switch, push owners, prefs, reader pull list, OPDS Basic + the marker link, the login page.
   Tests. Deployed with `auth_enabled = 0`.
2. Turn on (with the owner home): sign in on the web app, the iPad web app, YondeB, Panels;
   make Claude's token; then `auth_enabled = 1`.
3. YondeB: connect screen, Kometa login, OPDS source.
