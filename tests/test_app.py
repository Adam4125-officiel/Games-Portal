import app as app_module


def test_index_loads(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Games Portal" in resp.data


def test_search_shows_results(client, monkeypatch):
    import steam

    def fake_search(term):
        return [{"appid": 70, "name": "Half-Life", "icon_url": "http://img/70.jpg", "short_description": ""}]

    def fake_enrich(results):
        for r in results:
            r["short_description"] = "A classic FPS."
        return results

    monkeypatch.setattr(steam, "search", fake_search)
    monkeypatch.setattr(steam, "enrich_with_descriptions", fake_enrich)

    resp = client.get("/?q=half-life")
    assert resp.status_code == 200
    assert b"Half-Life" in resp.data
    assert b"A classic FPS." in resp.data


def test_search_degrades_gracefully_on_steam_failure(client, monkeypatch):
    import steam

    def fake_search(term):
        raise Exception("Steam is down")

    monkeypatch.setattr(steam, "search", fake_search)
    resp = client.get("/?q=anything")
    assert resp.status_code == 200
    assert b"unavailable" in resp.data


def test_request_requires_visitor_login(client):
    resp = client.post("/request", data={"appid": "70"})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_request_creates_a_row_when_signed_in(visitor_session, monkeypatch):
    import steam

    def fake_summary(appid):
        return {"appid": appid, "name": "Half-Life", "icon_url": "http://img/70.jpg",
                "short_description": "A classic."}

    monkeypatch.setattr(steam, "fetch_app_summary", fake_summary)

    resp = visitor_session.post("/request", data={"appid": "70", "next": "/"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Requested" in resp.data

    import db
    rows = db.list_requests()
    assert len(rows) == 1
    assert rows[0]["name"] == "Half-Life"
    assert rows[0]["requested_by_name"] == "alice"


def test_request_rejects_a_duplicate(visitor_session, monkeypatch):
    import db
    import steam

    db.create_request(70, "Half-Life", "", "", "someone-else", "Bob")
    monkeypatch.setattr(steam, "fetch_app_summary", lambda appid: {"appid": appid, "name": "Half-Life",
                                                                     "icon_url": "", "short_description": ""})

    resp = visitor_session.post("/request", data={"appid": "70", "next": "/"}, follow_redirects=True)
    assert b"already been requested" in resp.data
    assert len(db.list_requests()) == 1


def test_request_rejects_unresolvable_appid(visitor_session, monkeypatch):
    import steam
    monkeypatch.setattr(steam, "fetch_app_summary", lambda appid: None)

    resp = visitor_session.post("/request", data={"appid": "999999999", "next": "/"}, follow_redirects=True)
    assert b"Could not look up" in resp.data


def test_visitor_login_rejects_bad_credentials(client, monkeypatch):
    import jellyfin_auth
    monkeypatch.setattr(jellyfin_auth, "is_enabled", lambda: True)
    monkeypatch.setattr(jellyfin_auth, "authenticate", lambda u, p: {"ok": False, "reason": "invalid"})

    resp = client.post("/login", data={"username": "alice", "password": "wrong"}, follow_redirects=True)
    assert b"Incorrect username or password" in resp.data


def test_login_route_404s_when_jellyfin_not_configured(client, monkeypatch):
    import jellyfin_auth
    monkeypatch.setattr(jellyfin_auth, "is_enabled", lambda: False)
    resp = client.get("/login")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
def test_admin_requests_requires_login(client):
    resp = client.get("/admin/requests")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_admin_first_run_sets_password(client):
    resp = client.post("/admin/login", data={"password": "adminpass123", "confirm": "adminpass123"},
                        follow_redirects=True)
    assert resp.status_code == 200
    assert b"Requests" in resp.data

    import db
    from werkzeug.security import check_password_hash
    stored = db.get_setting("admin_password_hash")
    assert check_password_hash(stored, "adminpass123")


def test_admin_login_rejects_wrong_password(admin_client):
    # admin_client already completed first-run (password is set); a genuinely
    # separate client - own cookie jar, no session - now hits the ordinary
    # (non-first-run) login form, where a wrong password must be rejected.
    with app_module.app.test_client() as fresh_client:
        resp = fresh_client.post("/admin/login", data={"password": "nope"}, follow_redirects=True)
        assert b"Incorrect password" in resp.data


def test_admin_can_update_request_status(admin_client):
    import db
    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")

    resp = admin_client.post(f"/admin/requests/{rid}/status",
                              data={"status": "approved", "admin_note": "Go ahead"}, follow_redirects=True)
    assert resp.status_code == 200

    row = db.get_request(rid)
    assert row["status"] == "approved"
    assert row["admin_note"] == "Go ahead"


def test_admin_update_rejects_unknown_status(admin_client):
    import db
    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")

    admin_client.post(f"/admin/requests/{rid}/status", data={"status": "bogus", "admin_note": ""})
    row = db.get_request(rid)
    assert row["status"] == "pending"


def test_csrf_protection_rejects_missing_token(isolated_db):
    """Uses a client with TESTING left off, so the real CSRF check runs."""
    app_module.app.config["TESTING"] = False
    try:
        with app_module.app.test_client() as c:
            resp = c.post("/admin/login", data={"password": "x", "confirm": "x"})
            assert resp.status_code == 400
    finally:
        app_module.app.config["TESTING"] = True


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def test_new_request_notifies_admin(visitor_session, monkeypatch):
    import notifications
    import steam

    monkeypatch.setattr(steam, "fetch_app_summary",
                        lambda appid: {"appid": appid, "name": "Half-Life", "icon_url": "",
                                       "short_description": ""})
    calls = []
    monkeypatch.setattr(notifications, "notify_admin", lambda title, message: calls.append((title, message)))

    visitor_session.post("/request", data={"appid": "70", "next": "/"})
    assert len(calls) == 1
    assert "Half-Life" in calls[0][0]


def test_status_change_emails_the_visitor_if_a_seerr_contact_exists(admin_client, monkeypatch):
    import db
    import notifications

    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    db.replace_seerr_contacts([{"jellyfin_user_id": "jf-1", "seerr_user_id": "1",
                               "display_name": "Alice", "email": "alice@example.com",
                               "discord_id": ""}])
    calls = []
    monkeypatch.setattr(notifications, "send_email",
                        lambda subject, body, recipients: calls.append((subject, body, recipients)))

    admin_client.post(f"/admin/requests/{rid}/status", data={"status": "approved", "admin_note": ""})
    assert len(calls) == 1
    assert calls[0][2] == ["alice@example.com"]


def test_status_change_without_a_seerr_contact_sends_nothing(admin_client, monkeypatch):
    import db
    import notifications

    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    calls = []
    monkeypatch.setattr(notifications, "send_email",
                        lambda *a, **k: calls.append(1))

    admin_client.post(f"/admin/requests/{rid}/status", data={"status": "approved", "admin_note": ""})
    assert calls == []


def test_status_change_to_the_same_status_does_not_notify(admin_client, monkeypatch):
    import db
    import notifications

    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    db.replace_seerr_contacts([{"jellyfin_user_id": "jf-1", "seerr_user_id": "1",
                               "display_name": "Alice", "email": "alice@example.com",
                               "discord_id": ""}])
    calls = []
    monkeypatch.setattr(notifications, "send_email", lambda *a, **k: calls.append(1))

    admin_client.post(f"/admin/requests/{rid}/status", data={"status": "pending", "admin_note": ""})
    assert calls == []


def test_admin_notifications_requires_login(client):
    resp = client.get("/admin/notifications")
    assert resp.status_code == 302


def test_admin_notifications_page_loads(admin_client):
    resp = admin_client.get("/admin/notifications")
    assert resp.status_code == 200
    assert b"Notifications" in resp.data


def test_admin_notifications_settings_saves_recipients(admin_client):
    import db
    admin_client.post("/admin/notifications/settings", data={"recipients": "a@example.com, b@example.com"})
    assert db.get_setting("admin_notify_email") == "a@example.com, b@example.com"


def test_admin_notifications_test_requires_a_configured_channel(admin_client, monkeypatch):
    import notifications
    monkeypatch.setattr(notifications, "discord_configured", lambda: False)
    monkeypatch.setattr(notifications, "email_configured", lambda: False)
    resp = admin_client.post("/admin/notifications/test", follow_redirects=True)
    assert b"No notification channel" in resp.data


def test_admin_seerr_sync_now_requires_configuration(admin_client):
    resp = admin_client.post("/admin/notifications/seerr-sync", follow_redirects=True)
    assert b"isn&#39;t configured" in resp.data or b"isn't configured" in resp.data


def test_admin_seerr_sync_now_reports_success(admin_client, monkeypatch):
    import seerr
    monkeypatch.setattr(seerr, "is_enabled", lambda: True)
    monkeypatch.setattr(seerr, "sync_contacts", lambda: (3, 2))
    resp = admin_client.post("/admin/notifications/seerr-sync", follow_redirects=True)
    assert b"Synced 3" in resp.data
