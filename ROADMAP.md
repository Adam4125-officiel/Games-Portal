# Roadmap

Open feature ideas and unexplained symptoms only — a shipped idea's write-up gets
deleted down to one index line once it's done (the code and `docs/HISTORY.md`
become the better record at that point). See `CLAUDE.md` for how the code actually
works.

## Open decisions (confirm before/while building — don't guess silently)

- **Folder → game matching strategy.** Matching purely on the folder name string
  (a naive Sonarr-style scan) is fragile — a typo, punctuation, or a
  differently-formatted name (`Half-Life 2` vs `half_life_2_2004`) misses a real
  match. Two sturdier options:
  - (a) embed the Steam AppID in the folder name or a small sidecar file the admin
    drops in once per game, exact-match on that — most reliable, small one-time
    admin cost per game.
  - (b) fuzzy-match the name (e.g. `rapidfuzz`) with a confidence threshold;
    anything below it surfaces to the admin as "possible match?" instead of
    auto-resolving.

  Don't ship pure exact-string matching as the only mode — pick (a), (b), or both.
  Waits for the folder scanner build session — see `scanner.py` (not written yet).

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

- Show a small "already installed" badge directly on a search result if the
  scanner has already matched it, instead of only surfacing that inside a request.
- Discord notification on new request / status change, mirroring status-portal's
  optional webhook pattern.
- Jellyfin-backed sign-in is currently a single env var (`PORTAL_JELLYFIN_URL`),
  checked live on every sign-in with no cached user list — deliberately simpler
  than status-portal's `jellyfin_auth.py` (no integrations table, no sync task, no
  offline/degraded sign-in mode, no admin-side revocation). Worth revisiting if
  this app ever needs to gate visitor access per-user, or survive a Jellyfin outage
  gracefully for new sign-ins the way status-portal does.
- Search is a full page reload (`GET /?q=...`), not live/incremental like
  status-portal's search-as-you-type. Simpler and avoids turning every keystroke
  into a Steam API call, but worth revisiting for UX if it feels slow in practice.
