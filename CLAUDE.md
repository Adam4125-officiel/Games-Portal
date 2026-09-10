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

## Open decisions — see `ROADMAP.md`

This spec has a few things intentionally left open (folder→game matching strategy,
visitor identity, deployment mode). Don't silently pick one while building — they're
listed in `ROADMAP.md` with the tradeoffs; confirm with the user first.

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
   prerelease on GitHub too. **Cut a new `-rc.N` every time a self-contained
   chunk of current work finishes without that confirmation yet** — don't wait
   for the branch to merge first. The tag targets the *branch's* tip commit, not
   `main`, since the branch is still open at that point; the zip lets the user
   pull down and try that exact state without anything touching `main`. A stable
   (non-`-rc`) release only ever gets cut from `main`, after the merge the
   Branching rule above describes.
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
    scanner.py               # games-folder scan + matching — not built yet, waits on ROADMAP.md
    requirements.txt / requirements-dev.txt
    .env.example
    Dockerfile / docker-compose.yml / .dockerignore
    static/css/style.css    # design tokens + layout, carried over from status-portal
    static/js/theme.js      # light/dark toggle
    templates/
    tests/                   # pytest — db.py, steam.py, jellyfin_auth.py, app.py routes
    instance/                # portal.db + secret_key, created automatically — gitignored
