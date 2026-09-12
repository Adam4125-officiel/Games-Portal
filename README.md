# games-portal

A personal portal to search Steam's catalog and request a game get downloaded onto
a home server, sibling project to
[status-portal](https://github.com/Adam4125-officiel/Status-Portal) (same author,
same server, same house style — separate repo). It's purely a communication/
tracking layer - no download logic of any kind lives in this app.

- **Search** Steam's public catalog, no sign-in needed. **Request** a game with one
  click once signed in with your Jellyfin account.
- An **admin panel** tracks each request through to done, with a **games-folder
  scanner** that auto-detects what's already installed so it's never re-requested.
- A **blacklist** and optional **per-visitor request limits** keep things in check.
- Integrates with [status-portal](https://github.com/Adam4125-officiel/Status-Portal)
  for health monitoring, a home-page link, and notification delivery.

See the **[wiki](https://github.com/Adam4125-officiel/Games-Portal/wiki)** for
installation, configuration, a full admin-panel guide with screenshots, security
notes, and everything else.

## Quick start

Native Python:

```
pip install -r requirements.txt
cp .env.example .env   # fill in PORTAL_JELLYFIN_URL to enable visitor sign-in
python app.py               # dev server
python serve_waitress.py    # production (run this one at system startup)
```

Docker:

```
cp .env.example .env
docker compose up -d --build
```

The admin password is set on first visit to `/admin`. Without `PORTAL_JELLYFIN_URL`
set, search still works but sign-in (and therefore requesting) is disabled. See the
wiki's **[Installation and Setup](https://github.com/Adam4125-officiel/Games-Portal/wiki/Installation-and-Setup)**
page for the rest - session durations, Jellyfin compatibility, and publishing
beyond your LAN.

## Visual style

Same design language as status-portal — see `static/css/style.css` for the shared
tokens (colors, fonts, radii) and the layout/components built on top of them.
