# Roadmap

Open feature ideas and unexplained symptoms only — a shipped idea's write-up gets
deleted down to one index line once it's done (the code and `docs/HISTORY.md`
become the better record at that point). See `CLAUDE.md` for how the code actually
works.

## Ideas (unranked)

- Per-visitor Discord DMs on request status change. Discord IDs are already
  collected from Seerr and cached (`seerr.py`); delivery itself needs a real bot
  (a persistent connection), which is a separate, optional piece of
  infrastructure - status-portal keeps its own the same way. See
  `docs/HISTORY.md` for why this was scoped out of the first notifications pass.
- Jellyfin-backed sign-in is currently a single env var (`PORTAL_JELLYFIN_URL`),
  checked live on every sign-in with no cached user list — deliberately simpler
  than status-portal's `jellyfin_auth.py` (no integrations table, no sync task, no
  offline/degraded sign-in mode, no admin-side revocation). Worth revisiting if
  this app ever needs to gate visitor access per-user, or survive a Jellyfin outage
  gracefully for new sign-ins the way status-portal does.
- Search is a full page reload (`GET /?q=...`), not live/incremental like
  status-portal's search-as-you-type. Simpler and avoids turning every keystroke
  into a Steam API call, but worth revisiting for UX if it feels slow in practice.
