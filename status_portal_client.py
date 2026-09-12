"""
status_portal_client.py — delegates notification delivery to status-portal
(the sibling project, same author/server) instead of this app building its
own Discord bot / email stack. See CLAUDE.md's status-portal integration
notes for the agreed contract.

Every call from app.py (notify_new_request / notify_status_changed) is
instant: it just drops a job onto an in-memory queue and returns. The one
background worker thread (started once via start_background_worker(), same
started-once-guard shape as scanner.start_background_scanner()) does the
actual HTTP POST, so a slow or unreachable status-portal never blocks the
request/admin action that triggered it. Delivery failures (including "not
configured yet") are logged and dropped - never surfaced to the admin or
requester as an error, per the agreed contract.
"""
import logging
import queue
import threading

import requests

import config
import db

_logger = logging.getLogger(__name__)

_queue = queue.Queue()

_worker_started = False
_worker_lock = threading.Lock()


def start_background_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker_loop, daemon=True, name="status-portal-notify").start()


def _worker_loop():
    while True:
        _process_one(_queue.get())


def _process_one(job):
    """The "never surface a delivery failure" guarantee, isolated from the
    infinite loop above so it's directly unit-testable."""
    try:
        _deliver(job)
    except Exception:
        _logger.warning("status-portal notify delivery failed (%s)", job["path"], exc_info=True)


def _notify_target():
    """(url, api_key) as currently configured, url without a trailing slash -
    or (None, None) if either is unset."""
    url = db.get_status_portal_notify_url().rstrip("/")
    api_key = db.get_status_portal_notify_api_key()
    if not url or not api_key:
        return None, None
    return url, api_key


def _deliver(job):
    url, api_key = _notify_target()
    if url is None:
        _logger.debug("status-portal notify skipped - not configured yet (%s)", job["path"])
        return
    resp = requests.post(f"{url}{job['path']}", json=job["payload"],
                          headers={"X-Api-Key": api_key},
                          timeout=config.STATUS_PORTAL_NOTIFY_TIMEOUT_SECONDS)
    if resp.status_code >= 400:
        _logger.warning("status-portal notify %s returned %s: %s",
                         job["path"], resp.status_code, resp.text[:300])


def send_test_notification():
    """Synchronous, unlike everything else in this module - an admin pressing
    "Send test notification" wants to know right away whether the whole
    chain actually works, not "queued, check the logs later." A sanctioned
    exception to the no-slow-I/O-in-a-request-handler rule, same as an
    explicit one-shot "Scan now" button (see CLAUDE.md). Returns
    {"ok": bool, "message": str} - never raises."""
    url, api_key = _notify_target()
    if url is None:
        return {"ok": False, "message": "Not configured - set a notify URL and API key first."}
    payload = {"event": "test", "subject": "Games Portal test notification",
               "body": "This is a test notification from Games Portal's Integrations page."}
    try:
        resp = requests.post(f"{url}/api/notify/admin", json=payload,
                              headers={"X-Api-Key": api_key},
                              timeout=config.STATUS_PORTAL_NOTIFY_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException as e:
        return {"ok": False, "message": f"Could not reach status-portal: {e}"}
    if resp.status_code >= 400:
        return {"ok": False, "message": f"status-portal returned {resp.status_code}: {resp.text[:300]}"}
    return {"ok": True, "message": f"Delivered - status-portal returned {resp.status_code}."}


def notify_new_request(game_name, requester_name):
    """A new request was just created - the ONLY thing needed for the
    admin-facing alert; status-portal fans this out to whatever admin
    channels (Discord, email) are configured over there."""
    subject = f"New game request: {game_name}"
    body = f'"{game_name}" was requested by {requester_name}.'
    _queue.put({"path": "/api/notify/admin", "payload": {
        "event": "game_request_new", "subject": subject, "body": body,
    }})


def notify_status_changed(jellyfin_user_id, game_name, status):
    """A request's status just changed. No-op if jellyfin_user_id is blank
    (an anonymous request) - the per-user notification only ever applies to
    a request tied to a signed-in Jellyfin identity, and this must never send
    anything for one that isn't, per the agreed contract."""
    if not jellyfin_user_id:
        return
    subject = f'"{game_name}" is now {status}'
    body = f'Your request for "{game_name}" is now {status}.'
    _queue.put({"path": "/api/notify/user", "payload": {
        "jellyfin_user_id": jellyfin_user_id, "event": "gamesportal_request",
        "subject": subject, "body": body,
    }})
