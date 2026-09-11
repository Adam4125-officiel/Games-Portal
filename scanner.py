"""scanner.py — background scan of one or more configured games folders (one
subfolder per installed game), matching each folder to a Steam AppID so the
app can avoid showing/accepting a "Request" for something already installed.
Same idea as Sonarr/Radarr's library scan.

Configuration lives entirely in the database, edited from /admin/scanner -
no .env editing or restart required, unlike this module's first version.
Multiple root folders are supported (e.g. one per physical disk); each is
scanned independently, so one being temporarily offline never affects the
others. See games_folders()/set_games_folders() below.

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
GAMES_FOLDERS_SETTING = "scanner_games_folders"
SCAN_INTERVAL_MINUTES_SETTING = "scanner_interval_minutes"
FUZZY_THRESHOLD_SETTING = "scanner_fuzzy_match_threshold"

DEFAULT_SCAN_INTERVAL_MINUTES = 60
DEFAULT_FUZZY_MATCH_THRESHOLD = 82
MIN_SCAN_INTERVAL_SECONDS = 60


class ScannerError(Exception):
    """Any expected, explainable refusal - raised with a message meant to be
    shown to the admin verbatim (see app.py's admin_scanner_confirm)."""


def games_folders():
    """Every configured root folder, as a list, newest-edited-wins - stored as
    one newline-separated DB setting (the same "one text setting, cleaned on
    save" shape this house style already uses for other admin-entered lists,
    e.g. status-portal's notification recipient list)."""
    raw = db.get_setting(GAMES_FOLDERS_SETTING, "")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def set_games_folders(raw_text):
    """Cleans admin-entered textarea input (blank lines, stray whitespace,
    accidental duplicates) into a canonical newline-separated string before
    saving."""
    seen = set()
    deduped = []
    for line in (raw_text or "").splitlines():
        folder = line.strip()
        if folder and folder not in seen:
            seen.add(folder)
            deduped.append(folder)
    db.set_setting(GAMES_FOLDERS_SETTING, "\n".join(deduped))


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
                db.upsert_scanned_folder(root, folder_name, "matched", steam_appid=tagged_appid)
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

    db.mark_folder_matched(row_id, new_name, appid)
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
