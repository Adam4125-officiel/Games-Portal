# games-portal

A personal portal to search Steam's catalog and request a game get downloaded onto
a home server, sibling project to
[status-portal](https://github.com/Adam4125-officiel/Status-Portal) (same author,
same server, same house style — separate repo).

## What it does

- **Search**: a search bar hits Steam's public catalog and shows results (name,
  icon, short description).
- **Request**: a small button next to each result sends a request — this app never
  downloads anything itself, it's purely the communication layer between whoever's
  asking and the admin. Requesting requires signing in with a Jellyfin account (the
  same Jellyfin your media server already runs) so a request is always attributable
  to someone; search itself needs no sign-in.
- **Admin panel**: review requests, change their status (pending → approved →
  downloading → done, or rejected), leave a note, or delete a request outright.
  Password-protected, same pattern as status-portal's `/admin`.
- **My requests**: a signed-in visitor can see their own request history and
  current statuses at `/my-requests` - scoped to their own data only.
- **Auto-detection**: a background scan of a configured games folder — one
  subfolder per installed game — recognizes what's already installed, so it's
  never re-requested. See "Games-folder scanner" below.

## Running it

Native Python:

```
pip install -r requirements.txt
cp .env.example .env   # fill in PORTAL_JELLYFIN_URL to enable visitor sign-in
python app.py           # dev server
python serve_waitress.py   # production (run this one at system startup)
```

Docker:

```
cp .env.example .env
docker compose up -d --build
```

The admin password is set on first visit to `/admin`. Without `PORTAL_JELLYFIN_URL`
set, search still works but sign-in (and therefore requesting) is disabled.

## Jellyfin compatibility

Visitor sign-in works against Jellyfin **10.6 through 12.0** (the newest release
as of this writing). Jellyfin 12.0 disables legacy authorization by default,
which stops it reading the old `X-Emby-Token`/`X-MediaBrowser-Token` headers and
the lowercase `api_key` query parameter - this app has never relied on any of
those. It authenticates over the plain `Authorization: MediaBrowser ...,
Token="..."` header, which every Jellyfin version back to 10.6 has read first,
unconditionally - verified directly against `jellyfin/jellyfin`'s own source at
tag `v12.0` (`AuthorizationContext.cs`), not just its release notes. Proven with
a real local stand-in server that enforces 12.0's rule, not just a mocked one -
see `tests/test_jellyfin_12_compat.py`.

## Games-folder scanner

Configured entirely from **Folder Scanner** in the admin nav - no `.env`
editing or restart needed, same idea as Sonarr/Radarr's library scan. Add one
or more root folders (one subfolder per installed game each; one entry per
disk if your library spans more than one - each is scanned independently, so
one being unplugged never affects the others), a scan interval, and a
fuzzy-match confidence threshold, all editable live. Matching, in order:

1. **A tag already in the folder name.** This app tags a folder as
   `{steamapp-<appid>}` anywhere in its name, e.g. `Half-Life 2 {steamapp-220}`
   - curly braces because they're filesystem-safe on Windows/Linux/macOS, and
   deliberately echoing Sonarr's own real `{tvdb-<id>}` convention. A tagged
   folder is recognized instantly, no network call needed.
2. **A fuzzy match against Steam's catalog** for anything untagged. Never
   auto-accepted - a confident guess (default confidence 82) shows up on the
   Folder Scanner page as "awaiting review" for the admin to confirm or
   correct. Confirming it (whichever way it was found) renames the folder on
   disk to add the tag, so the next scan recognizes it directly instead of
   fuzzy-matching it again.

Once a game is recognized, the search page shows an "installed" badge instead
of a Request button, and a duplicate request for it is refused.

Under Docker, a folder still needs to be bind-mounted into the container
first (see `docker-compose.yml`'s comments, including how to add more than
one for multiple disks) - you then enter its *container-side* path into the
admin UI, same as any other folder.

## Visual style

Same design language as status-portal — see `static/css/style.css` for the shared
tokens (colors, fonts, radii) and the layout/components built on top of them.
