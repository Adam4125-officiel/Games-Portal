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


def _auth_header(token=None):
    """Jellyfin's client-identification header - a `Token` field is appended when
    one is available (e.g. to revoke it below), rather than sent via the separate
    X-Emby-Token header.

    Jellyfin 12.0 (2026-09) disables legacy authorization by default and migrates
    existing installs to match: X-Emby-Token, X-Emby-Authorization, the lowercase
    api_key query param, and the "Emby" auth scheme all stop being read unless an
    admin explicitly re-enables EnableLegacyAuthorization. The plain `Authorization`
    header with the `MediaBrowser` scheme is unaffected - it's been checked first,
    unconditionally, since at least Jellyfin 10.6 - so sending Client/Device/
    DeviceId/Version/Token there instead is both the fix for 12.0 and backward-
    compatible with everything older. Sent as both Authorization and
    X-Emby-Authorization anyway (costs nothing) for the rare very old/Emby-derived
    build that only reads the latter."""
    parts = f'Client="{CLIENT_NAME}", Device="{DEVICE_NAME}", DeviceId="{DEVICE_ID}", Version="{config.VERSION}"'
    if token:
        parts += f', Token="{token}"'
    value = f"MediaBrowser {parts}"
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


def list_public_users():
    """Every publicly-visible Jellyfin user (id, name) - straight from
    GET /Users/Public, the same list Jellyfin's own login screen uses to show
    avatars to pick from. Powers /admin/limits' per-user override list, so an
    override can be set for a visitor before they've ever signed in here.

    Deliberately unauthenticated - no API key involved, matching this app's
    existing no-API-key stance elsewhere (Steam search, this module's own
    AuthenticateByName call). Verified directly against jellyfin/jellyfin's
    own source (Jellyfin.Api/Controllers/UserController.cs) at tags v10.7.0
    and v12.0: GetPublicUsers() has no [Authorize] attribute in either, and
    the endpoint's path/shape is unchanged across that whole range - so this
    isn't a repeat of the AuthenticateByName header issue 12.0 caused
    elsewhere, since there was never an auth header here to begin with. It
    excludes hidden and disabled accounts (Jellyfin's own filtering) and,
    when this app isn't running on the same host/LAN as Jellyfin, accounts
    without remote-access enabled - both are Jellyfin's login-screen
    behavior, not something this app adds on top.

    Returns {"ok": True, "users": [{"id": ..., "name": ...}, ...]} on
    success, {"ok": False, "users": []} if Jellyfin isn't configured or
    couldn't be reached - "ok" lets the caller tell a real empty result
    (nobody public, or everyone filtered out) apart from a failure worth
    surfacing, the same shape authenticate() already uses this module."""
    if not is_enabled():
        return {"ok": False, "users": []}

    base_url = config.JELLYFIN_URL.rstrip("/")
    try:
        r = requests.get(f"{base_url}/Users/Public", timeout=config.JELLYFIN_AUTH_TIMEOUT_SECONDS)
        r.raise_for_status()
        payload = r.json()
    except (requests.RequestException, ValueError) as e:
        _logger.info("Could not fetch Jellyfin's public user list: %s", e)
        return {"ok": False, "users": []}

    if not isinstance(payload, list):
        _logger.info("Jellyfin's public user list returned an unexpected shape")
        return {"ok": False, "users": []}

    users = [{"id": raw["Id"], "name": raw.get("Name") or ""} for raw in payload if raw.get("Id")]
    return {"ok": True, "users": users}


def _revoke_token(base_url, token):
    """Best-effort: a failure here means one stale device session left behind in
    Jellyfin's own device list, which is untidy but harmless - it must never turn
    a successful sign-in into a failed one. The token itself is discarded either
    way; see the module docstring for why it's never kept.

    Authenticates via the Token field in the Authorization header (see
    _auth_header), not the legacy X-Emby-Token header - Jellyfin 12.0+ ignores the
    latter by default, which would otherwise make this silently no-op forever
    against a current server without ever raising an error here."""
    if not token:
        return
    try:
        requests.post(f"{base_url}/Sessions/Logout",
                       headers=_auth_header(token=token),
                       timeout=config.JELLYFIN_AUTH_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        _logger.info("Could not revoke the short-lived Jellyfin token after sign-in: %s", e)
