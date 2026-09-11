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
