"""
jellyfin_auth.py — Jellyfin as the visitor identity source.

Deliberately narrower than status-portal's module of the same name: this app has
no integrations table yet (one Jellyfin server is configured directly via
config.JELLYFIN_URL) and doesn't cache the Jellyfin user list, so there's no
offline/degraded sign-in mode and no session-revocation sync to run - just a live
credential check, once, at sign-in.

The password is used for exactly one outbound request and is never stored,
logged, or put in the session - and neither is the access token Jellyfin's
AuthenticateByName returns, since the whole point of this call is the identity
check, not an ongoing Jellyfin session. Flask's cookie is signed but not
encrypted, so anything put in it is readable by whoever holds the cookie.
"""
import logging

import requests

import config

_logger = logging.getLogger(__name__)

CLIENT_NAME = "Games Portal"
DEVICE_NAME = "Games Portal"
DEVICE_ID = "games-portal"


def _auth_header():
    """Jellyfin's client-identification header, sent as both Authorization and
    X-Emby-Authorization: modern Jellyfin reads the former, older/Emby-derived
    builds read the latter - sending both costs nothing."""
    value = (f'MediaBrowser Client="{CLIENT_NAME}", Device="{DEVICE_NAME}", '
             f'DeviceId="{DEVICE_ID}", Version="{config.VERSION}"')
    return {"Authorization": value, "X-Emby-Authorization": value}


def is_enabled():
    return bool(config.JELLYFIN_URL)


def authenticate(username, password):
    """Checks a username/password against Jellyfin itself, live. Returns:

        {"ok": True, "user": {"id": ..., "name": ...}}
        {"ok": False, "reason": "not_configured"}
        {"ok": False, "reason": "invalid"}       - Jellyfin said no
        {"ok": False, "reason": "disabled"}      - correct password, disabled account
        {"ok": False, "reason": "unreachable", "error": "..."}

    "unreachable" must never be reported as "wrong password" - telling someone
    their password is wrong when the server is simply down sends them off to
    reset a password that was fine."""
    if not is_enabled():
        return {"ok": False, "reason": "not_configured"}

    base_url = config.JELLYFIN_URL.rstrip("/")
    try:
        r = requests.post(f"{base_url}/Users/AuthenticateByName",
                           json={"Username": username, "Pw": password},
                           headers={"Content-Type": "application/json", **_auth_header()},
                           timeout=config.JELLYFIN_AUTH_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        _logger.warning("Jellyfin sign-in attempt could not reach the server: %s", e)
        return {"ok": False, "reason": "unreachable", "error": str(e)}

    if r.status_code in (400, 401, 403):
        return {"ok": False, "reason": "invalid"}
    if r.status_code >= 500 or not r.ok:
        _logger.warning("Jellyfin sign-in attempt got HTTP %s", r.status_code)
        return {"ok": False, "reason": "unreachable", "error": f"Jellyfin returned HTTP {r.status_code}"}

    try:
        payload = r.json()
        raw_user = payload.get("User") or {}
        token = payload.get("AccessToken")
    except ValueError:
        return {"ok": False, "reason": "unreachable", "error": "Unexpected (non-JSON) response"}

    if not raw_user.get("Id"):
        return {"ok": False, "reason": "unreachable", "error": "Jellyfin returned no user"}

    _revoke_token(base_url, token)

    policy = raw_user.get("Policy") or {}
    if policy.get("IsDisabled"):
        return {"ok": False, "reason": "disabled"}

    return {"ok": True, "user": {"id": raw_user["Id"], "name": raw_user.get("Name") or username}}


def _revoke_token(base_url, token):
    """Best-effort: a failure here means one stale device session left behind in
    Jellyfin's own device list, which is untidy but harmless - it must never turn
    a successful sign-in into a failed one. The token itself is discarded either
    way; see the module docstring for why it's never kept."""
    if not token:
        return
    try:
        requests.post(f"{base_url}/Sessions/Logout",
                       headers={"X-Emby-Token": token, **_auth_header()},
                       timeout=config.JELLYFIN_AUTH_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        _logger.info("Could not revoke the short-lived Jellyfin token after sign-in: %s", e)
