import requests

import jellyfin_auth


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200, ok=True):
        self._json = json_data or {}
        self.status_code = status_code
        self.ok = ok

    def json(self):
        return self._json


def test_is_enabled_reflects_config(monkeypatch):
    monkeypatch.setattr(jellyfin_auth.config, "JELLYFIN_URL", "")
    assert jellyfin_auth.is_enabled() is False
    monkeypatch.setattr(jellyfin_auth.config, "JELLYFIN_URL", "http://jellyfin:8096")
    assert jellyfin_auth.is_enabled() is True


def test_authenticate_not_configured(monkeypatch):
    monkeypatch.setattr(jellyfin_auth.config, "JELLYFIN_URL", "")
    result = jellyfin_auth.authenticate("alice", "pw")
    assert result == {"ok": False, "reason": "not_configured"}


def test_authenticate_success(monkeypatch):
    monkeypatch.setattr(jellyfin_auth.config, "JELLYFIN_URL", "http://jellyfin:8096")

    def fake_post(url, **kwargs):
        if url.endswith("/Users/AuthenticateByName"):
            return _FakeResponse({"User": {"Id": "abc123", "Name": "alice", "Policy": {"IsDisabled": False}},
                                   "AccessToken": "tok"}, status_code=200)
        return _FakeResponse(status_code=204)

    monkeypatch.setattr(jellyfin_auth.requests, "post", fake_post)
    result = jellyfin_auth.authenticate("alice", "correct-password")
    assert result == {"ok": True, "user": {"id": "abc123", "name": "alice"}}


def test_authenticate_invalid_credentials(monkeypatch):
    monkeypatch.setattr(jellyfin_auth.config, "JELLYFIN_URL", "http://jellyfin:8096")

    def fake_post(url, **kwargs):
        return _FakeResponse(status_code=401)

    monkeypatch.setattr(jellyfin_auth.requests, "post", fake_post)
    result = jellyfin_auth.authenticate("alice", "wrong-password")
    assert result == {"ok": False, "reason": "invalid"}


def test_authenticate_disabled_account(monkeypatch):
    monkeypatch.setattr(jellyfin_auth.config, "JELLYFIN_URL", "http://jellyfin:8096")

    def fake_post(url, **kwargs):
        if url.endswith("/Users/AuthenticateByName"):
            return _FakeResponse({"User": {"Id": "abc123", "Name": "alice", "Policy": {"IsDisabled": True}},
                                   "AccessToken": "tok"}, status_code=200)
        return _FakeResponse(status_code=204)

    monkeypatch.setattr(jellyfin_auth.requests, "post", fake_post)
    result = jellyfin_auth.authenticate("alice", "correct-password")
    assert result == {"ok": False, "reason": "disabled"}


def test_authenticate_unreachable_is_not_reported_as_wrong_password(monkeypatch):
    monkeypatch.setattr(jellyfin_auth.config, "JELLYFIN_URL", "http://jellyfin:8096")

    def fake_post(url, **kwargs):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(jellyfin_auth.requests, "post", fake_post)
    result = jellyfin_auth.authenticate("alice", "pw")
    assert result["ok"] is False
    assert result["reason"] == "unreachable"


def test_authenticate_never_persists_the_access_token(monkeypatch):
    """The AccessToken must never appear anywhere in the returned result - see
    the module docstring for why."""
    monkeypatch.setattr(jellyfin_auth.config, "JELLYFIN_URL", "http://jellyfin:8096")

    def fake_post(url, **kwargs):
        if url.endswith("/Users/AuthenticateByName"):
            return _FakeResponse({"User": {"Id": "abc123", "Name": "alice", "Policy": {}},
                                   "AccessToken": "super-secret-token"}, status_code=200)
        return _FakeResponse(status_code=204)

    monkeypatch.setattr(jellyfin_auth.requests, "post", fake_post)
    result = jellyfin_auth.authenticate("alice", "correct-password")
    assert "super-secret-token" not in str(result)


def test_revoke_uses_the_authorization_header_not_x_emby_token(monkeypatch):
    """Jellyfin 12.0 (2026-09) disables legacy authorization by default, which
    means the X-Emby-Token header is silently ignored - the revoke call must
    authenticate via a Token field inside the (always-read) Authorization header
    instead, or it would silently stop working against any current server."""
    monkeypatch.setattr(jellyfin_auth.config, "JELLYFIN_URL", "http://jellyfin:8096")
    logout_calls = []

    def fake_post(url, **kwargs):
        if url.endswith("/Users/AuthenticateByName"):
            return _FakeResponse({"User": {"Id": "abc123", "Name": "alice", "Policy": {}},
                                   "AccessToken": "tok-xyz"}, status_code=200)
        logout_calls.append(kwargs)
        return _FakeResponse(status_code=204)

    monkeypatch.setattr(jellyfin_auth.requests, "post", fake_post)
    jellyfin_auth.authenticate("alice", "correct-password")

    assert len(logout_calls) == 1
    headers = logout_calls[0]["headers"]
    assert "X-Emby-Token" not in headers
    assert 'Token="tok-xyz"' in headers["Authorization"]
    assert headers["Authorization"].startswith("MediaBrowser ")
