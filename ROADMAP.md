# Roadmap

Open feature ideas and unexplained symptoms only — a shipped idea's write-up gets
deleted down to one index line once it's done (the code and `docs/HISTORY.md`
become the better record at that point). See `CLAUDE.md` for how the code actually
works.

## Pending user action

- **Make this repo public on GitHub.** Decided (not still open) — the self-update
  system (`updater.py`/`update.py`) needs it, since it calls GitHub's releases API
  unauthenticated, which 404s against a private repo (see `docs/HISTORY.md`,
  2026-09-10). This session's own token can't flip repo visibility; needs doing by
  hand at Settings → General → Danger Zone → Change repository visibility. Once
  done, the update-check/download path still needs one real live run against
  actual GitHub releases to confirm the whole thing end-to-end — delete this
  line once both are done.

## Ideas (unranked)

- Discord/email notifications, delegated to status-portal instead of built
  here - shipped and confirmed via a real live joint-session test. See
  `CLAUDE.md`'s "status-portal integration" section and `docs/HISTORY.md`'s
  2026-09-12 entry.
- **Seerr contact sync, delegated to status-portal rather than built here.**
  Not part of the notification-delegation batch above - still open.
  status-portal already owns the jellyfinUserId -> email/Discord-ID
  resolution (`user_notify.find_seerr_account`/`contact_for`); this app would
  just need to call into it, same shape as the notification delegation
  already shipped (a joint session to agree the contract, not built or
  guessed at solo).
- Jellyfin-backed sign-in is currently a single env var (`PORTAL_JELLYFIN_URL`),
  checked live on every sign-in with no cached user list — deliberately simpler
  than status-portal's `jellyfin_auth.py` (no integrations table, no sync task, no
  offline/degraded sign-in mode, no admin-side revocation). `/admin/limits` now
  makes one live, uncached call to Jellyfin's own `Users/Public` (unauthenticated,
  same list its login screen shows) to populate the per-user override list — that's
  still not a directory sync (no caching, no admin-side revocation, nothing
  persisted), just one on-demand read for one admin page. Worth revisiting for real
  if this app ever needs more than that - gating visitor access per-user, or
  surviving a Jellyfin outage gracefully for new sign-ins the way status-portal does.
- Search is a full page reload (`GET /?q=...`), not live/incremental like
  status-portal's search-as-you-type. Simpler and avoids turning every keystroke
  into a Steam API call, but worth revisiting for UX if it feels slow in practice.
- Free disk space shown per configured games folder on `/admin/scanner` (a cheap
  `shutil.disk_usage` call) - helps judge whether to approve a request before it
  fills the drive.
- A pending-request count badge on the admin nav ("Requests · 3") so it's visible
  without opening the page.
- A "surprise me" button on `/collections` - picks something random from what's
  already installed.
