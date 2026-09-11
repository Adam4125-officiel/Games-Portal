"""scanner.py — background scan of one or more configured games folders (one
subfolder per installed game), matching each folder to a Steam AppID so the
app can avoid showing/accepting a "Request" for something already installed.
Same idea as Sonarr/Radarr's library scan.

Configuration lives entirely in the database, edited from /admin/scanner -
no .env editing or restart required, unlike this module's first version.
Multiple root folders are supported (e.g. one per physical disk); each is
scanned independently, so one being temporarily offline never affects the
others. See db.py's games_folders table (add_games_folder() etc.) and this
module's games_folders()/client_path_for() below.

Matching strategy, in order:
  1. An AppID already tagged in the folder name (`{steamapp-<id>}` - see
     tag_folder_name() below) - exact, trusted, no network call needed.
  2. A fuzzy match against Steam's search catalog (steam.py, already used by
     the request flow) for anything untagged - never auto-accepted. A match
     that clears the configured fuzzy-match threshold is surfaced to the
     admin as "possible match?" (see app.py's /admin/scanner); anything
     below it is left unmatched rather than nagging with a low-confidence
     guess.

Once the admin confirms a match (whichever way it was found), the folder is
renamed on disk to embed the tag - so the *next* scan recognizes it directly
via step 1 instead of fuzzy-matching it again. That's why there's no separate
"how was this matched" state to track: tagged and matched collapse into the
same thing one scan cycle after confirmation.

Mirrors updater.py's background-thread shape: a daemon thread does the
periodic work and writes to the database (installed_games, in db.py); request
handlers only ever read it (db.matched_appids()) - see CLAUDE.md's "No slow
I/O in a request handler" rule. The one sanctioned exception is the admin's
"Scan now" button, an explicit one-shot action.
"""
import logging
import os
import re
import threading
import time

import requests
from rapidfuzz import fuzz, utils as fuzz_utils

import db
import steam

_logger = logging.getLogger(__name__)

# {steamapp-<appid>}, e.g. "Half-Life 2 {steamapp-220}". Curly braces are
# filesystem-safe on Windows/Linux/macOS, and this deliberately echoes
# Sonarr's own real {tvdb-<id>} convention rather than inventing an unrelated
# one - see README for the documented format.
TAG_RE = re.compile(r"\{steamapp-(\d+)\}")

# Any bracketed group - not just this app's own tag - is release-group/version
# noise for search purposes ("Cyberpunk 2077 (GOG)", "Hollow Knight [v1.5]").
_BRACKET_GROUP_RE = re.compile(r"[(\[{][^()\[\]{}]*[)\]}]")
_SEPARATOR_RE = re.compile(r"[._-]+")
_WHITESPACE_RE = re.compile(r"\s+")

# ---------------------------------------------------------------------------
# Settings - all admin-editable from /admin/scanner, stored as DB settings
# rather than env vars (unlike this module's first version). A filesystem
# path list is exactly the kind of thing that's tedious to maintain by
# hand-editing .env and restarting every time a disk is added or removed;
# this is a routine admin-tunable toggle, not deploy-time config, per
# CLAUDE.md's config-split rule.
# ---------------------------------------------------------------------------
SCAN_INTERVAL_MINUTES_SETTING = "scanner_interval_minutes"
FUZZY_THRESHOLD_SETTING = "scanner_fuzzy_match_threshold"
RECENTLY_ADDED_COUNT_SETTING = "recently_added_count"

DEFAULT_SCAN_INTERVAL_MINUTES = 60
DEFAULT_FUZZY_MATCH_THRESHOLD = 82
DEFAULT_RECENTLY_ADDED_COUNT = 10
MIN_SCAN_INTERVAL_SECONDS = 60
MAX_RECENTLY_ADDED_COUNT = 50


class ScannerError(Exception):
    """Any expected, explainable refusal - raised with a message meant to be
    shown to the admin verbatim (see app.py's admin_scanner_confirm)."""


def games_folders():
    """Every configured root folder's real, server-side path, as a list -
    what scanner.scan_once() actually walks. See db.games_folders (the
    table) for the admin-managed per-folder label/client_path that go with
    each one; those are looked up separately (client_path_for() below) since
    scan_once() itself only ever needs the bare paths."""
    return [row["path"] for row in db.list_games_folders()]


def client_path_for(root_path):
    """The path a *visitor* should be told for this root, for the
    /collections page - the admin-entered client_path if set, otherwise the
    server's own path as a fallback (still correct information, just not
    guaranteed to mean anything on a visitor's own machine - the whole reason
    client_path exists is that a server-side D:\\Games and a visitor's own
    \\\\HOMESERVER\\Games or mapped drive letter are frequently not the same
    string at all). Returns None if this root isn't configured at all any
    more (a row that existed when a game was scanned but was since removed)."""
    for row in db.list_games_folders():
        if row["path"] == root_path:
            return row["client_path"].strip() or row["path"]
    return None


def folder_label_for(root_path):
    """This root's admin-set label ("SSD", "Main games drive", ...), or ""
    if it has none or isn't configured any more. Powers the "Available on
    <label>" badge (see app.py's _matched_games_by_appid) - falls back to a
    plain "Available" badge when blank, same fallback shape as
    client_path_for() above."""
    for row in db.list_games_folders():
        if row["path"] == root_path:
            return row["label"].strip()
    return ""


def split_genres(genres):
    """installed_games.genres (a comma-joined string, "" or NULL if none) ->
    a plain list, for grouping /collections into genre rows."""
    return [g for g in (genres or "").split(",") if g]


def display_path_for_game(root_path, folder_name):
    """The full client-facing path to one installed game - the resolved
    client-facing root (see client_path_for()) joined with the folder's own
    name (tag stripped, since that's this app's own bookkeeping, meaningless
    to a visitor browsing a file share). Join style follows whatever the
    root itself looks like: a backslash for a Windows-shaped root (a UNC
    path, or a drive letter like "Z:\\"), a forward slash otherwise. Returns
    None if the root isn't configured any more."""
    root = client_path_for(root_path)
    if root is None:
        return None
    name = strip_tag(folder_name)
    is_windows_style = root.startswith("\\\\") or re.match(r"^[A-Za-z]:", root)
    separator = "\\" if is_windows_style else "/"
    return root.rstrip("\\/") + separator + name


def scan_interval_seconds():
    raw = db.get_setting(SCAN_INTERVAL_MINUTES_SETTING, str(DEFAULT_SCAN_INTERVAL_MINUTES))
    minutes = int(raw) if raw.isdigit() else DEFAULT_SCAN_INTERVAL_MINUTES
    return max(MIN_SCAN_INTERVAL_SECONDS, minutes * 60)


def set_scan_interval_minutes(minutes):
    db.set_setting(SCAN_INTERVAL_MINUTES_SETTING, str(max(1, minutes)))


def fuzzy_match_threshold():
    raw = db.get_setting(FUZZY_THRESHOLD_SETTING, str(DEFAULT_FUZZY_MATCH_THRESHOLD))
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_FUZZY_MATCH_THRESHOLD
    return min(100, max(0, value))


def set_fuzzy_match_threshold(value):
    db.set_setting(FUZZY_THRESHOLD_SETTING, str(min(100, max(0, value))))


def recently_added_count():
    """How many games the public "Recently added" strip shows - admin-
    editable from /admin/scanner, same DB-settings pattern as the interval/
    threshold above."""
    raw = db.get_setting(RECENTLY_ADDED_COUNT_SETTING, str(DEFAULT_RECENTLY_ADDED_COUNT))
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_RECENTLY_ADDED_COUNT
    return min(MAX_RECENTLY_ADDED_COUNT, max(1, value))


def set_recently_added_count(value):
    db.set_setting(RECENTLY_ADDED_COUNT_SETTING, str(min(MAX_RECENTLY_ADDED_COUNT, max(1, int(value)))))


def is_enabled():
    return bool(games_folders())


def folder_exists(root):
    """A cheap local stat - whether a configured root currently resolves to a
    real, readable directory. Used only to show the admin a status indicator
    per configured folder (a disk that isn't plugged in, a typo'd path), not
    to gate scanning itself - see scan_once()'s own per-root handling."""
    return os.path.isdir(root)


# ---------------------------------------------------------------------------
# Tag handling
# ---------------------------------------------------------------------------
def extract_tagged_appid(folder_name):
    match = TAG_RE.search(folder_name)
    return int(match.group(1)) if match else None


def strip_tag(folder_name):
    return TAG_RE.sub("", folder_name).strip()


def tag_folder_name(folder_name, appid):
    """This app's own tag, appended - replacing any existing one rather than
    doubling up, so re-tagging a folder (a corrected manual match) never
    produces "Name {steamapp-1} {steamapp-2}"."""
    return f"{strip_tag(folder_name)} {{steamapp-{appid}}}"


def _clean_for_search(folder_name):
    """A folder name -> a plausible Steam search query: bracketed groups
    stripped, separators turned to spaces, whitespace collapsed."""
    text = _BRACKET_GROUP_RE.sub(" ", folder_name)
    text = _SEPARATOR_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------
def _best_fuzzy_candidate(folder_name):
    """The best-scoring Steam search result for this folder's cleaned name, or
    None if Steam has no results at all (or the cleaned name is empty). Lets
    requests.RequestException propagate on a genuine network/timeout failure -
    the caller must leave that folder's existing state untouched rather than
    downgrading it over a transient Steam outage, so "no candidate" and
    "couldn't check" are deliberately not the same return value."""
    query = _clean_for_search(folder_name)
    if not query:
        return None
    results = steam.search(query)
    if not results:
        return None
    # processor=default_process lower-cases and strips punctuation/whitespace
    # before scoring - without it, fuzz.WRatio is case-sensitive, so "Elden
    # Ring" vs. Steam's "ELDEN RING" scores 30/100 instead of a perfect 100.
    scored = [(fuzz.WRatio(query, r["name"], processor=fuzz_utils.default_process), r)
              for r in results]
    score, best = max(scored, key=lambda pair: pair[0])
    return {"appid": best["appid"], "name": best["name"], "score": score}


def _join_genres(genres):
    return ",".join(genres) if genres else ""


def _cached_or_fetched_details(existing_row, appid):
    """(name, icon_url, short_description, genres) for a matched appid, shown
    on the public /collections page (genres also drives its genre-row
    grouping). genres is a comma-joined string, matching the
    installed_games.genres column, or "" if Steam listed none. Reused from
    `existing_row` when it's already cached for this same appid *and* already
    has a genres value - a row matched before the genres column existed has
    name/icon/description cached but genres still NULL, and that None (not
    "") is exactly what forces one backfill fetch the next time it's
    scanned, rather than leaving it uncategorized forever. Otherwise, a
    tagged folder never changes, so re-fetching every scan cycle would be
    pure waste (and unlike the fuzzy-match step, there's no confidence
    question here to re-check). A fetch failure (Steam down, a delisted app)
    falls back to whatever was already cached rather than blanking it out -
    a transient outage must not erase a perfectly good cached name/icon."""
    if (existing_row and existing_row.get("steam_appid") == appid and existing_row.get("name")
            and existing_row.get("genres") is not None):
        return (existing_row["name"], existing_row["icon_url"], existing_row["short_description"],
                existing_row["genres"])
    summary = steam.fetch_app_summary(appid)
    if summary:
        return summary["name"], summary["icon_url"], summary["short_description"], _join_genres(summary["genres"])
    if existing_row:
        return (existing_row.get("name"), existing_row.get("icon_url"),
                existing_row.get("short_description"), existing_row.get("genres"))
    return None, None, None, None


def _resolved_matched_at(existing_row, appid):
    """The matched_at timestamp to store for this row - preserved from
    `existing_row` if it was already matched under this exact appid (nothing
    new happened, so its place in "recently added" shouldn't move), freshly
    stamped otherwise (this is either a genuinely new match or a different
    game than what used to be here)."""
    if (existing_row and existing_row.get("status") == "matched"
            and existing_row.get("steam_appid") == appid and existing_row.get("matched_at")):
        return existing_row["matched_at"]
    return db.now_iso()


def scan_once():
    """One full pass over every configured root folder. Returns a summary
    dict; never raises for an ordinary per-folder failure (a Steam timeout
    just leaves that one folder's state as it was) or a single root being
    unreadable (a disk that's offline just leaves *its* existing entries
    untouched - the other configured roots are still scanned normally). A
    no-op, empty summary if nothing is configured yet."""
    empty = {"scanned": 0, "matched": 0, "pending_review": 0, "unmatched": 0}
    roots = games_folders()
    if not roots:
        return empty

    threshold = fuzzy_match_threshold()
    counts = {"matched": 0, "pending_review": 0, "unmatched": 0}
    seen_pairs = set()
    scanned_roots = set()
    errors = []
    # One query, reused per folder below, rather than a lookup per folder -
    # this is what lets a tagged folder's cached Steam details be reused
    # instead of re-fetched from Steam every single scan cycle.
    existing_by_key = {(row["root_path"], row["folder_name"]): row for row in db.list_scanned_folders()}

    for root in roots:
        try:
            entries = os.listdir(root)
        except OSError as e:
            _logger.warning(
                "Could not list games folder %s: %s - leaving its existing entries unchanged "
                "rather than treating an unreadable folder as \"nothing is installed any more\"",
                root, e)
            errors.append(f"{root}: {e}")
            continue
        scanned_roots.add(root)

        folder_names = sorted(name for name in entries if os.path.isdir(os.path.join(root, name)))
        for folder_name in folder_names:
            seen_pairs.add((root, folder_name))
            tagged_appid = extract_tagged_appid(folder_name)
            if tagged_appid is not None:
                existing = existing_by_key.get((root, folder_name))
                name, icon_url, short_description, genres = _cached_or_fetched_details(existing, tagged_appid)
                matched_at = _resolved_matched_at(existing, tagged_appid)
                db.upsert_scanned_folder(root, folder_name, "matched", steam_appid=tagged_appid,
                                          name=name, icon_url=icon_url, short_description=short_description,
                                          matched_at=matched_at, genres=genres)
                counts["matched"] += 1
                continue

            try:
                candidate = _best_fuzzy_candidate(folder_name)
            except requests.RequestException as e:
                _logger.info("Steam search failed while matching folder %r under %s: %s - "
                             "leaving its state unchanged this cycle", folder_name, root, e)
                continue

            if candidate is not None and candidate["score"] >= threshold:
                db.upsert_scanned_folder(root, folder_name, "pending_review",
                                          candidate_appid=candidate["appid"],
                                          candidate_name=candidate["name"],
                                          candidate_score=candidate["score"])
                counts["pending_review"] += 1
            else:
                db.upsert_scanned_folder(root, folder_name, "unmatched")
                counts["unmatched"] += 1

    db.prune_scanned_folders(roots, scanned_roots, seen_pairs)
    result = {"scanned": len(seen_pairs), **counts}
    if errors:
        result["errors"] = errors
    return result


def matched_appids():
    return db.matched_appids()


# ---------------------------------------------------------------------------
# Admin-confirmed matches
# ---------------------------------------------------------------------------
def confirm_match(row_id, appid):
    """Renames a folder on disk to embed the confirmed {steamapp-<appid>} tag
    and updates its row. Raises ScannerError (message meant for the admin
    verbatim) on any refusal - the folder went away, its games folder isn't
    configured any more, the appid doesn't resolve on Steam, or the new name
    collides with an existing folder."""
    row = db.get_scanned_folder(row_id)
    if row is None:
        raise ScannerError("That folder isn't in the scanner's list any more - try scanning again.")
    folder_name = row["folder_name"]
    root = row["root_path"]

    if root not in games_folders():
        raise ScannerError("That folder's games folder isn't configured any more - try scanning again.")
    # Defense in depth only - scan_once() only ever writes plain basenames
    # from os.listdir() into this column, so this should never actually fire.
    if not folder_name or "/" in folder_name or "\\" in folder_name or folder_name in (".", ".."):
        raise ScannerError("That folder's stored name looks unsafe - refusing to touch it.")

    old_path = os.path.join(root, folder_name)
    root_abs = os.path.abspath(root)
    if os.path.dirname(os.path.abspath(old_path)) != root_abs:
        raise ScannerError("That folder is not directly inside its games folder - refusing to touch it.")
    if not os.path.isdir(old_path):
        raise ScannerError(f'The folder "{folder_name}" no longer exists on disk - try scanning again.')

    summary = steam.fetch_app_summary(appid)
    if summary is None:
        raise ScannerError(f"Steam app {appid} doesn't resolve to a real, listed game.")

    new_name = tag_folder_name(folder_name, appid)
    new_path = os.path.join(root, new_name)
    if new_name != folder_name:
        if os.path.exists(new_path):
            raise ScannerError(f'Can\'t rename to "{new_name}" - a folder with that name already exists.')
        try:
            os.rename(old_path, new_path)
        except OSError as e:
            raise ScannerError(f"Could not rename the folder: {e}")

    matched_at = _resolved_matched_at(row, appid)
    db.mark_folder_matched(row_id, new_name, appid, name=summary["name"], icon_url=summary["icon_url"],
                            short_description=summary["short_description"], matched_at=matched_at,
                            genres=_join_genres(summary["genres"]))
    return {"folder_name": new_name, "appid": appid, "name": summary["name"], "root_path": root}


# ---------------------------------------------------------------------------
# Background scanner - a plain daemon thread, same shape as
# updater.start_background_checker(). Safe to call more than once. Always
# started, even with nothing configured yet - scan_once() itself is a cheap
# no-op until the admin adds a folder from /admin/scanner, and starting
# unconditionally means a folder added later takes effect on the next cycle
# with no restart needed, matching every other DB-backed setting in this app.
# ---------------------------------------------------------------------------
_scanner_started = False
_scanner_lock = threading.Lock()


def start_background_scanner():
    global _scanner_started
    with _scanner_lock:
        if _scanner_started:
            return
        _scanner_started = True
    threading.Thread(target=_scanner_loop, daemon=True, name="games-scanner").start()


def _scanner_loop():
    while True:
        try:
            scan_once()
        except Exception:
            _logger.exception("Background games-folder scan failed")
        time.sleep(scan_interval_seconds())
