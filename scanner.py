"""scanner.py — background scan of a configured games folder (one subfolder per
installed game), matching each folder to a Steam AppID so the app can avoid
showing/accepting a "Request" for something already installed. Same idea as
Sonarr/Radarr's library scan.

Matching strategy, in order:
  1. An AppID already tagged in the folder name (`{steamapp-<id>}` - see
     tag_folder_name() below) - exact, trusted, no network call needed.
  2. A fuzzy match against Steam's search catalog (steam.py, already used by
     the request flow) for anything untagged - never auto-accepted. A match
     that clears config.SCAN_FUZZY_MATCH_THRESHOLD is surfaced to the admin as
     "possible match?" (see app.py's /admin/scanner); anything below it is
     left unmatched rather than nagging with a low-confidence guess.

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

import config
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


class ScannerError(Exception):
    """Any expected, explainable refusal - raised with a message meant to be
    shown to the admin verbatim (see app.py's admin_scanner_confirm)."""


def is_enabled():
    return bool(config.GAMES_FOLDER)


def _folder_path(folder_name):
    return os.path.join(config.GAMES_FOLDER, folder_name)


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
    # Caught by hand-testing against realistic folder names, not by the mocked
    # unit tests, which happened not to include a case difference.
    scored = [(fuzz.WRatio(query, r["name"], processor=fuzz_utils.default_process), r)
              for r in results]
    score, best = max(scored, key=lambda pair: pair[0])
    return {"appid": best["appid"], "name": best["name"], "score": score}


def scan_once():
    """One full pass over config.GAMES_FOLDER. Returns a summary dict; never
    raises for an ordinary per-folder failure (a Steam timeout just leaves
    that one folder's state as it was). A no-op, empty summary if the
    scanner isn't configured."""
    empty = {"scanned": 0, "matched": 0, "pending_review": 0, "unmatched": 0}
    if not is_enabled():
        return empty

    try:
        entries = os.listdir(config.GAMES_FOLDER)
    except OSError as e:
        _logger.warning(
            "Could not list the games folder %s: %s - skipping this scan pass entirely "
            "rather than treating an unreadable folder as \"nothing is installed any more\"",
            config.GAMES_FOLDER, e)
        return {**empty, "error": str(e)}

    folder_names = sorted(name for name in entries if os.path.isdir(_folder_path(name)))
    counts = {"matched": 0, "pending_review": 0, "unmatched": 0}
    seen = []

    for folder_name in folder_names:
        tagged_appid = extract_tagged_appid(folder_name)
        if tagged_appid is not None:
            db.upsert_scanned_folder(folder_name, "matched", steam_appid=tagged_appid)
            counts["matched"] += 1
            seen.append(folder_name)
            continue

        try:
            candidate = _best_fuzzy_candidate(folder_name)
        except requests.RequestException as e:
            _logger.info("Steam search failed while matching folder %r: %s - leaving its "
                         "state unchanged this cycle", folder_name, e)
            seen.append(folder_name)  # still present on disk - must not be pruned
            continue

        if candidate is not None and candidate["score"] >= config.SCAN_FUZZY_MATCH_THRESHOLD:
            db.upsert_scanned_folder(folder_name, "pending_review",
                                      candidate_appid=candidate["appid"],
                                      candidate_name=candidate["name"],
                                      candidate_score=candidate["score"])
            counts["pending_review"] += 1
        else:
            db.upsert_scanned_folder(folder_name, "unmatched")
            counts["unmatched"] += 1
        seen.append(folder_name)

    db.prune_scanned_folders(seen)
    return {"scanned": len(folder_names), **counts}


def matched_appids():
    return db.matched_appids()


# ---------------------------------------------------------------------------
# Admin-confirmed matches
# ---------------------------------------------------------------------------
def confirm_match(row_id, appid):
    """Renames a folder on disk to embed the confirmed {steamapp-<appid>} tag
    and updates its row. Raises ScannerError (message meant for the admin
    verbatim) on any refusal - the folder went away, the appid doesn't
    resolve on Steam, or the new name collides with an existing folder."""
    row = db.get_scanned_folder(row_id)
    if row is None:
        raise ScannerError("That folder isn't in the scanner's list any more - try scanning again.")
    folder_name = row["folder_name"]

    # Defense in depth only - scan_once() only ever writes plain basenames
    # from os.listdir() into this column, so this should never actually fire.
    if not folder_name or "/" in folder_name or "\\" in folder_name or folder_name in (".", ".."):
        raise ScannerError("That folder's stored name looks unsafe - refusing to touch it.")

    old_path = _folder_path(folder_name)
    games_root = os.path.abspath(config.GAMES_FOLDER)
    if os.path.dirname(os.path.abspath(old_path)) != games_root:
        raise ScannerError("That folder is not directly inside the games folder - refusing to touch it.")
    if not os.path.isdir(old_path):
        raise ScannerError(f'The folder "{folder_name}" no longer exists on disk - try scanning again.')

    summary = steam.fetch_app_summary(appid)
    if summary is None:
        raise ScannerError(f"Steam app {appid} doesn't resolve to a real, listed game.")

    new_name = tag_folder_name(folder_name, appid)
    new_path = _folder_path(new_name)
    if new_name != folder_name:
        if os.path.exists(new_path):
            raise ScannerError(f'Can\'t rename to "{new_name}" - a folder with that name already exists.')
        try:
            os.rename(old_path, new_path)
        except OSError as e:
            raise ScannerError(f"Could not rename the folder: {e}")

    db.mark_folder_matched(row_id, new_name, appid)
    return {"folder_name": new_name, "appid": appid, "name": summary["name"]}


# ---------------------------------------------------------------------------
# Background scanner - a plain daemon thread, same shape as
# updater.start_background_checker(). Safe to call more than once.
# ---------------------------------------------------------------------------
_scanner_started = False
_scanner_lock = threading.Lock()


def start_background_scanner():
    if not is_enabled():
        return
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
        time.sleep(config.SCAN_INTERVAL_SECONDS)
