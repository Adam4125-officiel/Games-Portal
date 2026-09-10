import pytest

import app as app_module
import db


def _reset_module_state():
    """Module-level globals that would otherwise leak between tests - a rate
    limit or lockout counter left over from a previous test makes the next one
    fail for the wrong reason."""
    app_module._login_state["failures"] = 0
    app_module._login_state["locked_until"] = 0.0
    app_module._user_login_state["failures"] = 0
    app_module._user_login_state["locked_until"] = 0.0


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Points db.py at a fresh, empty SQLite file per test instead of the real
    instance/portal.db."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test_portal.db"))
    db.init_db()
    _reset_module_state()
    return db.DB_PATH


@pytest.fixture
def client(isolated_db):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def admin_client(client):
    """A client with an admin session already established (sets the
    first-run password)."""
    client.post("/admin/login", data={"password": "adminpass123", "confirm": "adminpass123"})
    return client


@pytest.fixture
def visitor_session(client, monkeypatch):
    """Signs the test client in as a visitor without a real Jellyfin server -
    mocks jellyfin_auth.authenticate at the point app.py calls it."""
    import jellyfin_auth

    def fake_authenticate(username, password):
        return {"ok": True, "user": {"id": "jf-user-1", "name": username}}

    monkeypatch.setattr(jellyfin_auth, "authenticate", fake_authenticate)
    monkeypatch.setattr(jellyfin_auth, "is_enabled", lambda: True)
    client.post("/login", data={"username": "alice", "password": "hunter2"})
    return client
