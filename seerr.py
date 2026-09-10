"""
seerr.py — READ-ONLY contact sync from Jellyseerr/Overseerr, adapted from
status-portal's `integrations.fetch_seerr_users` + `user_notify.sync_seerr_contacts`.

Every request this module makes is a GET. Nothing here ever creates,
approves, or modifies a request, a user, or any setting on Seerr's side -
this app is a request *tracker* of its own, not a client of Seerr's request
queue, and the only reason to talk to Seerr at all is that it already knows
each Jellyfin account's email and Discord ID (from importing them), which
this app needs to notify a visitor about their own request's status.

Single-instance, env-var config (config.SEERR_URL/SEERR_API_KEY) rather than
an integrations table - same simplification as jellyfin_auth.py, for the
same reason: this app has no integrations table, and one Seerr is the normal
case for a single-admin portal.
"""
import logging
import threading
import time

import requests

import config
import db

_logger = logging.getLogger(__name__)


def is_enabled():
    return bool(config.SEERR_URL and config.SEERR_API_KEY)


def _seerr_email(raw):
    """Seerr's `email` for a Jellyfin-imported user is only sometimes an
    email - when the Jellyfin account has none, Seerr puts the username
    there instead. Anything that isn't address-shaped is dropped rather than
    cached, so a bare username never reaches an SMTP server and comes back
    as an RFC 5321 rejection."""
    value = (raw or "").strip()
    return value if db.looks_like_email(value) else ""


def _first_discord_id(settings):
    """Seerr allows several Discord IDs per user; this app sends to one.
    First non-empty entry, tolerating the older singular `discordId`
    spelling."""
    ids = settings.get("discordIds")
    if isinstance(ids, list):
        return next((str(i).strip() for i in ids if str(i).strip()), "")
    return str(settings.get("discordId") or "").strip()


def _fetch_notification_settings(base_url, user_id):
    """One user's notification settings, where Seerr actually keeps their
    Discord ID - it lives on a per-user sub-resource, not the base user
    record. GET only."""
    r = requests.get(f"{base_url}/api/v1/user/{user_id}/settings/notifications",
                     headers={"X-Api-Key": config.SEERR_API_KEY},
                     timeout=config.SEERR_TIMEOUT_SECONDS)
    r.raise_for_status()
    payload = r.json()
    return payload if isinstance(payload, dict) else {}


def fetch_users(limit=200):
    """Every Seerr user, normalised - including `jellyfin_user_id`, which is
    the whole point of this call. Seerr can import Jellyfin accounts, and
    when it has, each Seerr user carries the Jellyfin id it was imported
    from - the only link this app will follow (matching on email or username
    instead would eventually attach one person's contact details to
    another). GET only, throughout."""
    base_url = config.SEERR_URL.rstrip("/")
    r = requests.get(f"{base_url}/api/v1/user",
                     headers={"X-Api-Key": config.SEERR_API_KEY},
                     params={"take": limit}, timeout=config.SEERR_TIMEOUT_SECONDS)
    r.raise_for_status()
    payload = r.json()
    results = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(results, list):
        raise ValueError("Unexpected response from Seerr's /api/v1/user")

    users = []
    for entry in results:
        embedded = entry.get("settings") or {}
        jellyfin_id = str(entry.get("jellyfinUserId") or entry.get("jellyfinId") or "")
        if not jellyfin_id or entry.get("id") is None:
            continue
        users.append({
            "id": str(entry["id"]),
            "display_name": entry.get("displayName") or entry.get("username") or "",
            "email": _seerr_email(entry.get("email")),
            "discord_id": _first_discord_id(embedded),
            "jellyfin_user_id": jellyfin_id,
        })

    # One extra GET per *linked* user, for the Discord ID Seerr only exposes
    # on the notification-settings sub-resource. A failure on one user
    # leaves the rest (and their email) intact.
    for user in users:
        try:
            settings = _fetch_notification_settings(base_url, user["id"])
        except (requests.RequestException, ValueError) as e:
            _logger.info("Could not read Seerr notification settings for user %s: %s", user["id"], e)
            continue
        user["discord_id"] = _first_discord_id(settings) or user["discord_id"]

    return users


class SyncError(Exception):
    pass


def sync_contacts():
    """Refreshes the local mirror of what Seerr holds. Replaced wholesale on
    success, left completely alone on failure, same shape as this app's
    other periodic syncs."""
    if not is_enabled():
        raise SyncError("Seerr isn't configured (PORTAL_SEERR_URL/PORTAL_SEERR_API_KEY).")
    try:
        users = fetch_users()
    except requests.RequestException as e:
        raise SyncError(f"Could not reach Seerr: {e}")
    except ValueError as e:
        raise SyncError(str(e))
    # db.replace_seerr_contacts() names the Seerr-side id "seerr_user_id" (it
    # sits next to this app's own visitor id in that table); fetch_users()
    # calls it "id" as the more natural name for a general-purpose fetch.
    # Mapped explicitly here rather than unifying the two names, so each
    # stays the right name for what it's read next to.
    contacts = [{**u, "seerr_user_id": u["id"]} for u in users]
    db.replace_seerr_contacts(contacts)
    with_contact = sum(1 for u in users if u["email"] or u["discord_id"])
    _logger.info("Seerr contact sync: cached %d linked account(s), %d with contact details.",
                len(users), with_contact)
    return len(users), with_contact


# ---------------------------------------------------------------------------
# Background checker - a plain daemon thread, same pattern as updater.py's
# (this app has no scheduler.py yet - see that module's own note on this).
# ---------------------------------------------------------------------------
_checker_started = False
_checker_lock = threading.Lock()


def start_background_checker():
    global _checker_started
    with _checker_lock:
        if _checker_started:
            return
        _checker_started = True
    threading.Thread(target=_checker_loop, daemon=True, name="seerr-sync").start()


def _checker_loop():
    while True:
        if is_enabled():
            try:
                sync_contacts()
            except SyncError as e:
                _logger.warning("Seerr contact sync failed: %s", e)
            except Exception:
                _logger.exception("Seerr contact sync crashed")
        time.sleep(max(60, config.SEERR_SYNC_INTERVAL_SECONDS))
