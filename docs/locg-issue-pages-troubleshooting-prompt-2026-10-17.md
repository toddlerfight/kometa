# Troubleshooting brief: Kometa can't read League of Comic Geeks issue pages (2026-10-17)

## Goal

Kometa, a self-hosted comic library app, needs to read LOCG **issue pages**, for example
`https://leagueofcomicgeeks.com/comic/3728658/100-bullets-the-us-of-anger-4`. Those pages carry an
issue's credits and solicitation text before release; no other catalogue has them that early. The
reads would be automatic but gentle: a handful a day, at least 10 s apart. Find out **why** the
server's reads fail, and either find a reliable way for them to work or conclude that none exists.
Report what you tested and what each test returned.

## What works and what doesn't (2026-10-17, Brisbane time)

| Route | LOCG lists and search (`/comic/get_comics?list=series&series_id=…`, search) | LOCG issue pages (`/comic/{id}/…`) |
|---|---|---|
| The owner's everyday Chrome on his Mac | works | **works** (loads with credits) |
| FlareSolverr 3.5.2 (latest image), on the home NAS, via Kometa | works ("Challenge not detected", 7–12 s) | **fails** since about 15:30: "Error solving the challenge. Timeout after 25.0 seconds"; with a 120 s timeout it fails the same way |
| Plain HTTP (curl, a fetch tool) | n/a | 403 |
| Playwright with the **installed Chrome in headless mode**, a fresh persistent profile, from the owner's Mac (same home network as the working Chrome), 20 different issue pages 10 s apart | n/a | **0 of 20**: each answered in under 0.3 s with HTTP 403 and a page titled **"Restricted"** (no Cloudflare interstitial) |

Earlier the same day FlareSolverr did clear LOCG's home page twice (in 9 s and 12 s).

## Background

- **Kometa's LOCG client** (`kometa/locg_client.py`, Python, inside a Docker container on the
  server, a Mac mini running OrbStack):
  - `_access()` gets a `cf_clearance` cookie and a user agent from FlareSolverr (`flaresolverr.clearance`).
    If that fails, it uses a pass pasted into config (`locg_cf_clearance`, `locg_user_agent`).
  - Requests go out through a `curl_cffi` session impersonating Chrome.
  - While FlareSolverr is enabled, each GET goes through FlareSolverr's `request.get` instead
    (`_flaresolverr_get`). Kometa still sends a `headers` parameter, which FlareSolverr v2 ignores
    with a warning.
- **A pasted Safari pass** (Safari's user agent plus its `cf_clearance`) worked on 2026-10-14. A
  pass copied from Chrome was refused, consistent with Cloudflare tying a clearance to the browser
  that earned it.
- **Prior research:** `docs/research/comic-data-sources.md` §1 (LOCG). LOCG's official API is for
  retailers only, and no sign-up exists. Every active LOCG scraper found in 2026 drives a real
  browser.
- **Bug fixed today** (`f316a38`): JSON that came back through FlareSolverr wasn't HTML-unescaped,
  so every LOCG list parsed to zero rows. Lists now parse; issue pages are a separate problem.
- **Kometa's issue-page sweep now rests** 1 h → 2 → 4 … up to 12 h after a run of failures, so the
  server isn't hammering LOCG while you test.

## Questions to answer, cheapest first

1. **What is "Restricted"?** Capture the full response for one blocked request: status, headers
   (look for Cloudflare's markers such as `cf-ray` and `cf-mitigated`, and the `server` header) and
   body. Is it Cloudflare's block, or LOCG's own application turning the request away? What in the
   request is it keyed on?
2. **What does the working Chrome send that the others don't?** From the owner's Chrome DevTools
   (Network tab, the issue-page request): the cookies (is he signed in to LOCG? which cookies:
   `cf_clearance`, `__cf_bm`, a session cookie?), the user agent and the `sec-ch-ua` headers.
   Does the same page load in that Chrome's **incognito** window, with no cookies and not signed in?
   If incognito fails, issue pages may now need a signed-in or established session, which changes
   the whole approach.
3. **Is it headless?** Load one issue page with the same installed Chrome **headed** (a normal
   window, normal profile, launched by hand, not by an automation tool), from the server and from
   the Mac. Then one page driven by Playwright, headed. Which of these load?
4. **FlareSolverr's view.** For one issue page, what does FlareSolverr's raw answer contain: a
   challenge page, the "Restricted" page, or nothing? Is the failing challenge a Turnstile widget
   (which FlareSolverr can't solve) or the automatic check?
5. **The pass route.** Does Kometa's own test (`POST /api/test/locg` with an admin token) pass with
   a fresh Safari pass pasted into config? Does an issue page then load through Kometa's
   `curl_cffi` path?
6. **The network.** The owner's Chrome works from the same home address, so a block on the address
   is unlikely. Confirm it anyway (one request from another network, if handy).

## Rules

- **At most 30 requests to LOCG in total,** at least 10 s apart. Stop at the first sign that it's
  getting stricter (a page that worked starts failing).
- **No CAPTCHA-solving services, no residential proxies.** Respect LOCG's terms: if the honest
  answer is "LOCG doesn't allow this", that's a valid result.
- **Code** in the Kometa repo (if you touch it): read INSTRUCTIONS.md first. Commit and stop, never
  deploy, never edit an existing test, and call outside services only through their module.
- **Keep it out of the public repo:** no LAN addresses, hostnames, tokens or cookies.

## Deliver

A short report:
- what "Restricted" is;
- the answer to each question above, with what was observed;
- the access method that works, if any, and what it would take on the server (a headed Chrome in
  the logged-in desktop session, a pass, something else), with its ongoing cost (passes expiring, a
  browser to keep alive);
- or the conclusion that issue pages aren't reachable, and why.
