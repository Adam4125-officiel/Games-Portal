# Notes for future Claude Code sessions on this repo

This file is for context that survives between sessions — architecture decisions,
gotchas, and standing workflows that aren't obvious from reading the code cold. Keep
it updated as the project evolves; don't let it go stale.

This project is brand new. Most of the sections below are inherited from
[status-portal](https://github.com/Adam4125-officiel/Status-Portal) — same author,
same home server, same workflow — adapted for this project. There's no accumulated
war-story history yet; that grows the same way it did there: a rule gets added here
the first time it's actually broken, with the story in `docs/HISTORY.md`.

## What this is

A personal **Steam game request portal**, sibling project to status-portal (same
server, same admin, separate repo). It is **not** a downloader — no torrenting, no
actual file transfer of any kind lives in this app. It's purely a
communication/tracking layer:

1. A visitor searches Steam's catalog (name, icon, short description) and hits a
   small **Request** button next to a result.
2. That creates a request row the admin reviews from `/admin` — approve/reject, set
   a status (pending → approved → downloading → done, or rejected), leave a note.
3. A background scan of a configured games folder — one subfolder per installed
   game — auto-detects what's already present (same idea as Sonarr/Radarr's library
   scan), so an already-installed game doesn't get re-requested.

Single admin, password-protected `/admin`. No download logic of any kind belongs in
this app, ever — if a feature request starts to look like "and then it fetches the
file", that belongs in a different project.

- Backend: **Python / Flask** — `app.py` for dev, `serve_waitress.py` (waitress, a
  real WSGI server) for production. Same split as status-portal, same reason: the
  Flask dev server isn't meant to run 24/7.
- Storage: **SQLite**, single file under `instance/`, created automatically.
- Steam data: the public, unauthenticated `store.steampowered.com/api/storesearch`
  endpoint — no API key needed, no scraping.
- Self-update (`updater.py`/`update.py`, ported from status-portal): checks/downloads
  from this repo's own GitHub Releases API, unauthenticated - same as status-portal,
  which works there because that repo is public. **This repo must stay public for
  that to keep working** - going private again would 404 every check/download with
  no code change needed to reproduce it (see docs/HISTORY.md, 2026-09-10).

## Settled decisions from the original spec

The original spec deliberately left three things open: folder→game matching
strategy, visitor identity, deployment mode. All three are now settled and
built - visitor identity is Jellyfin-backed sign-in (login/logout only, no
further account data - see `jellyfin_auth.py`'s module docstring), both
native Python and Docker are supported, and the folder scanner
(`scanner.py`) tags a folder with its Steam AppID when known and falls back
to admin-confirmed fuzzy matching otherwise - see README's "Games-folder
scanner" section for the tag format. Don't relitigate these; if a genuinely
new open decision comes up while building, it goes in `ROADMAP.md` the same
way, confirmed with the user first rather than picked silently.

## Visual identity — same as status-portal, don't design a new one

status-portal's look ("network control room / monitoring rack") carries over as-is.
`static/css/style.css` in this repo starts from status-portal's actual design
tokens (colors, fonts, radii) — see that file, don't invent new ones. If a new
component needs something the tokens don't cover, add a token, don't hardcode a
value inline.

## Workflow conventions inherited from status-portal (same author — keep these)

These aren't up for relitigating; they're standing rules across this author's
projects, not something specific to status-portal's own business logic.

**Config split.** Secrets and deploy-shaped config (ports, intervals, URLs) are
environment variables read in one `config.py` — nothing else reads `os.environ`
directly. Routine admin-tunable toggles are DB-backed settings, edited live from
`/admin`, no restart needed. A new env var means updating three places, not one:
`config.py`, `.env.example`, and `docker-compose.yml`'s `environment:` block if
Docker is supported — compose only substitutes `.env` into itself, it doesn't
inject it into the container, so anything missing there is silently ignored under
Docker no matter how correctly the user set it.

**No slow I/O in a request handler.** Both the Steam search call and the
games-folder scan are exactly what this rule is about. Standard pattern: a
background thread does the polling/scanning and writes to a module-level cache;
request handlers only ever read the cache. The one sanctioned exception is an
explicit one-shot admin action the user knows will be slow (a "Scan now" button) —
never something that fires on every page load.

**No ORM, no migration framework**, if SQLite stays hand-rolled like status-portal's
`db.py`. A schema change to a table that already has real data needs an
`_ensure_column()`-style call in `init_db()` — `CREATE TABLE IF NOT EXISTS` is a
silent no-op on a table that already exists.

**Branching: always a branch + PR, never straight to `main`.** One branch/PR per
piece of work, not a branch per fix — it stays open and keeps collecting commits
as work continues, including fixes found after it looked done, until the user
explicitly confirms it's stable from **real end-to-end testing against a real
instance** (a real Jellyfin server, a real deployment). Passing `pytest`/Playwright/
mocked-stand-in verification is necessary before asking for that confirmation, but
it is not what satisfies it — those catch what they catch, and this project has
already had a case (the Jellyfin 12.0 legacy-auth fix) where mocked coverage alone
would not have proven the fix worked against a real server. Only once the user
confirms: merge with a regular merge commit (`gh pr merge N --merge`, never
squash/rebase, so the individual per-fix commits survive on `main` for `git
bisect`/`git revert`), then delete the branch. Doc-only edits (`CLAUDE.md`,
`ROADMAP.md`, `docs/HISTORY.md`) can go straight to `main` if no branch is
currently open; if one is open, they ride along on it instead.

**Commit cadence: one commit per completed fix, not one per session.** "Complete"
means it works and its tests pass — don't batch a whole session into one commit,
and don't hold everything back for one tidy final commit.

**Release process**, once there's a first stable line to cut:
1. Bump `VERSION` (tracked file at repo root, no leading `v`) first — the single
   source of truth anything comparing versions reads.
2. `vMAJOR.MINOR.PATCH`, with a `-rc.N` suffix for anything not yet confirmed
   stable from real end-to-end testing (see Branching above) — mark it a
   prerelease on GitHub too. Pre-releasing is a decision made **per batch of
   handed-over work, not per branch and not per feature.** When the user hands
   over several independent features/fixes to build in one go, that whole
   handoff is one batch — finish it first, then cut `-rc.N` (one for each
   branch the batch touched, in one pass) once, not as each branch happens to
   finish first. A batch of one (a single fix or feature worked in isolation)
   still gets its own `-rc.N` when it's done; the rule only forbids
   fragmenting one batch into several releases (see `docs/HISTORY.md`'s
   2026-09-10 rc.6–rc.10 entry for what that looked like and why it happened
   twice in the same session even after being caught once). If a cadence rule
   change like this one lands mid-batch, it governs the *rest of that same
   batch* too — don't keep releasing under the old cadence and only apply the
   fix starting next time. When it's genuinely unclear whether something is
   its own batch or part of one already in flight, ask the user rather than
   defaulting to cutting a release. The tag targets the *branch's* tip commit,
   not `main`, since the branch is still open at that point; the zip lets the
   user pull down and try that exact state without anything touching `main`.
   A stable (non-`-rc`) release only ever gets cut from `main`, after the
   merge the Branching rule above describes.
3. Changelog from `git log <previous-tag>..HEAD --oneline`, grouped informally into
   Added / Fixed / Changed — written for a person, not a machine.
4. `git archive --format=zip -o <name>-vX.Y.Z.zip HEAD` for the release asset —
   tracked files only, already clean.
5. `git tag vX.Y.Z && git push origin vX.Y.Z && gh release create vX.Y.Z <zip> --title "vX.Y.Z" --notes "<changelog>" [--prerelease]`.
   If tag-push or `gh` access isn't available (a cloud session without full git
   credentials): build the zip anyway, push a branch, and hand the user everything
   needed to finish the release by hand — tag name, target commit, title, notes,
   the zip.

**Ending a session** (only when the user explicitly says the session/work is
done): update `CLAUDE.md` / `docs/HISTORY.md`, trim `ROADMAP.md` for anything
shipped, cut a final `-rc.N` for whatever hasn't been confirmed stable from real
end-to-end testing (branch stays open, nothing merges without that confirmation —
see Branching above), then delete every already-merged/stale branch, remote and
local. A session ending mid-branch is not itself the confirmation.

**Testing/verification habits.** A fresh sandbox/codespace starts with nothing
beyond a bare Python install — no project deps, no Playwright/Chromium. Standing
authorization to install whatever's needed without asking, same as status-portal:
`pip install -r requirements.txt` once it exists, and for anything JS-dependent
(the Steam search box almost certainly qualifies), `pip install playwright &&
python -m playwright install --with-deps chromium` (~1 minute). Run the full test
suite *and* a live smoke test (`curl`, or the real browser via Playwright for
anything JS-dependent) before calling something done. Say plainly
what was and wasn't actually verified rather than implying full coverage from unit
tests alone. If a login/session flow is involved: a curl cookie jar needs both
`-b` (send) and `-c` (save) on every request, and never combine `curl -X POST`
with `-L` against a CSRF-protected route — the redirect gets re-POSTed with a
stale token and looks exactly like a failure.

**Any UI change gets a real Playwright look, at both form factors.** Not
optional, not just for JS-dependent flows — any change touching a template or
`static/css/style.css` gets screenshotted with Playwright before calling it
done, at a desktop width (e.g. 1280px) *and* a phone width (e.g. 375px). This
app's admin panel in particular has had real, user-reported mobile layout
problems that CSS-reading-only review missed. Screenshot both themes if the
change touches anything color-related.

**Two companion files**, same split as status-portal:
- `ROADMAP.md` — open ideas and unexplained symptoms only. A shipped idea's
  write-up gets deleted down to one index line once it's done — the code and
  `docs/HISTORY.md` become the better record at that point.
- `docs/HISTORY.md` — the narrative archive: what actually broke, how it
  presented, what's been verified against a real deployment and when. Rules live
  in this file; the stories behind them live there.

## Project structure (grows as the app does)

    CLAUDE.md
    README.md
    ROADMAP.md
    VERSION
    docs/HISTORY.md
    app.py                  # Flask routes (public + admin) — dev server
    serve_waitress.py       # production WSGI entrypoint
    config.py               # env-var config
    db.py                   # SQLite layer
    steam.py                # storesearch + appdetails client
    jellyfin_auth.py        # visitor identity — live Jellyfin credential check
    updater.py               # self-update: check/download/verify/backup/replace/rollback
    update.py                # CLI wrapper around updater.py, usable when the web UI is broken
    scanner.py               # games-folder scan + tag/fuzzy matching, installed_games table
    requirements.txt / requirements-dev.txt
    .env.example
    Dockerfile / docker-compose.yml / .dockerignore
    static/css/style.css    # design tokens + layout, carried over from status-portal
    static/js/theme.js      # light/dark toggle
    static/js/admin_confirm.js  # confirm() guard on any button[data-confirm]
    static/js/scroll_restore.js # restores scroll position after a refresh/redirect
    static/js/rail_scroll.js    # arrow-scroll for horizontal rails (Recently added, Collections)
    templates/                  # admin_base.html holds the shared admin nav shell
    tests/                   # pytest — db.py, steam.py, jellyfin_auth.py, updater.py, scanner.py, app.py routes
    instance/                # portal.db + secret_key + update_backups/ + db_backups/, gitignored
