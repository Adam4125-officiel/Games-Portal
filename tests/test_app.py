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


def test_admin_backup_restore_form_carries_a_working_csrf_token(isolated_db):
    """Regression test: every POST form under /admin/ must render a real
    csrf_token hidden field, or the real (non-TESTING) CSRF check 400s every
    submission - caught live via Playwright against a real browser (unit
    tests with TESTING=True never exercise the real check at all), not by any
    test until this one. Proves the *rendered form*, not just the route in
    isolation, actually carries a token that round-trips."""
    import io
    import re
    app_module.app.config["TESTING"] = False
    try:
        with app_module.app.test_client() as c:
            # First-run admin login also needs a real token, taken from the
            # rendered login page itself.
            login_page = c.get("/admin/login").data.decode()
            token = re.search(r'name="csrf_token" value="([^"]+)"', login_page).group(1)
            c.post("/admin/login", data={"password": "adminpass123", "confirm": "adminpass123",
                                         "csrf_token": token})

            backup_page = c.get("/admin/backup").data.decode()
            assert 'name="csrf_token"' in backup_page
            token = re.search(r'name="csrf_token" value="([^"]+)"', backup_page).group(1)

            resp = c.post("/admin/backup/restore",
                          data={"csrf_token": token, "backup": (io.BytesIO(b"not a database"), "x.db")},
                          content_type="multipart/form-data", follow_redirects=True)
            # A 400 here means the CSRF check itself rejected the request (the
            # bug); "Restore refused" after a redirect means CSRF passed and
            # normal validation correctly rejected the bogus upload instead.
            assert resp.status_code == 200
            assert b"Restore refused" in resp.data
    finally:
        app_module.app.config["TESTING"] = True


# ---------------------------------------------------------------------------
# Backup and restore
# ---------------------------------------------------------------------------
def test_admin_backup_requires_login(client):
    resp = client.get("/admin/backup")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_admin_backup_download_requires_login(client):
    resp = client.get("/admin/backup/download")
    assert resp.status_code == 302


def test_admin_backup_download_returns_a_zip(admin_client):
    import db
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    resp = admin_client.get("/admin/backup/download")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"

    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        assert zf.namelist() == ["portal.db"]


def test_admin_restore_requires_login(client):
    resp = client.post("/admin/backup/restore", data={})
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_admin_restore_without_a_file_is_refused(admin_client):
    resp = admin_client.post("/admin/backup/restore", data={}, follow_redirects=True)
    assert b"Choose a backup file" in resp.data


def test_admin_restore_rejects_a_non_database_upload(admin_client):
    import io
    data = {"backup": (io.BytesIO(b"not a database"), "backup.db")}
    resp = admin_client.post("/admin/backup/restore", data=data,
                             content_type="multipart/form-data", follow_redirects=True)
    assert b"Restore refused" in resp.data


def test_admin_restore_replaces_the_database_and_snapshots_the_old_one(admin_client):
    import io
    import sqlite3

    import db

    # Build a real, valid backup of the (currently empty-of-requests) live db,
    # then add a distinguishing row to the *current* db so we can tell after
    # restoring whether it actually got replaced.
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    admin_client.get("/admin/backup/download")  # sanity: download still works pre-restore

    other_db_path = db.DB_PATH + ".other"
    conn = sqlite3.connect(other_db_path)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, steam_appid INTEGER, name TEXT, "
                "icon_url TEXT, short_description TEXT, requested_by_id TEXT, requested_by_name TEXT, "
                "status TEXT, admin_note TEXT, created_at TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO requests (id, steam_appid, name, icon_url, short_description, "
                "requested_by_id, requested_by_name, status, admin_note, created_at, updated_at) "
                "VALUES (1, 999, 'From The Restored DB', '', '', 'x', 'x', 'pending', '', 'x', 'x')")
    conn.commit()
    conn.close()

    with open(other_db_path, "rb") as f:
        data = {"backup": (io.BytesIO(f.read()), "other.db")}
    resp = admin_client.post("/admin/backup/restore", data=data,
                             content_type="multipart/form-data", follow_redirects=True)
    assert b"Database restored" in resp.data

    rows = db.list_requests()
    assert len(rows) == 1
    assert rows[0]["name"] == "From The Restored DB"

    # The pre-restore snapshot exists and still has the original data.
    import app as app_mod
    backups = app_mod._list_db_safety_backups()
    assert len(backups) == 1
    snap_conn = sqlite3.connect(backups[0]["path"])
    snap_conn.row_factory = sqlite3.Row
    snap_row = snap_conn.execute("SELECT * FROM requests").fetchone()
    snap_conn.close()
    assert snap_row["name"] == "Half-Life"
