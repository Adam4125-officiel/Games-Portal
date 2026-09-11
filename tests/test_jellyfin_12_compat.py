"""A real, running stand-in for Jellyfin 12.0's auth rules - not a monkeypatched
`requests.post` like tests/test_jellyfin_auth.py, but an actual local HTTP server
that enforces the same header-resolution order verified straight from Jellyfin's
own source (jellyfin/jellyfin, tag v12.0, AuthorizationContext.cs):
`EnableLegacyAuthorization` defaults to false, which stops the server reading
X-Emby-Token/X-MediaBrowser-Token/X-Emby-Authorization/the lowercase api_key query
param at all - only the plain `Authorization` header (MediaBrowser scheme, a
Token="..." field) and the capitalized ApiKey query param still work, exactly as
jellyfin_auth.py's own docstring claims.

This exists because neither this repo's nor status-portal's committed test suite
actually runs anything that *enforces* that rule - both only assert on the
headers jellyfin_auth.py happens to send. A server that would refuse the old
X-Emby-Token-only pattern and accept the new one is stronger evidence that the
fix (already shipped - see jellyfin_auth._auth_header) actually works against
real 12.0 behavior, not just that it looks right.

Asserting on the *server's own record* of whether it saw a valid-per-12.0
request, not on jellyfin_auth.authenticate()'s return value: _revoke_token()
is documented as best-effort and never inspects the /Sessions/Logout response
(not even its status code), so authenticate() succeeds either way - a naive
"did sign-in succeed" test would pass even if the revoke call were silently
broken, which is exactly the class of false confidence this file exists to
avoid. See test_the_old_x_emby_token_only_pattern_would_be_caught_here below,
which proves this file's own stand-in would actually fail if the revoke call
regressed back to X-Emby-Token."""
import re
import threading

import pytest
from flask import Flask, jsonify, request
from werkzeug.serving import make_server

import config
import jellyfin_auth

FAKE_USER_ID = "12345678123412341234123456789012"
FAKE_USERNAME = "alice"
FAKE_TOKEN = "12.0-issued-token"

_AUTH_TOKEN_RE = re.compile(r'Token="([^"]*)"')


def _build_standin_app(recorded):
    """`recorded` is a plain dict the test reads after the fact - Flask view
    functions can't return extra data to the caller, so server-side state that
    outlives one request has to live in a shared, closed-over object."""
    app = Flask(__name__)

    @app.post("/Users/AuthenticateByName")
    def authenticate():
        # Real Jellyfin never requires a prior token here - this is the call that
        # produces one. EnableLegacyAuthorization has no bearing on it either way.
        return jsonify({
            "User": {"Id": FAKE_USER_ID, "Name": FAKE_USERNAME, "Policy": {"IsDisabled": False}},
            "AccessToken": FAKE_TOKEN,
        })

    @app.get("/Users/Public")
    def public_users():
        # Real Jellyfin serves this with no [Authorize] attribute at all (see
        # jellyfin_auth.list_public_users()'s docstring) - recorded so the test
        # below can prove this app's own call never sent one either, the same
        # "assert on what the server actually saw" spirit as logout() below.
        recorded["public_users_had_auth_header"] = "Authorization" in request.headers
        return jsonify([{"Id": FAKE_USER_ID, "Name": FAKE_USERNAME}])

    @app.post("/Sessions/Logout")
    def logout():
        # The 12.0 rule, enforced for real: only a Token="..." field inside the
        # Authorization header is honoured. X-Emby-Token and friends are never
        # read at all - a request relying on them must be refused here, the same
        # way it would against a real 12.0 server with EnableLegacyAuthorization
        # off. Recorded rather than just returned as a status code, because
        # jellyfin_auth._revoke_token() never looks at the response (see module
        # docstring) - the only reliable signal is what the server itself saw.
        auth_header = request.headers.get("Authorization", "")
        match = _AUTH_TOKEN_RE.search(auth_header)
        valid = bool(match and match.group(1) == FAKE_TOKEN)
        recorded["logout_had_valid_12x_auth"] = valid
        recorded["logout_had_legacy_header"] = "X-Emby-Token" in request.headers
        return ("", 204) if valid else ("", 401)

    return app


@pytest.fixture
def jellyfin_12_standin():
    recorded = {}
    server = make_server("127.0.0.1", 0, _build_standin_app(recorded))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", recorded
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_sign_in_succeeds_against_a_server_enforcing_12s_disabled_legacy_auth(
        jellyfin_12_standin, monkeypatch):
    base_url, _recorded = jellyfin_12_standin
    monkeypatch.setattr(config, "JELLYFIN_URL", base_url)
    result = jellyfin_auth.authenticate(FAKE_USERNAME, "whatever-password")
    assert result == {"ok": True, "user": {"id": FAKE_USER_ID, "name": FAKE_USERNAME}}


def test_the_revoke_call_actually_satisfies_the_servers_12x_rule(jellyfin_12_standin, monkeypatch):
    """The real point of this file: not just that sign-in succeeds (which it
    would even if the revoke call were silently broken - see module docstring),
    but that the /Sessions/Logout request jellyfin_auth actually sent is one a
    genuine 12.0 server (legacy auth disabled) would have accepted, and that it
    never relied on the legacy X-Emby-Token header to do it."""
    base_url, recorded = jellyfin_12_standin
    monkeypatch.setattr(config, "JELLYFIN_URL", base_url)
    jellyfin_auth.authenticate(FAKE_USERNAME, "whatever-password")
    assert recorded.get("logout_had_valid_12x_auth") is True
    assert recorded.get("logout_had_legacy_header") is False


def test_list_public_users_against_a_real_running_server(jellyfin_12_standin, monkeypatch):
    """Proves list_public_users() actually parses a real HTTP round trip (real
    Flask JSON serialization, a real socket) end to end, not just a
    hand-crafted mock - and that it sends no Authorization header at all,
    which is what makes it unaffected by 12.0's legacy-auth change in the
    first place (verified separately, straight from jellyfin/jellyfin's own
    source - see the function's own docstring)."""
    base_url, recorded = jellyfin_12_standin
    monkeypatch.setattr(config, "JELLYFIN_URL", base_url)
    result = jellyfin_auth.list_public_users()
    assert result == {"ok": True, "users": [{"id": FAKE_USER_ID, "name": FAKE_USERNAME}]}
    assert recorded.get("public_users_had_auth_header") is False


def test_the_old_x_emby_token_only_pattern_would_be_caught_here(jellyfin_12_standin):
    """Proves the stand-in - and therefore the test above - is a real regression
    guard, not a tautology: a request using *only* the pre-12.0 X-Emby-Token
    header, with no Authorization Token field at all, must be recorded as
    invalid and refused by this same server."""
    import requests
    base_url, recorded = jellyfin_12_standin
    response = requests.post(f"{base_url}/Sessions/Logout",
                              headers={"X-Emby-Token": FAKE_TOKEN}, timeout=5)
    assert response.status_code == 401
    assert recorded.get("logout_had_valid_12x_auth") is False
