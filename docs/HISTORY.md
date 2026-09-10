# History

The narrative archive: what actually broke, how it presented, and what's been
verified against a real deployment (and when). Rules belong in `CLAUDE.md`; the
stories behind them belong here. When `CLAUDE.md` gains a rule because something
broke, it links to the write-up here instead of retelling it inline.

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
