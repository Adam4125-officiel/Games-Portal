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


def test_admin_can_delete_a_request(admin_client):
    import db
    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")

    resp = admin_client.post(f"/admin/requests/{rid}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert b"deleted" in resp.data.lower()
    assert db.get_request(rid) is None


def test_admin_delete_404s_for_an_unknown_request(admin_client):
    resp = admin_client.post("/admin/requests/999999/delete")
    assert resp.status_code == 404


def test_admin_delete_requires_login(client):
    import db
    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")

    resp = client.post(f"/admin/requests/{rid}/delete")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]
    assert db.get_request(rid) is not None


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
# Session idle timeout
# ---------------------------------------------------------------------------
def test_admin_session_expires_after_the_idle_timeout(admin_client, monkeypatch):
    real_now = app_module.time.time()
    timeout = app_module._admin_session_timeout_seconds()
    monkeypatch.setattr(app_module.time, "time", lambda: real_now + timeout + 1)

    resp = admin_client.get("/admin/requests", follow_redirects=True)
    assert resp.status_code == 200
    assert b"session expired" in resp.data
    assert b"Password" in resp.data  # rendered the login page, not the requests list


def test_admin_session_stays_alive_within_the_idle_window(admin_client, monkeypatch):
    real_now = app_module.time.time()
    timeout = app_module._admin_session_timeout_seconds()
    monkeypatch.setattr(app_module.time, "time", lambda: real_now + timeout - 5)

    resp = admin_client.get("/admin/requests")
    assert resp.status_code == 200


def test_admin_session_timeout_of_zero_disables_expiry(admin_client, monkeypatch):
    import db
    db.set_setting("admin_session_timeout_hours", "0")
    real_now = app_module.time.time()
    # Well past any reasonable hours-based idle timeout, but still short of the
    # 30-day cookie Max-Age itself - otherwise itsdangerous would reject the
    # signed cookie as simply too old before this app's own hook ever runs,
    # which would make the test pass for the wrong reason.
    monkeypatch.setattr(app_module.time, "time", lambda: real_now + 29 * 24 * 3600)

    resp = admin_client.get("/admin/requests")
    assert resp.status_code == 200


def test_expired_admin_post_redirects_to_login_instead_of_400ing(isolated_db):
    """Uses a client with TESTING left off, so the real CSRF check runs too -
    proving the timeout hook is registered before it, per its own docstring."""
    app_module.app.config["TESTING"] = False
    try:
        with app_module.app.test_client() as c:
            with c.session_transaction() as sess:
                sess["logged_in"] = True
                sess["last_seen"] = 0.0
                sess["csrf_token"] = "test-token"
            resp = c.post("/admin/requests/1/status",
                           data={"csrf_token": "test-token", "status": "approved", "admin_note": ""})
            assert resp.status_code == 302
            assert "/admin/login" in resp.headers["Location"]
    finally:
        app_module.app.config["TESTING"] = True


def test_visitor_session_expires_after_the_idle_timeout(visitor_session, monkeypatch):
    real_now = app_module.time.time()
    timeout = app_module._user_session_timeout_seconds()
    monkeypatch.setattr(app_module.time, "time", lambda: real_now + timeout + 1)

    resp = visitor_session.post("/request", data={"appid": "70", "next": "/"})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_visitor_session_stays_alive_within_the_idle_window(visitor_session, monkeypatch):
    import steam

    real_now = app_module.time.time()
    timeout = app_module._user_session_timeout_seconds()
    monkeypatch.setattr(app_module.time, "time", lambda: real_now + timeout - 5)
    monkeypatch.setattr(steam, "fetch_app_summary", lambda appid: {
        "appid": appid, "name": "Half-Life", "icon_url": "", "short_description": ""})

    resp = visitor_session.post("/request", data={"appid": "70", "next": "/"}, follow_redirects=True)
    assert b"Requested" in resp.data


# ---------------------------------------------------------------------------
# About / self-update
# ---------------------------------------------------------------------------
def test_admin_about_requires_login(client):
    resp = client.get("/admin/about")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_admin_about_renders_before_any_check(admin_client):
    resp = admin_client.get("/admin/about")
    assert resp.status_code == 200
    assert b"Not checked yet" in resp.data


def test_admin_about_check_updates_the_cache_and_flashes_the_result(admin_client, monkeypatch):
    import updater

    def fake_refresh(*a, **k):
        return {"ok": True, "current": "1.0.0", "latest": "2.0.0", "update_available": True}

    monkeypatch.setattr(updater, "refresh_update_cache_if_stale", fake_refresh)
    resp = admin_client.post("/admin/about/check", follow_redirects=True)
    assert b"Update available" in resp.data


def test_admin_about_check_reports_an_error_without_crashing(admin_client, monkeypatch):
    import updater

    def fake_refresh(*a, **k):
        return {"ok": False, "error": "Could not reach GitHub: timeout"}

    monkeypatch.setattr(updater, "refresh_update_cache_if_stale", fake_refresh)
    resp = admin_client.post("/admin/about/check", follow_redirects=True)
    assert b"Could not reach GitHub" in resp.data


def test_admin_about_settings_saves_channel_and_check_enabled(admin_client):
    import updater
    resp = admin_client.post("/admin/about/settings",
                              data={"update_channel": "unstable"}, follow_redirects=True)
    assert resp.status_code == 200
    assert updater.get_channel() == "unstable"
    assert updater.update_check_enabled() is False  # checkbox omitted = unchecked


def test_admin_about_settings_saves_session_timeouts(admin_client):
    import db
    admin_client.post("/admin/about/settings",
                       data={"update_channel": "stable",
                             "admin_session_timeout_hours": "6",
                             "user_session_timeout_hours": "48"})
    assert db.get_setting("admin_session_timeout_hours") == "6"
    assert db.get_setting("user_session_timeout_hours") == "48"


def test_admin_about_settings_clamps_session_timeouts_to_the_cookie_lifetime(admin_client):
    import db
    absurd = app_module.MAX_SESSION_TIMEOUT_HOURS + 1000
    admin_client.post("/admin/about/settings",
                       data={"update_channel": "stable",
                             "admin_session_timeout_hours": str(absurd)})
    assert db.get_setting("admin_session_timeout_hours") == str(app_module.MAX_SESSION_TIMEOUT_HOURS)


def test_admin_about_settings_rejects_an_unknown_channel(admin_client):
    import updater
    admin_client.post("/admin/about/settings", data={"update_channel": "bogus"})
    assert updater.get_channel() == "stable"


def test_admin_update_refuses_when_disabled_by_config(admin_client, monkeypatch):
    import config as config_module
    monkeypatch.setattr(config_module, "ENABLE_INAPP_UPDATE", False)
    resp = admin_client.post("/admin/about/update", follow_redirects=True)
    assert b"disabled" in resp.data.lower()


def test_admin_update_refuses_on_a_git_checkout(admin_client, monkeypatch):
    import config as config_module
    monkeypatch.setattr(config_module, "IS_GIT_CHECKOUT", True)
    resp = admin_client.post("/admin/about/update", follow_redirects=True)
    assert b"git checkout" in resp.data.lower()


def test_admin_update_applies_and_triggers_a_restart(admin_client, monkeypatch):
    import config as config_module
    import updater

    monkeypatch.setattr(config_module, "IS_GIT_CHECKOUT", False)
    restart_calls = []
    monkeypatch.setattr(app_module, "_restart_process", lambda: restart_calls.append(1))
    monkeypatch.setattr(updater, "perform_update",
                        lambda **k: {"applied": True, "current": "1.0.0", "latest": "2.0.0",
                                     "backup": "some-backup"})
    marker_calls = []
    monkeypatch.setattr(updater, "write_pending_marker", lambda *a: marker_calls.append(a))

    resp = admin_client.post("/admin/about/update", follow_redirects=True)
    assert resp.status_code == 200
    assert restart_calls == [1]
    assert marker_calls == [("some-backup", "2.0.0")]


def test_admin_update_reports_a_failed_update_without_restarting(admin_client, monkeypatch):
    import config as config_module
    import updater

    monkeypatch.setattr(config_module, "IS_GIT_CHECKOUT", False)
    restart_calls = []
    monkeypatch.setattr(app_module, "_restart_process", lambda: restart_calls.append(1))

    def fake_perform_update(**k):
        raise updater.UpdateError("Could not reach GitHub")

    monkeypatch.setattr(updater, "perform_update", fake_perform_update)
    resp = admin_client.post("/admin/about/update", follow_redirects=True)
    assert b"Update failed" in resp.data
    assert restart_calls == []


def test_admin_update_no_op_when_already_up_to_date_does_not_restart(admin_client, monkeypatch):
    import config as config_module
    import updater

    monkeypatch.setattr(config_module, "IS_GIT_CHECKOUT", False)
    restart_calls = []
    monkeypatch.setattr(app_module, "_restart_process", lambda: restart_calls.append(1))
    monkeypatch.setattr(updater, "perform_update",
                        lambda **k: {"applied": False, "reason": "already up to date"})
    resp = admin_client.post("/admin/about/update", follow_redirects=True)
    assert b"Nothing to update" in resp.data
    assert restart_calls == []
