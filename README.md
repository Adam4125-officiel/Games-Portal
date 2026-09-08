# steam-request-portal

*(working title — rename freely before the first commit)*

A personal portal to search Steam's catalog and request a game get downloaded onto
a home server, sibling project to
[status-portal](https://github.com/Adam4125-officiel/Status-Portal) (same author,
same server, same house style — separate repo).

**Status: not built yet.** This repo currently only holds the initial spec for
Claude Code to build from — see `CLAUDE.md` for architecture/conventions and
`ROADMAP.md` for the open decisions that need answering before or during the first
build session.

## What it does (spec)

- **Search**: a search bar hits Steam's public catalog and shows results (name,
  icon, short description).
- **Request**: a small button next to each result sends a request — this app never
  downloads anything itself, it's purely the communication layer between whoever's
  asking and the admin.
- **Admin panel**: review requests, change their status (pending → approved →
  downloading → done, or rejected), leave a note. Password-protected, same pattern
  as status-portal's `/admin`.
- **Auto-detection**: a background scan of a configured games folder — one
  subfolder per installed game — flags requests that are already present, so they
  don't get asked for twice.

## Running it

Not written yet. Once it exists, this section will mirror status-portal's: native
Python (`pip install -r requirements.txt`, `python app.py` for dev,
`python serve_waitress.py` for production) with Docker as a secondary option.

## Visual style

Same design language as status-portal — see `static/css/style.css` for the shared
tokens (colors, fonts, radii).
