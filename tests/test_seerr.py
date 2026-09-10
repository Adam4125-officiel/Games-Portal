import pytest

import seerr


class FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._json


def test_is_enabled_requires_both_url_and_key(monkeypatch):
    monkeypatch.setattr(seerr.config, "SEERR_URL", "")
    monkeypatch.setattr(seerr.config, "SEERR_API_KEY", "")
    assert seerr.is_enabled() is False
    monkeypatch.setattr(seerr.config, "SEERR_URL", "http://seerr.local")
    assert seerr.is_enabled() is False
    monkeypatch.setattr(seerr.config, "SEERR_API_KEY", "key123")
    assert seerr.is_enabled() is True


def test_seerr_email_drops_non_email_values():
    assert seerr._seerr_email("not-an-email") == ""
    assert seerr._seerr_email("real@example.com") == "real@example.com"
    assert seerr._seerr_email(None) == ""


def test_first_discord_id_handles_list_and_singular_forms():
    assert seerr._first_discord_id({"discordIds": ["123", "456"]}) == "123"
    assert seerr._first_discord_id({"discordIds": []}) == ""
    assert seerr._first_discord_id({"discordId": "789"}) == "789"
    assert seerr._first_discord_id({}) == ""


def test_fetch_users_only_returns_jellyfin_linked_accounts(monkeypatch):
    monkeypatch.setattr(seerr.config, "SEERR_URL", "http://seerr.local")
    monkeypatch.setattr(seerr.config, "SEERR_API_KEY", "key123")

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/api/v1/user"):
            return FakeResponse({"results": [
                {"id": 1, "displayName": "Alice", "email": "alice@example.com",
                 "jellyfinUserId": "jf-1"},
                {"id": 2, "displayName": "NoLink", "email": "nolink@example.com",
                 "jellyfinUserId": None},
            ]})
        return FakeResponse({"discordIds": ["999"]})

    monkeypatch.setattr(seerr.requests, "get", fake_get)
    users = seerr.fetch_users()
    assert len(users) == 1
    assert users[0]["jellyfin_user_id"] == "jf-1"
    assert users[0]["email"] == "alice@example.com"
    assert users[0]["discord_id"] == "999"


def test_fetch_users_drops_username_masquerading_as_email(monkeypatch):
    monkeypatch.setattr(seerr.config, "SEERR_URL", "http://seerr.local")
    monkeypatch.setattr(seerr.config, "SEERR_API_KEY", "key123")

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/api/v1/user"):
            return FakeResponse({"results": [
                {"id": 1, "displayName": "Alice", "email": "just_a_username",
                 "jellyfinUserId": "jf-1"},
            ]})
        return FakeResponse({})

    monkeypatch.setattr(seerr.requests, "get", fake_get)
    users = seerr.fetch_users()
    assert users[0]["email"] == ""


def test_fetch_users_survives_a_failed_notification_settings_call(monkeypatch):
    monkeypatch.setattr(seerr.config, "SEERR_URL", "http://seerr.local")
    monkeypatch.setattr(seerr.config, "SEERR_API_KEY", "key123")

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/api/v1/user"):
            return FakeResponse({"results": [
                {"id": 1, "displayName": "Alice", "email": "alice@example.com", "jellyfinUserId": "jf-1"},
            ]})
        raise seerr.requests.Timeout("slow")

    monkeypatch.setattr(seerr.requests, "get", fake_get)
    users = seerr.fetch_users()
    assert len(users) == 1
    assert users[0]["email"] == "alice@example.com"
    assert users[0]["discord_id"] == ""


def test_sync_contacts_requires_configuration(isolated_db, monkeypatch):
    monkeypatch.setattr(seerr.config, "SEERR_URL", "")
    monkeypatch.setattr(seerr.config, "SEERR_API_KEY", "")
    with pytest.raises(seerr.SyncError):
        seerr.sync_contacts()


def test_sync_contacts_populates_the_database(isolated_db, monkeypatch):
    import db
    monkeypatch.setattr(seerr.config, "SEERR_URL", "http://seerr.local")
    monkeypatch.setattr(seerr.config, "SEERR_API_KEY", "key123")
    monkeypatch.setattr(seerr, "fetch_users", lambda: [
        {"id": "1", "jellyfin_user_id": "jf-1", "display_name": "Alice",
         "email": "alice@example.com", "discord_id": "999"},
    ])
    total, with_contact = seerr.sync_contacts()
    assert total == 1
    assert with_contact == 1
    contact = db.get_seerr_contact("jf-1")
    assert contact["email"] == "alice@example.com"


def test_sync_contacts_leaves_previous_data_on_network_failure(isolated_db, monkeypatch):
    import db
    db.replace_seerr_contacts([{"jellyfin_user_id": "jf-1", "seerr_user_id": "1",
                               "display_name": "Alice", "email": "alice@example.com",
                               "discord_id": ""}])
    monkeypatch.setattr(seerr.config, "SEERR_URL", "http://seerr.local")
    monkeypatch.setattr(seerr.config, "SEERR_API_KEY", "key123")

    def boom():
        raise seerr.requests.ConnectionError("down")

    monkeypatch.setattr(seerr, "fetch_users", boom)
    with pytest.raises(seerr.SyncError):
        seerr.sync_contacts()

    assert db.get_seerr_contact("jf-1")["email"] == "alice@example.com"
