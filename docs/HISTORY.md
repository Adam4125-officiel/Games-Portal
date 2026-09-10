# History

The narrative archive: what actually broke, how it presented, and what's been
verified against a real deployment (and when). Rules belong in `CLAUDE.md`; the
stories behind them belong here. When `CLAUDE.md` gains a rule because something
broke, it links to the write-up here instead of retelling it inline.

## 2026-09-10 — Folder scanner built; and a correction to how prereleases get cut

Built `scanner.py` against the matching strategy the user picked when asked
(ROADMAP.md's last open decision): an AppID tag on the folder name or a
`.steam-appid` sidecar file wins when present (exact, auto-applied); with
neither, `rapidfuzz` scores the folder name against every active request's
title, and anything above the confidence threshold is surfaced at
`/admin/scanner` as "possible match?" - never auto-resolved. Confirming a
suggestion renames the folder to embed the AppID (so every future scan
matches it exactly, step one, and never asks about that folder again),
records it installed, and marks the request done. Verified live: a real
temp folder with an exact-tagged subfolder and a fuzzy-only one, scanned for
real, the fuzzy suggestion confirmed through the real admin UI, the folder
actually renamed on disk, the request actually marked done, and the
"installed" badge actually showing on a subsequent real search.

This closes out all three of the open decisions ROADMAP.md started with
(folder matching here; visitor identity and deployment mode in the first
session) - see that file's now-trimmed "Open decisions" section and
`CLAUDE.md`'s pointer to this entry.

**Also this stretch: six prereleases (`v1.0.0-rc.4` through `rc.9`) got cut
for what was really one batch of independent feature work** (request
management, DB backup/restore, notifications, self-update), one rc per
branch as each finished, then three more near-duplicates when a shared
one-line port-default fix (5000 → 5001, to stop colliding with
status-portal) rode along on each already-cut branch and got its own fresh
release rather than just a commit. Called out by the user mid-session:
pre-releasing is meant to be a coarser decision than committing - a batch of
finished work, not every individual commit or every branch the moment it
happens to finish. The three fully-superseded releases and tags (`rc.3`,
`rc.4`, `rc.5` - each missing only the port fix its `rc.7`/`rc.8`/`rc.9`
counterpart has) were deleted as cleanup, and `CLAUDE.md`'s release-process
rule now says this explicitly, including that a small shared fix touching
several open branches rides along as a plain commit on each rather than
triggering a release of its own.

## 2026-09-10 — Jellyfin 12.0 disabled legacy authorization; fixed before it ever shipped broken

Jellyfin 12.0 released 2026-09-08 (jumping straight from 10.11.x - there's no
10.12 or 11) and disables `EnableLegacyAuthorization` by default, with a
migration (`DisableLegacyAuthorization`) that flips it off on existing installs
too. That stops `X-Emby-Token`, `X-MediaBrowser-Token`, `X-Emby-Authorization`,
the lowercase `api_key` query param, and the `"Emby"` auth scheme name from being
read at all. Confirmed straight from `AuthorizationContext.cs` in the `v12.0` tag,
not just the release notes - every one of those is gated behind
`_configurationManager.Configuration.EnableLegacyAuthorization` in
`GetAuthorizationInfoFromDictionary`/`GetAuthorizationDictionary`/`GetAuthorization`,
while the plain `Authorization` header with the `MediaBrowser` scheme (and the
`ApiKey` query param) is read unconditionally, first, same as every version back
to 10.6.

This surfaced from another agent's work on status-portal, relayed secondhand -
worth independently verifying rather than trusting, given it's a security-adjacent
claim about a real breaking change, so it was checked directly against
`jellyfin/jellyfin`'s GitHub releases and source (`gh api`) rather than taken on
faith. It held up exactly as described.

`jellyfin_auth.py`'s sign-in check itself (`POST /Users/AuthenticateByName`) was
never affected - that endpoint doesn't require a prior token, so it doesn't touch
the legacy-gated code paths. The one call that did was `_revoke_token()`'s
best-effort cleanup of the short-lived token after a successful sign-in, which
sent `X-Emby-Token` - against a 12.0 server that would have silently stopped
working (a 401, swallowed by the function's own best-effort design, so it would
never have surfaced as an error, just an ever-growing stale device list in
Jellyfin's own UI). Fixed by moving the token into a `Token="..."` field on the
`Authorization` header instead, alongside the existing Client/Device/DeviceId/
Version fields - the same header used for the pre-token client-identification
call, just now optionally carrying a token too.

Verified two ways: `tests/test_jellyfin_auth.py` asserts the revoke call's
headers directly (no `X-Emby-Token`, an `Authorization` header carrying
`Token="..."`), and against a small stand-in server built to actually enforce
12.0's rule - it 401s the old `X-Emby-Token`-only pattern and accepts the new
one, proving both that the fix works and that the *previous* code would have
silently failed against a real 12.0 server.

## 2026-09-10 — First build: Flask skeleton, Steam search, Jellyfin-backed requests, admin panel

The first session against the spec in `CLAUDE.md`. Scope was deliberately capped
to everything except the folder scanner (`scanner.py`), which waits on the
folder→game matching decision in `ROADMAP.md`.

Two of the three open decisions from `ROADMAP.md` got settled before writing any
code that depended on them: visitor identity is Jellyfin-backed sign-in (not
anonymous free text), and both native Python and Docker are supported from day
one. `jellyfin_auth.py` here is intentionally a smaller cousin of status-portal's
module of the same name - a live credential check only, no cached user list, no
sync task, no offline sign-in mode - see the "Ideas" section in `ROADMAP.md` for
what that leaves on the table.

Two real bugs turned up during verification, not just in tests written against
the code as built:

- **Steam's `short_description` comes back with un-decoded HTML entities**
  (`&quot;Perpetual Testing Initiative&quot;` as literal text, not a real quote
  character) - Jinja's autoescaping then re-escaped the literal `&`, so the page
  showed `&quot;` verbatim instead of a quote mark. Only visible by actually
  looking at a rendered page, not from curl output or a unit test asserting on
  the raw string. Fixed in `steam.py` with `html.unescape()` before the text ever
  reaches a template.
- **`steam.search()` sliced storesearch's `items` to the result limit before
  filtering out non-`"app"` entries** (bundles, etc.), so a page of results could
  come back short even when enough real games were present further down the same
  response. Caught by a live search, not by the original unit tests, which
  happened not to mix in a non-app type. Fixed by filtering first, then slicing;
  a regression test (`test_search_filters_before_slicing_to_the_limit`) covers it
  now.

**What's verified, and how:**
- `pytest tests/` - 36 tests: `db.py` CRUD, `steam.py` against mocked HTTP
  responses (including both bugs above), `jellyfin_auth.py` against mocked
  Jellyfin responses (success/invalid/disabled/unreachable, and that the access
  token never appears in the result), and `app.py`'s routes via Flask's test
  client (CSRF enforcement, admin first-run + login + lockout-adjacent paths,
  visitor login gate, duplicate-request prevention, status updates).
- A live dev server (`python app.py`) smoke-tested with real `curl` cookie-jar
  requests: real search results back from Steam's actual API, the real
  first-run→admin-login→CSRF flow, and the visitor-login route correctly 404ing
  with no `PORTAL_JELLYFIN_URL` set.
- A full Playwright browser pass against that live server: dark/light theme
  toggle (and that it persists across reload), real Steam search rendering with
  icons and descriptions, and - against a small local Flask stand-in for
  Jellyfin's `/Users/AuthenticateByName` - the complete golden path end to end:
  sign in, search, request, duplicate-request rejection, the request showing up
  in `/admin/requests` attributed to the right visitor, an admin status
  update/note persisting, and the new status reflecting back on the public
  search page.
- **Not verified:** sign-in against a real Jellyfin server (none was available in
  this environment - the mocked/stand-in coverage above is what exists instead),
  and anything folder-scanner-related, since that code doesn't exist yet.

Released as `v1.0.0-rc.1` - the "rc" reflects the real-Jellyfin gap above, not any
known defect.
