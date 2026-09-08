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

- **Who a request belongs to.** No visitor accounts are specced yet. Options: fully
  anonymous with a free-text name field, or reuse status-portal's Jellyfin-backed
  visitor login if a shared identity across both portals is ever wanted. Decide
  before building the request form — retrofitting an identity model onto existing
  request rows is more work than picking one up front.

- **Games-folder mount.** Native Python vs Docker changes whether the scanned path
  is a plain local path or a bind mount — settle alongside how this gets deployed.

## Ideas (unranked)

- Show a small "already installed" badge directly on a search result if the
  scanner has already matched it, instead of only surfacing that inside a request.
- Discord notification on new request / status change, mirroring status-portal's
  optional webhook pattern.
