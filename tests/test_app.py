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
# Games-folder scanner
# ---------------------------------------------------------------------------
def test_admin_scanner_requires_login(client):
    resp = client.get("/admin/scanner")
    assert resp.status_code == 302


def test_admin_scanner_disabled_state(admin_client, monkeypatch):
    import scanner
    monkeypatch.setattr(scanner, "is_enabled", lambda: False)
    resp = admin_client.get("/admin/scanner")
    assert resp.status_code == 200
    assert b"Disabled" in resp.data


def test_admin_scanner_scan_now_reports_a_summary(admin_client, monkeypatch):
    import scanner
    monkeypatch.setattr(scanner, "scan_once",
                        lambda: {"ok": True, "folders_scanned": 5, "exact_matches": 2, "suggestions": 1})
    resp = admin_client.post("/admin/scanner/scan", follow_redirects=True)
    assert b"Scanned 5 folder" in resp.data


def test_admin_scanner_scan_now_reports_when_disabled(admin_client, monkeypatch):
    import scanner
    monkeypatch.setattr(scanner, "scan_once", lambda: {"ok": False, "error": "No games folder configured."})
    resp = admin_client.post("/admin/scanner/scan", follow_redirects=True)
    assert b"No games folder configured" in resp.data


def test_admin_scanner_confirm_requires_login(client):
    resp = client.post("/admin/scanner/matches/1/confirm")
    assert resp.status_code == 302


def test_admin_scanner_confirm_success(admin_client, monkeypatch):
    import scanner
    monkeypatch.setattr(scanner, "confirm_match", lambda match_id: "Half Life 2 [220]")
    resp = admin_client.post("/admin/scanner/matches/1/confirm", follow_redirects=True)
    assert b"Confirmed" in resp.data
    assert b"Half Life 2 [220]" in resp.data


def test_admin_scanner_confirm_reports_scan_error(admin_client, monkeypatch):
    import scanner

    def boom(match_id):
        raise scanner.ScanError("The folder no longer exists.")

    monkeypatch.setattr(scanner, "confirm_match", boom)
    resp = admin_client.post("/admin/scanner/matches/1/confirm", follow_redirects=True)
    assert b"Could not confirm" in resp.data


def test_admin_scanner_reject_success(admin_client, monkeypatch):
    import scanner
    calls = []
    monkeypatch.setattr(scanner, "reject_match", lambda match_id: calls.append(match_id))
    resp = admin_client.post("/admin/scanner/matches/1/reject", follow_redirects=True)
    assert calls == [1]
    assert b"dismissed" in resp.data


def test_search_shows_installed_badge(client, monkeypatch):
    import db
    import steam

    monkeypatch.setattr(steam, "search",
                        lambda term: [{"appid": 220, "name": "Half-Life 2", "icon_url": "",
                                       "short_description": ""}])
    monkeypatch.setattr(steam, "enrich_with_descriptions", lambda results: results)
    db.upsert_installed_game(220, "Half-Life 2 [220]", "/games/Half-Life 2 [220]")

    resp = client.get("/?q=half-life")
    assert b"installed" in resp.data


def test_scanner_end_to_end_via_admin_routes(admin_client, tmp_path, monkeypatch):
    """A slightly bigger integration test: real scanner.scan_once() (not
    mocked) against a real temp folder, through the real admin routes."""
    import db
    import scanner

    games_dir = tmp_path / "games"
    games_dir.mkdir()
    monkeypatch.setattr(scanner.config, "GAMES_FOLDER", str(games_dir))

    rid = db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    (games_dir / "Half Life 2").mkdir()

    resp = admin_client.post("/admin/scanner/scan", follow_redirects=True)
    assert b"1 suggestion" in resp.data

    resp = admin_client.get("/admin/scanner")
    assert b"Half Life 2" in resp.data
    assert b"Half-Life 2" in resp.data

    match = db.list_pending_scan_matches()[0]
    resp = admin_client.post(f"/admin/scanner/matches/{match['id']}/confirm", follow_redirects=True)
    assert b"Confirmed" in resp.data
    assert db.get_request(rid)["status"] == "done"
    assert (games_dir / "Half Life 2 [220]").is_dir()
