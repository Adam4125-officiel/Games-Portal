# History

The narrative archive: what actually broke, how it presented, and what's been
verified against a real deployment (and when). Rules belong in `CLAUDE.md`; the
stories behind them belong here. When `CLAUDE.md` gains a rule because something
broke, it links to the write-up here instead of retelling it inline.

## 2026-09-10 — Self-update system ported from status-portal; caught a real archive-validation gap and a private-repo blocker along the way

Added `updater.py`/`update.py` - check GitHub for a newer release, download and
verify it, back up and replace this app's own files, roll back on failure, and
a CLI (`update.py apply`/`rollback`/`list-backups`/`channel`) usable over SSH
when the web UI itself is broken. Ported near-verbatim from status-portal's
module of the same name, since the whole point was parity with that project's
existing, hardened design - the security posture (hardcoded repo constant,
HTTPS-only with cert verification, download host allow-list re-checked after
redirects, size/SHA-256 verification, zip-slip and protected-path checks,
atomic file replacement, automatic pre-restart rollback) is unchanged from
there. Scoped down for what this app actually has: no `scheduler.py` yet, so
the periodic check is a plain daemon thread instead of a registered task; no
2FA, so the in-app "Update now" button is gated the same as every other admin
POST here (login + CSRF + a client-side confirm()) instead of status-portal's
step-up TOTP requirement.

**A real gap turned up while porting the archive-path validation, not just
while writing tests against the ported code as-is.** `_archive_members()`
strips a shared top-level directory when every member has one, to handle
GitHub's auto-generated zipball layout - but a *single-entry* archive made
entirely of `"../evil.py"` or `"/etc/passwd"` also technically satisfies
"every member shares one first segment" (`".."` or `""`), so that segment got
treated as a legitimate directory to strip *before* the parent-directory and
absolute-path checks ever ran, defusing the very thing those checks exist to
catch. Traced all the way through: this was not an actual directory-escape
vulnerability, because the final `destination.startswith(APP_ROOT)` check two
guards later is an independent backstop that still confines the write - but it
meant a malformed archive got silently coerced into landing somewhere odd
inside the app root instead of aborting loudly, which is the code's own stated
design goal. Fixed by excluding `""` and `".."` as strip-prefix candidates, and
by checking for a leading `/` before any normalisation could hide it (the
original also ran `os.path.isabs()` *after* an unconditional `.lstrip("/")`,
which meant that check could never actually fire for a POSIX-style absolute
path). Both are `tests/test_updater.py` regression cases now. **This identical
logic exists unchanged in status-portal's own `updater.py`** - worth an
equivalent fix there, flagged to the user rather than touched directly since
that's a different repo.

**The live smoke test found something more fundamental: this repo was
private.** `update.py check` against the real, live GitHub repo 404'd -
unauthenticated calls to a private repo's releases API 404 rather than 403
(GitHub doesn't reveal that a private repo exists to someone without access).
status-portal's identical unauthenticated design works *there* only because
that repo is public; nothing about this being "the same system as
status-portal" made that true here too. Confirmed live: `gh api` (this
session's authenticated token) could see the repo fine, while a plain
`requests.get()` - exactly what `updater.py` uses, deliberately, per its own
"never configurable, no credentials" security posture for the update source -
could not. Put to the user as a real three-way decision (make the repo public
/ add a `PORTAL_GITHUB_TOKEN` and accept a new credential surface / ship it
non-functional for now) rather than guessed at, since the consequences differ
enough that it wasn't this session's call to make alone. Chosen: make the repo
public. The full git history was audited first (`git log --all` for `.env`,
`secret_key`, `instance/`, and password/key/token-shaped strings) and came back
clean, so there was nothing to worry about being exposed - the actual
visibility flip needs a token with repo-admin rights this session's own
`GITHUB_TOKEN` doesn't have, so it's still pending the user's own action as of
this write-up. Once it's flipped, the update-check/download path still needs a
real live run against actual GitHub releases to be confirmed end-to-end -
everything up to that point has only been verified against mocked HTTP
responses and a real filesystem in `tmp_path`.

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

## 2026-09-10 — One feature batch, five branches, five separate prereleases (rule fixed twice, only stuck the second time)

The user handed over one batch of five independent features to build in a single
session: self-update system, request management, notifications/Seerr sync, DB
backup/restore, and the folder scanner. Each landed on its own branch/PR (correct -
that's the Branching rule), but the release step read "cut a new `-rc.N` every time
a self-contained chunk of current work finishes" and took each *branch* finishing
as that trigger. Result: `v1.0.0-rc.6` through `v1.0.0-rc.10`, one GitHub
prerelease per branch, none of them representing more than a fifth of what the user
actually asked for - exactly the "prerelease at every single little feature" the
user then had to call out and ask to have fixed.

It was already caught once, mid-batch: a commit on the `folder-scanner` branch
(`9f59981`) rewrote `CLAUDE.md`'s cadence rule to say batches, not branches. Then
the very next thing that branch did was finish the scanner and cut `v1.0.0-rc.10` -
one more per-branch release, under the rule that had just been rewritten to forbid
it. Rewriting a rule mid-batch didn't help because "per-branch" was still true of
the branch already in progress; the fix only guarded *future* batches, not the rest
of the one it landed in the middle of.

`CLAUDE.md`'s release-process section now says explicitly that a rule change like
this governs the rest of the batch it lands in, not just batches started after it,
and spells out that "batch" means the whole handoff, not the branch. The five
existing rc.6-rc.10 releases and their branches were left as-is at the user's
call - they still work as individual test builds - so this entry is the fix,
not a cleanup log.

## 2026-09-11 — Admin tools, "my requests," a games-folder scanner, DB backup/restore, and a session-security pass — the Discord/email/Seerr half deferred cross-repo

A nine-item batch handed over in one session. Two items (Discord/email
notifications, Seerr contact sync) were deliberately **not** built here after
discussion mid-session: status-portal already owns the hard parts of "notify
one specific person" (a real Discord bot with DM capability, the Seerr
jellyfinUserId -> email/Discord-ID resolution), and duplicating that here
would mean two divergent implementations of the same idea instead of one app
delegating to the other. The agreed shape (two API keys, one per direction;
two separate notify endpoints rather than a generic enqueue; an explicit
`jellyfin_user_id` concept on `requests`) is captured in `ROADMAP.md`'s Ideas
section, waiting on a joint session with both repos' agents live at once
rather than a spec written from one side alone. A third item ("my requests"
showing other Jellyfin account settings - join date, library access) was also
narrowed mid-session: Jellyfin stays login/logout only in this app, nothing
more, so the shipped page is a pure view over this app's own `requests` table.

What shipped: admins can delete a request outright (not just change its
status); visitors get a `/my-requests` page scoped strictly to their own
data; a new games-folder scanner (`scanner.py`) recognizes installed games
via an embedded `{steamapp-<id>}` tag or an admin-confirmed fuzzy match
against Steam's own search backend, and the search/request flow now respects
it (an "installed" badge instead of a Request button, duplicate requests for
an installed game refused); admins can download/restore a database backup;
admin and visitor sessions now expire after a configurable idle period
instead of lasting the full 30-day cookie lifetime regardless of use; and the
Jellyfin 12.0 fix from the previous entry was independently re-verified
straight from `jellyfin/jellyfin`'s own `v12.0` source (not just its release
notes) and proven against a real local server that actually enforces the
disabled-legacy-auth rule, not just a mocked one.

**The "random disconnects" bug report got an actual investigation, not an
assumed cause.** Checked and ruled out: multi-process secret-key mismatch
(this app runs one process, many threads, sharing module state - status-
portal's documented version of this bug doesn't apply here), flash-message
cookie bloat (every template that can show one calls
`get_flashed_messages()`), CSRF token regeneration on login (session is
never cleared at sign-in). Found a real, previously-invisible gap instead:
`config._load_or_create_secret_key()` silently swallowed a failure to
persist a freshly-generated key. If `instance/` were ever unwritable (a
Docker volume permission mismatch, say), every future process start would
silently generate a new ephemeral key and invalidate every session - which
from the outside looks exactly like unexplained disconnects, especially
since a crash-and-restart isn't something a user would necessarily connect
to "why was I logged out." Now logs loudly instead of failing silently.

**Two real bugs turned up during verification, not just in tests written
against the code as built** - the same pattern the first-build entry above
already established for this project:

- **rapidfuzz's `fuzz.WRatio` is case-sensitive by default.** Hand-testing
  the scanner's fuzzy matching against realistic folder names found "Elden
  Ring" vs. Steam's own "ELDEN RING" scoring 30/100 - well under any sane
  confidence threshold - because nothing was normalizing case before
  scoring. The mocked unit tests never caught this; they happened not to
  include a case difference. Fixed by passing `rapidfuzz.utils.default_process`
  as the scoring processor; confirmed the new regression test
  (`test_fuzzy_matching_ignores_case_differences`) actually catches it by
  reverting the fix locally and watching the test fail before restoring it.
- **The database restore feature's safety-snapshot directory was anchored to
  a fixed path, not to `db.DB_PATH`.** `_db_safety_backup_dir()` (originally
  a `DB_SAFETY_BACKUP_DIR` constant built from `config.APP_ROOT`) never
  followed the test suite's `isolated_db` fixture, which only monkeypatches
  `db.DB_PATH` - so every restore test was silently writing real timestamped
  `.db` snapshots into this actual repo's `instance/db_backups/` directory
  instead of staying inside the test's own `tmp_path` sandbox. Caught by
  setting up a live smoke test in an isolated copy of the app and noticing
  stray files show up in `git status` for the real checkout, not by anything
  in the mocked suite. Harmless in that `instance/` is gitignored, but real
  filesystem pollution outside test isolation nonetheless, and the identical
  class of bug would leak real snapshots in a production deployment where
  `db.DB_PATH` is ever relocated. Fixed by deriving the directory from
  `db.DB_PATH` at call time instead.

**What's verified, and how:** `pytest tests/` - 158 tests total, run after
every individual fix, covering every item above (including a real local
Flask stand-in server for the Jellyfin 12.0 proof, and full `tmp_path`
folder-tree tests for the scanner's tag/fuzzy-match/prune/rename behavior).
A full live smoke test against a real running dev server in an isolated copy
of the app (so nothing touched this actual checkout's own `instance/`):
admin first-run login, session-timeout settings save, the scanner's "Scan
now" and "Confirm match" against real Steam search results (including the
refusal path for a bogus AppID and the successful on-disk folder rename),
the search page's "installed" badge and hidden Request button, a database
backup download opened and verified as a real SQLite file, and the full
visitor path (sign-in against a small local Jellyfin-shaped stand-in,
search, request, "my requests" showing only that visitor's own request,
admin seeing and deleting it). Two real curl gotchas from this file's own
testing guidance were hit and corrected mid-session, not just cited in the
abstract: a `-L`-followed POST redirect that re-sent a stale CSRF token
looked exactly like a 400 failure, and a `-b`-without-`-c` cookie jar meant
a flash message written server-side never made it back to the client.

**Not verified:** a real Jellyfin 12.0 server (still none available in this
environment - same gap the very first release shipped with), a real Docker
Compose run with a real bind-mounted games folder, and a restore against a
large, production-sized database (only small test databases were exercised).

Released as `v1.1.0-rc.1`.

## 2026-09-11 — v1.1.0-rc.1 through rc.5 confirmed stable on a real deployment; merged

The batch above went through five rc's of real usage, not just the mocked
suite, each fixing something only a live install surfaced: a schema crash on
an install carrying an earlier scanner attempt's table shape (rc.2), waitress
inheriting the dev server's chatty logging (rc.3 - production logs were as
noisy as a dev session by default), a genuine investigation into reported
"random disconnects" that couldn't be reproduced here but got a targeted
hardening fix anyway (rc.3), the admin nav visually colliding with the
sign-in bar on mobile (rc.3), and then two rounds of real feature feedback
once the scanner was actually tried for the first time: moving its config
out of `.env` into the admin UI with real multi-folder support (rc.4), and a
public Collections page plus per-folder client-facing paths (rc.5).

Confirmed stable from real end-to-end testing against a real deployment -
the bar this repo's branching rule actually requires, not just passing
tests. Merged to `main` with a regular merge commit (`dda59a6`), branch
deleted both sides. Released as `v1.1.0`.

## 2026-09-11 — Deleted-game tracking, blacklist, request limits, tag-aware badges, and Jellyfin-style rails

One batch, several independent asks handed over together: deleted games are
now tracked instead of silently vanishing, an admin-maintained blacklist
blocks specific AppIDs from ever being requested, an optional global/per-user
request-rate limit (mirroring Seerr/Jellyseerr's shape) caps how many games a
visitor can request per day/week/month, the "available" badge names the
matched disk's label when one is set, the "Recently added" count is now
admin-configurable, scroll position survives a refresh or a form Save, the
visitor sign-in bar no longer renders under `/admin`, and both "Recently
added" and `/collections` became horizontally-scrolling rails - the latter
grouped into Jellyfin-style rows by Steam genre, newly cached per matched
game (`installed_games.genres`).

**Real hand-testing against a live dev server - a real scanned games folder
with real Steam AppIDs, a real folder deletion, real Steam blacklist/genre
data - turned up three bugs a template-only read-through and the mocked test
suite both missed:**

- `index.html`'s "Recently added" rail never actually included
  `rail_scroll.js` - `collections.html` had the script tag, `index.html`
  simply didn't. The arrow buttons rendered and looked normal but did
  nothing at all when clicked, and (since the visibility-toggle logic never
  ran either) never hid themselves on a row short enough not to need them.
  Only visible by actually clicking the arrow in a real browser and checking
  whether the rail scrolled - a static render diff would never have caught a
  missing `<script>` tag whose absence changes no visible markup at all.
- The rail's first cut reused `.result-card`'s icon-left/text-right layout
  at rail width (~280px) - titles wrapped mid-word, descriptions truncated to
  a handful of characters. Redesigned as a poster-style `.rail-card` (icon on
  top at Steam's own header-image aspect ratio, text below) once real Steam
  artwork in a real screenshot made the squeeze obvious in a way reading the
  CSS never would have.
- `scroll_restore.js`'s own sessionStorage-based restore was silently getting
  overridden by the browser's *own* native scroll restoration firing after
  it on a plain reload - passed on a Save-and-redirect (a fresh navigation,
  which the browser doesn't auto-restore) but failed the more basic F5 case
  that was the entire point of the feature. Root-caused by measuring
  `window.scrollY` before/after a real Playwright reload rather than trusting
  the code path in isolation; fixed with `history.scrollRestoration =
  "manual"` so the script is the sole authority either way.

**What's verified, and how:** `pytest tests/` - 234 tests, covering every
item above including deleted-row transitions (and reappearance back to
matched), genre-caching and its one-time backfill for rows matched before
the column existed, blacklist enforcement and admin CRUD, and request-limit
enforcement (global, per-user override, rejected requests excluded from the
count). A full live smoke test in an isolated copy of the app, against real
Steam data: a real games folder scanned with real tagged subfolders (Half-
Life 2, Portal 2, Stardew Valley and others), a real server-side folder
deletion confirmed to flip that row to "deleted" and immediately show a
"Sign in to request" button again on search instead of "Available", a real
AppID (Elden Ring) blacklisted through the admin UI and confirmed refused
with its reason shown, a real global weekly limit and a real per-user
monthly override both saved and reflected in the admin UI, and Playwright
screenshots at 1280px and 375px in both themes for the search page, admin
scanner/blacklist/limits pages, and collections' genre rows.

**Not verified:** a real Jellyfin server (still none available in this
environment), a real Docker Compose run, and a production-sized database
under any of this batch's new tables.

Released as `v1.2.0-rc.1`.

### rc.2 — real-user feedback on rc.1: every game links to Steam, and request-limit overrides list Jellyfin's own users

Two follow-ups from trying rc.1 for real, still the same batch/PR:

- Every game card (search results, Recently added, Collections) now links
  out to its own Steam store page - `admin_requests.html`/`my_requests.html`
  already had the equivalent for request rows; this filled in the three
  places that didn't.
- `/admin/limits`' per-user override list previously only showed visitors
  who had already made a request, explicitly because this app has no
  Jellyfin user directory sync (see `ROADMAP.md`). Since the actual need was
  narrower - just listing candidates for an override, not a real directory -
  it now also lists every account from Jellyfin's own unauthenticated `GET
  /Users/Public` (the same list its login screen shows), live and uncached,
  merged with past requesters as a fallback for an account since deleted or
  hidden. **Verified against Jellyfin's own source before writing any code**
  (`Jellyfin.Api/Controllers/UserController.cs` at tags `v10.7.0` and
  `v12.0`, straight from GitHub): `GetPublicUsers()` has no `[Authorize]`
  attribute in either, confirming this was never affected by 12.0's
  legacy-auth-header change - there was no auth header on this endpoint to
  begin with, a different situation from the sign-in flow's own 12.0 fix.
  Also proven against a real running stand-in server, not just a mocked
  response (`tests/test_jellyfin_12_compat.py`'s new
  `test_list_public_users_against_a_real_running_server`).

`pytest tests/` - 249 tests. Released as `v1.2.0-rc.2`.
