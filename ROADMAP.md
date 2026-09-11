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

- **Discord/email notifications and Seerr contact sync, delegated to status-portal
  rather than built here.** Considered building these directly in this repo
  (Discord webhook + SMTP email + a Seerr read-only sync, status-portal's own
  `notifications.py`/`user_notify.py`/`integrations.py` pattern), but status-portal
  already owns the hard part - `discord_bot.py` (a real bot with DM capability),
  the per-user `notification_queue` + preference columns, and the Seerr
  jellyfinUserId -> email/Discord-ID resolution (`user_notify.find_seerr_account`/
  `contact_for`). Rather than duplicate that here, the plan is for this app to call
  into it instead. Deliberately deferred rather than built either partially or
  guessed at solo - needs a joint session with both repos' agents live at once to
  agree on the contract, not a spec written from one side alone. Shape agreed so far:
  - **Two API keys, one per direction** - not a single shared secret. This app
    calling status-portal's notify endpoint(s) authenticates with a key
    status-portal issues to it; whatever status-portal ever needs to call back
    into this app (if anything) uses a separate key this app issues in the other
    direction. Neither reuses either app's own admin password or Jellyfin
    credentials - a new, narrow trust boundary between two apps that have never
    talked to each other before, same host-allow-list spirit as `updater.py`'s
    GitHub calls.
  - **Two separate notify endpoints, not one generic enqueue.** A "new request
    came in" admin alert reuses status-portal's *existing* `notifications.notify()`
    fan-out (the same admin Discord webhook + email that already exists there) -
    this app just triggers it, no new admin-side plumbing needed on status-portal's
    end. A "your request's status changed" per-user notification is a genuinely new
    event type on status-portal's side, wired into its existing per-user
    preference-column machinery (`EVENT_CHANNEL_PREFERENCE`,
    `user_preferences.notify_email`/`notify_discord_id`) the way `request_update`
    already works for Seerr requests there - not a bare "send this string to this
    person" endpoint, which would be a much less controlled surface to expose
    across a trust boundary.
  - **This app needs an explicit `jellyfin_user_id` concept on `requests`.**
    `requested_by_id` in `db.py` already *is* the Jellyfin user id in practice
    (it's populated straight from the Jellyfin-authenticated session, since
    Jellyfin is the only visitor identity source this app has) - but the
    cross-repo contract should either name that column as the link explicitly or
    add a dedicated one, rather than have status-portal have to assume
    `requested_by_id` is Jellyfin-shaped by convention alone.
  - Fire-and-forget from this app's side either way (a background thread, never
    blocking the request/admin-update handler that triggers it) - a notification
    failure, including status-portal being unreachable, must never break the
    action that triggered it, same rule both apps already apply internally.
- Jellyfin-backed sign-in is currently a single env var (`PORTAL_JELLYFIN_URL`),
  checked live on every sign-in with no cached user list — deliberately simpler
  than status-portal's `jellyfin_auth.py` (no integrations table, no sync task, no
  offline/degraded sign-in mode, no admin-side revocation). Worth revisiting if
  this app ever needs to gate visitor access per-user, or survive a Jellyfin outage
  gracefully for new sign-ins the way status-portal does.
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
