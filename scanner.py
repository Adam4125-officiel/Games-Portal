"""
scanner.py — games-folder scan + matching, per the strategy confirmed with the
user (see ROADMAP.md's now-resolved "folder -> game matching strategy" entry):

  1. Exact match first: a Steam AppID embedded in the folder name
     ("Half-Life 2 [220]") or a small sidecar file (".steam-appid" containing
     just the number, for admins who'd rather not rename anything by hand).
     Confident, auto-applied - no admin review needed.
  2. No AppID found: fuzzy-match the folder name against every *active*
     request's title (rapidfuzz). Above the confidence threshold, surfaced to
     the admin as a "possible match?" suggestion - never auto-resolved.
  3. When the admin confirms a suggestion, the folder is renamed to embed the
     AppID (so every future scan matches it exactly, step 1, and never asks
     again), the game is recorded as installed, and the matched request is
     marked done.

This module only ever reads and (on explicit admin confirmation) renames
directories under config.GAMES_FOLDER - never deletes, never touches file
contents, never does anything without GAMES_FOLDER being set.
"""
import logging
import os
import re
import threading
import time

from rapidfuzz import fuzz

import config
import db

_logger = logging.getLogger(__name__)

# Matches a trailing "[220]" or "(220)" tag on a folder name, capturing the
# digits and the untagged name separately.
APPID_TAG_RE = re.compile(r"^(?P<name>.*?)\s*[\[\(](?P<appid>\d+)[\]\)]\s*$")
SIDECAR_FILENAME = ".steam-appid"


def is_enabled():
    return bool(config.GAMES_FOLDER)


def _extract_appid(folder_path, folder_name):
    """An AppID embedded in the folder name or a sidecar file, or None. The
    sidecar is checked first - a name tag can be stale if a game folder was
    renamed by something else after the sidecar was written, and the sidecar
    is the more deliberate of the two (an admin has to create it on purpose)."""
    sidecar_path = os.path.join(folder_path, SIDECAR_FILENAME)
    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            digits = f.read().strip()
        if digits.isdigit():
            return int(digits)
    except OSError:
        pass

    match = APPID_TAG_RE.match(folder_name)
    if match:
        return int(match.group("appid"))
    return None


def _untagged_name(folder_name):
    """The folder name with any trailing AppID tag stripped, for fuzzy
    matching - "Half-Life 2 [220]" and "Half-Life 2" should score identically
    against the request title "Half-Life 2"."""
    match = APPID_TAG_RE.match(folder_name)
    return match.group("name") if match else folder_name


def _list_game_folders():
    try:
        entries = os.listdir(config.GAMES_FOLDER)
    except OSError as e:
        _logger.warning("Could not list games folder %s: %s", config.GAMES_FOLDER, e)
        return []
    folders = []
    for name in sorted(entries):
        path = os.path.join(config.GAMES_FOLDER, name)
        if os.path.isdir(path):
            folders.append((name, path))
    return folders


def scan_once():
    """Walks config.GAMES_FOLDER once. Returns a summary dict. Never raises
    for a missing/unreadable folder - see _list_game_folders() - so this is
    safe to call from both the background thread and the "Scan now" admin
    button without either needing its own error handling."""
    if not is_enabled():
        return {"ok": False, "error": "No games folder configured (PORTAL_GAMES_FOLDER)."}

    folders = _list_game_folders()
    active_requests = [r for r in db.list_requests() if r["status"] != "rejected" and r["status"] != "done"]

    exact_matches = 0
    suggestions = 0
    for folder_name, folder_path in folders:
        appid = _extract_appid(folder_path, folder_name)
        if appid is not None:
            db.upsert_installed_game(appid, folder_name, folder_path)
            db.delete_scan_matches_for_folder(folder_path)
            matching_request = next((r for r in active_requests if r["steam_appid"] == appid), None)
            if matching_request and matching_request["status"] != "done":
                db.update_request_status(matching_request["id"], "done",
                                         "Auto-detected in the games folder.")
            exact_matches += 1
            continue

        rejected = db.get_rejected_pairs(folder_path)
        clean_name = _untagged_name(folder_name)
        for req in active_requests:
            if req["id"] in rejected:
                continue
            score = fuzz.token_sort_ratio(clean_name, req["name"])
            if score >= config.SCANNER_FUZZY_THRESHOLD:
                db.upsert_scan_match(folder_name, folder_path, req["id"], round(score))
                suggestions += 1

    return {"ok": True, "folders_scanned": len(folders), "exact_matches": exact_matches,
           "suggestions": suggestions}


class ScanError(Exception):
    pass


def confirm_match(match_id):
    """Renames the folder to embed the AppID, records it as installed, marks
    the matched request done, and marks this (and any other pending
    suggestion for the same folder) resolved. Raises ScanError with a message
    meant for the admin on any failure - the rename is the one real
    filesystem write in this whole module, so it gets the most care."""
    row = db.get_scan_match(match_id)
    if row is None or row["status"] != "pending":
        raise ScanError("That suggestion no longer exists.")
    request = db.get_request(row["request_id"])
    if request is None:
        raise ScanError("The matched request no longer exists.")

    old_path = row["folder_path"]
    if not os.path.isdir(old_path):
        raise ScanError(f"The folder '{row['folder_name']}' no longer exists on disk.")

    appid = request["steam_appid"]
    new_name = f"{_untagged_name(row['folder_name'])} [{appid}]"
    new_path = os.path.join(os.path.dirname(old_path), new_name)
    if os.path.exists(new_path) and new_path != old_path:
        raise ScanError(f"Can't rename to '{new_name}' - something already exists there.")

    try:
        if new_path != old_path:
            os.rename(old_path, new_path)
    except OSError as e:
        raise ScanError(f"Could not rename the folder: {e}")

    db.upsert_installed_game(appid, new_name, new_path)
    db.delete_scan_matches_for_folder(old_path)
    if new_path != old_path:
        db.delete_scan_matches_for_folder(new_path)
    if request["status"] != "done":
        db.update_request_status(request["id"], "done", "Confirmed installed by the admin.")
    return new_name


def reject_match(match_id):
    row = db.get_scan_match(match_id)
    if row is None:
        raise ScanError("That suggestion no longer exists.")
    db.set_scan_match_status(match_id, "rejected")


# ---------------------------------------------------------------------------
# Background checker - a plain daemon thread, same pattern as updater.py's
# and seerr.py's (this app has no scheduler.py yet).
# ---------------------------------------------------------------------------
_checker_started = False
_checker_lock = threading.Lock()


def start_background_checker():
    global _checker_started
    with _checker_lock:
        if _checker_started:
            return
        _checker_started = True
    threading.Thread(target=_checker_loop, daemon=True, name="games-scanner").start()


def _checker_loop():
    while True:
        if is_enabled():
            try:
                scan_once()
            except Exception:
                _logger.exception("Games-folder scan crashed")
        time.sleep(max(60, config.SCANNER_INTERVAL_SECONDS))
