import app as app_module


def test_index_loads(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Games Portal" in resp.data


def test_index_footer_links_to_the_repo(client):
    import config
    import updater
    resp = client.get("/")
    assert b"games-portal" in resp.data
    assert config.VERSION_DISPLAY.encode() in resp.data
    assert f'href="{updater.REPO_URL}"'.encode() in resp.data
    assert b"Check it out on GitHub" in resp.data


def test_collections_footer_links_to_the_repo(client):
    import updater
    resp = client.get("/collections")
    assert f'href="{updater.REPO_URL}"'.encode() in resp.data
    assert b"Check it out on GitHub" in resp.data


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


def test_search_results_link_out_to_their_steam_store_page(client, monkeypatch):
    import steam

    def fake_search(term):
        return [{"appid": 70, "name": "Half-Life", "icon_url": "", "short_description": ""}]

    monkeypatch.setattr(steam, "search", fake_search)
    monkeypatch.setattr(steam, "enrich_with_descriptions", lambda results: results)

    resp = client.get("/?q=half-life")
    assert b'href="https://store.steampowered.com/app/70"' in resp.data
    assert b"Check out on Steam" in resp.data


def test_search_degrades_gracefully_on_steam_failure(client, monkeypatch):
    import steam

    def fake_search(term):
        raise Exception("Steam is down")

    monkeypatch.setattr(steam, "search", fake_search)
    resp = client.get("/?q=anything")
    assert resp.status_code == 200


def test_search_shows_available_badge_instead_of_a_request_button(client, monkeypatch):
    import db
    import steam

    def fake_search(term):
        return [{"appid": 220, "name": "Half-Life 2", "icon_url": "", "short_description": ""}]

    monkeypatch.setattr(steam, "search", fake_search)
    monkeypatch.setattr(steam, "enrich_with_descriptions", lambda results: results)
    db.add_games_folder("/games")
    db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "matched", steam_appid=220,
                              name="Half-Life 2")

    resp = client.get("/?q=half-life")
    assert resp.status_code == 200
    assert b'class="badge available literal"' in resp.data
    assert b'name="appid" value="220"' not in resp.data  # no Request form rendered


def test_search_shows_available_on_the_folders_label(client, monkeypatch):
    import db
    import steam

    def fake_search(term):
        return [{"appid": 220, "name": "Half-Life 2", "icon_url": "", "short_description": ""}]

    monkeypatch.setattr(steam, "search", fake_search)
    monkeypatch.setattr(steam, "enrich_with_descriptions", lambda results: results)
    db.add_games_folder("/games", label="SSD")
    db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "matched", steam_appid=220,
                              name="Half-Life 2")

    resp = client.get("/?q=half-life")
    assert b"Available on SSD" in resp.data


def test_search_shows_a_blacklisted_badge_and_no_request_button(visitor_session, monkeypatch):
    import db
    import steam

    def fake_search(term):
        return [{"appid": 220, "name": "Half-Life 2", "icon_url": "", "short_description": ""}]

    monkeypatch.setattr(steam, "search", fake_search)
    monkeypatch.setattr(steam, "enrich_with_descriptions", lambda results: results)
    db.add_to_blacklist(220, "Half-Life 2", "Nope")

    resp = visitor_session.get("/?q=half-life")
    assert b'class="badge blacklisted"' in resp.data
    assert b"Nope" in resp.data
    assert b'name="appid" value="220"' not in resp.data


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


def test_request_refuses_an_appid_the_scanner_has_already_matched(visitor_session, monkeypatch):
    import db
    monkeypatch.setattr(app_module.scanner, "matched_appids", lambda: {70})

    resp = visitor_session.post("/request", data={"appid": "70", "next": "/"}, follow_redirects=True)
    assert b"already installed" in resp.data
    assert db.list_requests() == []


def test_request_rejects_unresolvable_appid(visitor_session, monkeypatch):
    import steam
    monkeypatch.setattr(steam, "fetch_app_summary", lambda appid: None)

    resp = visitor_session.post("/request", data={"appid": "999999999", "next": "/"}, follow_redirects=True)
    assert b"Could not look up" in resp.data


def test_request_refuses_a_blacklisted_appid(visitor_session):
    import db
    db.add_to_blacklist(220, "Half-Life 2", "Nope")

    resp = visitor_session.post("/request", data={"appid": "220", "next": "/"}, follow_redirects=True)
    assert b"can&#39;t be requested" in resp.data or b"can't be requested" in resp.data
    assert db.list_requests() == []


def test_request_refuses_once_the_global_limit_is_reached(visitor_session, monkeypatch):
    import db
    import steam
    db.set_global_request_limit("daily", 1)
    monkeypatch.setattr(steam, "fetch_app_summary",
                         lambda appid: {"appid": appid, "name": "Game", "icon_url": "", "short_description": ""})

    first = visitor_session.post("/request", data={"appid": "70", "next": "/"}, follow_redirects=True)
    assert b"Requested" in first.data

    second = visitor_session.post("/request", data={"appid": "220", "next": "/"}, follow_redirects=True)
    assert b"reached your request limit" in second.data
    assert len(db.list_requests()) == 1


def test_a_per_user_override_takes_priority_over_the_global_limit(visitor_session, monkeypatch):
    import db
    import steam
    db.set_global_request_limit("daily", 1)
    db.set_user_request_limit("jf-user-1", "alice", "daily", 5)
    monkeypatch.setattr(steam, "fetch_app_summary",
                         lambda appid: {"appid": appid, "name": "Game", "icon_url": "", "short_description": ""})

    for appid in (70, 220, 620):
        resp = visitor_session.post("/request", data={"appid": str(appid), "next": "/"}, follow_redirects=True)
        assert b"Requested" in resp.data
    assert len(db.list_requests()) == 3


def test_a_rejected_request_does_not_count_against_the_limit(visitor_session, monkeypatch):
    import db
    import steam
    db.set_global_request_limit("daily", 1)
    monkeypatch.setattr(steam, "fetch_app_summary",
                         lambda appid: {"appid": appid, "name": "Game", "icon_url": "", "short_description": ""})

    visitor_session.post("/request", data={"appid": "70", "next": "/"})
    db.update_request_status(db.list_requests()[0]["id"], "rejected", "")

    resp = visitor_session.post("/request", data={"appid": "220", "next": "/"}, follow_redirects=True)
    assert b"Requested" in resp.data


# ---------------------------------------------------------------------------
# Collections (public, read-only, no sign-in - same as search itself) and the
# "Recently added" strip on the search page
# ---------------------------------------------------------------------------
def test_collections_requires_no_login(client):
    resp = client.get("/collections")
    assert resp.status_code == 200
    assert b"Nothing recognized as installed yet" in resp.data


def test_collections_shows_matched_games_with_pictures(client):
    import db
    db.add_games_folder("/games")
    db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "matched", steam_appid=220,
                              name="Half-Life 2", icon_url="http://img/220.jpg",
                              short_description="A classic.")
    resp = client.get("/collections")
    assert resp.status_code == 200
    assert b"Half-Life 2" in resp.data
    assert b'src="http://img/220.jpg"' in resp.data
    assert b'class="badge available literal"' in resp.data


def test_collections_games_link_out_to_their_steam_store_page(client):
    import db
    db.add_games_folder("/games")
    db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "matched", steam_appid=220,
                              name="Half-Life 2")
    resp = client.get("/collections")
    assert b'href="https://store.steampowered.com/app/220"' in resp.data
    assert b"Check out on Steam" in resp.data


def test_collections_falls_back_to_the_folder_name_when_steam_details_are_missing(client):
    import db
    db.add_games_folder("/games")
    db.upsert_scanned_folder("/games", "Some Game {steamapp-999}", "matched", steam_appid=999)
    resp = client.get("/collections")
    assert b"Some Game" in resp.data


def test_collections_groups_games_into_genre_rows(client):
    import db
    db.add_games_folder("/games")
    db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "matched", steam_appid=220,
                              name="Half-Life 2", genres="Action,Adventure")
    db.upsert_scanned_folder("/games", "Stardew Valley {steamapp-413150}", "matched", steam_appid=413150,
                              name="Stardew Valley", genres="")

    resp = client.get("/collections")
    assert resp.status_code == 200
    assert b"Action" in resp.data
    assert b"Adventure" in resp.data
    assert b"Uncategorized" in resp.data
    # A game with two genres appears once per genre row it belongs to.
    assert resp.data.count(b'rail-card__title">Half-Life 2') == 2


def test_collections_shows_available_on_the_folders_label(client):
    import db
    db.add_games_folder("/games", label="SSD")
    db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "matched", steam_appid=220,
                              name="Half-Life 2")
    resp = client.get("/collections")
    assert b"Available on SSD" in resp.data


def test_index_shows_recently_added_only_without_an_active_search(client, monkeypatch):
    import db
    import steam
    db.add_games_folder("/games")
    db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "matched", steam_appid=220,
                              name="Half-Life 2", matched_at=db.now_iso())

    resp = client.get("/")
    assert b"Recently added" in resp.data
    assert b"Half-Life 2" in resp.data

    monkeypatch.setattr(steam, "search", lambda term: [])
    resp = client.get("/?q=portal")
    assert b"Recently added" not in resp.data


def test_recently_added_games_link_out_to_their_steam_store_page(client):
    import db
    db.add_games_folder("/games")
    db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "matched", steam_appid=220,
                              name="Half-Life 2", matched_at=db.now_iso())
    resp = client.get("/")
    assert b'href="https://store.steampowered.com/app/220"' in resp.data
    assert b"Check out on Steam" in resp.data


def test_index_respects_the_admin_configured_recently_added_count(client):
    import db
    import scanner
    db.add_games_folder("/games")
    scanner.set_recently_added_count(1)
    for appid, name in ((220, "Half-Life 2"), (620, "Portal 2")):
        db.upsert_scanned_folder("/games", f"{name} {{steamapp-{appid}}}", "matched", steam_appid=appid,
                                  name=name, matched_at=db.now_iso())

    resp = client.get("/")
    assert resp.data.count(b"rail-card__title") == 1


# ---------------------------------------------------------------------------
# "My requests" (Jellyfin used only for login/logout - see the module note
# on jellyfin_auth.py; this page is a pure view over this app's own data)
# ---------------------------------------------------------------------------
def test_my_requests_requires_sign_in(client):
    resp = client.get("/my-requests")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_my_requests_shows_only_the_signed_in_visitors_own_requests(visitor_session):
    """The test that actually proves the "never let anyone browse another
    visitor's requests" constraint - two different requesters, only one shown."""
    import db
    db.create_request(70, "Half-Life", "", "", "jf-user-1", "alice")  # matches visitor_session's id
    db.create_request(220, "Half-Life 2", "", "", "someone-else", "Bob")

    resp = visitor_session.get("/my-requests")
    assert resp.status_code == 200
    assert b"Half-Life 2" not in resp.data
    assert b"Half-Life" in resp.data


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


def test_visitor_signin_button_shown_on_public_pages(client, monkeypatch):
    import jellyfin_auth
    monkeypatch.setattr(jellyfin_auth, "is_enabled", lambda: True)
    resp = client.get("/")
    assert b">Sign in<" in resp.data


def test_visitor_signin_button_hidden_in_admin(admin_client, monkeypatch):
    """The visitor sign-in bar (and, if a Jellyfin session happened to also be
    active, the visitor's own user chip) has no place on the admin panel - the
    admin already has its own separate "Sign out" in the admin nav."""
    import jellyfin_auth
    monkeypatch.setattr(jellyfin_auth, "is_enabled", lambda: True)
    resp = admin_client.get("/admin/requests")
    assert resp.status_code == 200
    assert b">Sign in<" not in resp.data


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


# ---------------------------------------------------------------------------
# Games-folder scanner admin routes - always reachable, even unconfigured
# (see app.py's admin_scanner), so there's somewhere to actually add a
# folder from. Settings (which folders, scan interval, fuzzy threshold) are
# DB-backed, edited from this same page - see scanner.py.
# ---------------------------------------------------------------------------
def test_admin_scanner_page_is_reachable_with_nothing_configured(admin_client):
    resp = admin_client.get("/admin/scanner")
    assert resp.status_code == 200
    assert b"No games folders configured" in resp.data


def test_admin_scanner_scan_with_nothing_configured_flashes_a_hint_not_a_404(admin_client):
    resp = admin_client.post("/admin/scanner/scan", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Add at least one games folder" in resp.data


def test_admin_scanner_requires_login(client):
    resp = client.get("/admin/scanner")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_admin_scanner_nav_link_is_always_shown(admin_client):
    """Confirms the nav entry doesn't depend on the scanner being configured
    - it must always be there for the admin to actually reach the settings
    that configure it in the first place."""
    resp = admin_client.get("/admin/requests")
    assert b"Folder Scanner" in resp.data


def test_admin_scanner_page_lists_pending_and_unmatched(admin_client, tmp_path):
    import db
    db.add_games_folder(str(tmp_path))
    db.upsert_scanned_folder(str(tmp_path), "Half Life 2", "pending_review",
                              candidate_appid=220, candidate_name="Half-Life 2", candidate_score=91.0)
    db.upsert_scanned_folder(str(tmp_path), "Mystery Game", "unmatched")

    resp = admin_client.get("/admin/scanner")
    assert resp.status_code == 200
    assert b"Half Life 2" in resp.data
    assert b"Mystery Game" in resp.data


def test_admin_scanner_add_folder(admin_client):
    import scanner
    resp = admin_client.post("/admin/scanner/folders/add", data={
        "path": "/mnt/games", "label": "Main", "client_path": "\\\\HOMESERVER\\Games",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert scanner.games_folders() == ["/mnt/games"]
    assert scanner.client_path_for("/mnt/games") == "\\\\HOMESERVER\\Games"


def test_admin_scanner_add_folder_refuses_a_duplicate(admin_client):
    import db
    db.add_games_folder("/mnt/games")
    resp = admin_client.post("/admin/scanner/folders/add", data={"path": "/mnt/games"},
                              follow_redirects=True)
    assert b"already configured" in resp.data


def test_admin_scanner_update_folder(admin_client):
    import db
    folder_id = db.add_games_folder("/mnt/games")
    resp = admin_client.post(f"/admin/scanner/folders/{folder_id}/update",
                              data={"label": "Main Drive", "client_path": "Z:\\"},
                              follow_redirects=True)
    assert resp.status_code == 200
    row = db.get_games_folder(folder_id)
    assert row["label"] == "Main Drive"
    assert row["client_path"] == "Z:\\"


def test_admin_scanner_delete_folder(admin_client):
    import db
    import scanner
    folder_id = db.add_games_folder("/mnt/games")
    resp = admin_client.post(f"/admin/scanner/folders/{folder_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert scanner.games_folders() == []


def test_admin_scanner_settings_saves_interval_and_threshold(admin_client):
    import scanner
    resp = admin_client.post("/admin/scanner/settings", data={
        "scan_interval_minutes": "15",
        "fuzzy_match_threshold": "90",
        "recently_added_count": "20",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert scanner.scan_interval_seconds() == 15 * 60
    assert scanner.fuzzy_match_threshold() == 90
    assert scanner.recently_added_count() == 20


def test_admin_scanner_page_lists_deleted_games(admin_client):
    import db
    db.add_games_folder("/games")
    db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "deleted", steam_appid=220,
                              name="Half-Life 2")
    resp = admin_client.get("/admin/scanner")
    assert resp.status_code == 200
    assert b"Half-Life 2" in resp.data
    assert b'class="badge deleted"' in resp.data


def test_admin_scanner_forget_deleted_removes_the_row(admin_client):
    import db
    db.add_games_folder("/games")
    row_id = db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "deleted", steam_appid=220,
                                       name="Half-Life 2")
    resp = admin_client.post(f"/admin/scanner/{row_id}/forget", follow_redirects=True)
    assert resp.status_code == 200
    assert db.get_scanned_folder(row_id) is None


def test_admin_scanner_forget_deleted_404s_for_a_non_deleted_row(admin_client):
    import db
    db.add_games_folder("/games")
    row_id = db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "matched", steam_appid=220)
    resp = admin_client.post(f"/admin/scanner/{row_id}/forget")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Blacklist admin routes
# ---------------------------------------------------------------------------
def test_admin_blacklist_requires_login(client):
    resp = client.get("/admin/blacklist")
    assert resp.status_code == 302


def test_admin_blacklist_add_looks_up_the_name_from_steam(admin_client, monkeypatch):
    import steam
    monkeypatch.setattr(steam, "fetch_app_summary",
                         lambda appid: {"appid": appid, "name": "Half-Life 2", "icon_url": "",
                                        "short_description": "", "genres": []})
    resp = admin_client.post("/admin/blacklist/add", data={"appid": "220", "reason": "Too big"},
                              follow_redirects=True)
    assert resp.status_code == 200
    assert b"Half-Life 2" in resp.data
    assert b"Too big" in resp.data

    import db
    assert db.is_blacklisted(220) is True


def test_admin_blacklist_add_refuses_a_duplicate(admin_client, monkeypatch):
    import db
    import steam
    monkeypatch.setattr(steam, "fetch_app_summary",
                         lambda appid: {"appid": appid, "name": "Half-Life 2", "icon_url": "",
                                        "short_description": "", "genres": []})
    db.add_to_blacklist(220, "Half-Life 2")
    resp = admin_client.post("/admin/blacklist/add", data={"appid": "220"}, follow_redirects=True)
    assert b"already blacklisted" in resp.data


def test_admin_blacklist_delete(admin_client):
    import db
    entry_id = db.add_to_blacklist(220, "Half-Life 2")
    resp = admin_client.post(f"/admin/blacklist/{entry_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert db.is_blacklisted(220) is False


# ---------------------------------------------------------------------------
# Request-limit admin routes
# ---------------------------------------------------------------------------
def test_admin_limits_requires_login(client):
    resp = client.get("/admin/limits")
    assert resp.status_code == 302


def test_admin_limits_saves_the_global_limit(admin_client):
    import db
    resp = admin_client.post("/admin/limits/global", data={"period": "weekly", "count": "5"},
                              follow_redirects=True)
    assert resp.status_code == 200
    assert db.get_global_request_limit() == ("weekly", 5)


def test_admin_limits_global_unlimited_ignores_count(admin_client):
    import db
    resp = admin_client.post("/admin/limits/global", data={"period": "none", "count": ""},
                              follow_redirects=True)
    assert resp.status_code == 200
    assert db.get_global_request_limit()[0] == "none"


def test_admin_limits_lists_known_requesters(admin_client):
    import db
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    resp = admin_client.get("/admin/limits")
    assert resp.status_code == 200
    assert b"Alice" in resp.data
    assert b"Following the global limit" in resp.data


def test_admin_limits_sets_a_per_user_override(admin_client):
    import db
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    resp = admin_client.post("/admin/limits/user/jf-1/set",
                              data={"name": "Alice", "period": "monthly", "count": "3"},
                              follow_redirects=True)
    assert resp.status_code == 200
    assert db.get_user_request_limit("jf-1") == ("monthly", 3)


def test_admin_limits_clearing_an_override_reverts_to_global(admin_client):
    import db
    db.set_user_request_limit("jf-1", "Alice", "monthly", 3)
    resp = admin_client.post("/admin/limits/user/jf-1/set",
                              data={"name": "Alice", "period": "none", "count": ""},
                              follow_redirects=True)
    assert resp.status_code == 200
    assert db.get_user_request_limit("jf-1") is None


def test_admin_limits_lists_jellyfin_users_who_have_never_requested(admin_client, monkeypatch):
    """A per-user override should be settable before that visitor ever signs
    in here - the whole point of listing Jellyfin's own user directory
    instead of only past requesters."""
    import jellyfin_auth
    monkeypatch.setattr(jellyfin_auth, "list_public_users",
                         lambda: {"ok": True, "users": [{"id": "jf-2", "name": "Bob"}]})
    monkeypatch.setattr(jellyfin_auth, "is_enabled", lambda: True)

    resp = admin_client.get("/admin/limits")
    assert resp.status_code == 200
    assert b"Bob" in resp.data
    assert b"Never requested" in resp.data


def test_admin_limits_merges_jellyfin_users_with_past_requesters(admin_client, monkeypatch):
    """A visitor known to both Jellyfin and this app's own requests table
    must appear once, not twice - and keep their live Jellyfin name."""
    import db
    import jellyfin_auth
    db.create_request(70, "Half-Life", "", "", "jf-1", "old-stored-name")
    monkeypatch.setattr(jellyfin_auth, "list_public_users",
                         lambda: {"ok": True, "users": [{"id": "jf-1", "name": "Alice"}]})
    monkeypatch.setattr(jellyfin_auth, "is_enabled", lambda: True)

    resp = admin_client.get("/admin/limits")
    assert resp.data.count(b"request-row__title") == 1
    assert b"Alice" in resp.data
    assert b"old-stored-name" not in resp.data
    assert b"Last requested" in resp.data  # this one HAS requested before


def test_admin_limits_keeps_a_requester_no_longer_on_jellyfins_public_list(admin_client, monkeypatch):
    """An account since deleted or hidden in Jellyfin must not just vanish
    from the override list if it still has an override or request history."""
    import db
    import jellyfin_auth
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    monkeypatch.setattr(jellyfin_auth, "list_public_users", lambda: {"ok": True, "users": []})
    monkeypatch.setattr(jellyfin_auth, "is_enabled", lambda: True)

    resp = admin_client.get("/admin/limits")
    assert b"Alice" in resp.data


def test_admin_limits_flags_an_unreachable_jellyfin(admin_client, monkeypatch):
    import jellyfin_auth
    monkeypatch.setattr(jellyfin_auth, "list_public_users", lambda: {"ok": False, "users": []})
    monkeypatch.setattr(jellyfin_auth, "is_enabled", lambda: True)

    resp = admin_client.get("/admin/limits")
    assert b"Couldn&#39;t reach Jellyfin" in resp.data or b"Couldn't reach Jellyfin" in resp.data


def test_admin_limits_does_not_flag_jellyfin_when_not_configured(admin_client, monkeypatch):
    import jellyfin_auth
    monkeypatch.setattr(jellyfin_auth, "is_enabled", lambda: False)
    resp = admin_client.get("/admin/limits")
    assert b"Couldn&#39;t reach Jellyfin" not in resp.data
    assert b"Couldn't reach Jellyfin" not in resp.data


def test_admin_scanner_scan_runs_a_pass_and_flashes_a_summary(admin_client, monkeypatch, tmp_path):
    import db
    import scanner
    db.add_games_folder(str(tmp_path))
    monkeypatch.setattr(scanner, "scan_once",
                         lambda: {"scanned": 1, "matched": 1, "pending_review": 0, "unmatched": 0})

    resp = admin_client.post("/admin/scanner/scan", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Scanned 1 folder" in resp.data


def test_admin_scanner_confirm_flashes_the_scanner_error_on_refusal(admin_client, monkeypatch):
    import scanner

    def boom(row_id, appid):
        raise scanner.ScannerError("That folder is gone.")
    monkeypatch.setattr(scanner, "confirm_match", boom)

    resp = admin_client.post("/admin/scanner/1/confirm", data={"appid": "220"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"That folder is gone." in resp.data


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


# ---------------------------------------------------------------------------
# Database backup / restore
# ---------------------------------------------------------------------------
def test_admin_backup_db_requires_login(client):
    resp = client.get("/admin/about/backup-db")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_admin_backup_db_downloads_a_valid_sqlite_database(admin_client):
    import db
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")

    resp = admin_client.get("/admin/about/backup-db")
    assert resp.status_code == 200
    assert resp.data.startswith(db.SQLITE_HEADER)
    assert "attachment" in resp.headers["Content-Disposition"]


def test_admin_restore_db_requires_a_file(admin_client):
    resp = admin_client.post("/admin/about/restore-db", data={}, follow_redirects=True)
    assert b"Choose a backup file" in resp.data


def test_admin_restore_db_rejects_a_bad_upload_without_touching_the_live_db(admin_client):
    import db
    import io
    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")

    resp = admin_client.post(
        "/admin/about/restore-db",
        data={"backup": (io.BytesIO(b"not a database"), "backup.db")},
        content_type="multipart/form-data", follow_redirects=True)
    assert b"Restore refused" in resp.data
    assert db.get_request(rid) is not None


def test_admin_restore_db_replaces_the_data_and_restarts(admin_client, monkeypatch, tmp_path):
    import io
    import db

    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")

    # Build a standalone "uploaded backup" with different data.
    other_path = tmp_path / "other.db"
    original_db_path = db.DB_PATH
    db.DB_PATH = str(other_path)
    db.init_db()
    db.create_request(220, "Half-Life 2", "", "", "jf-2", "Bob")
    db.DB_PATH = original_db_path
    upload_bytes = other_path.read_bytes()

    restart_calls = []
    monkeypatch.setattr(app_module, "_restart_process", lambda: restart_calls.append(1))

    resp = admin_client.post(
        "/admin/about/restore-db",
        data={"backup": (io.BytesIO(upload_bytes), "backup.db")},
        content_type="multipart/form-data", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Database restored" in resp.data
    assert restart_calls == [1]

    rows = db.list_requests()
    assert len(rows) == 1
    assert rows[0]["name"] == "Half-Life 2"


def test_db_safety_snapshots_stay_inside_the_isolated_test_sandbox(isolated_db):
    """Regression guard: the safety-snapshot directory must follow db.DB_PATH
    (which conftest.py's isolated_db fixture points at a tmp_path sandbox for
    every test), not a fixed config.APP_ROOT-based path - otherwise every
    restore test silently writes real snapshot files into this actual repo's
    instance/db_backups/ instead of staying contained. Found by hand-testing
    a real restore against a running dev server and then noticing stray
    files in git status, not by any assertion in the mocked test suite."""
    import os
    import config as config_module
    import db
    real_instance_dir = os.path.join(config_module.APP_ROOT, "instance")

    backup_dir = app_module._db_safety_backup_dir()

    assert backup_dir.startswith(os.path.dirname(db.DB_PATH))
    assert not backup_dir.startswith(real_instance_dir)


# ---------------------------------------------------------------------------
# status-portal integration: GET /health and /admin/integrations
# ---------------------------------------------------------------------------
def test_health_requires_api_key(client):
    resp = client.get("/health")
    assert resp.status_code == 401


def test_health_rejects_wrong_key(client):
    import db
    db.regenerate_health_api_key()
    resp = client.get("/health", headers={"X-Api-Key": "wrong-key"})
    assert resp.status_code == 401


def test_health_returns_status_version_and_pending_count(client):
    import config
    import db
    key = db.regenerate_health_api_key()
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")

    resp = client.get("/health", headers={"X-Api-Key": key})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["version"] == config.VERSION
    assert body["pending_requests"] == 1


def test_admin_integrations_requires_login(client):
    resp = client.get("/admin/integrations")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_admin_integrations_page_generates_a_health_key_on_first_view(admin_client):
    import db
    assert db.get_health_api_key() is None
    resp = admin_client.get("/admin/integrations")
    assert resp.status_code == 200
    key = db.get_health_api_key()
    assert key
    assert key.encode() in resp.data


def test_admin_integrations_regenerate_health_key(admin_client):
    import db
    first = db.get_or_create_health_api_key()
    resp = admin_client.post("/admin/integrations/health-key/regenerate", follow_redirects=True)
    assert resp.status_code == 200
    assert db.get_health_api_key() != first


def test_admin_integrations_add_update_delete_link(admin_client):
    import db
    resp = admin_client.post("/admin/integrations/links/add",
                              data={"label": "LAN", "url": "http://192.168.1.10:5000"},
                              follow_redirects=True)
    assert resp.status_code == 200
    links = db.list_status_portal_links()
    assert len(links) == 1
    link_id = links[0]["id"]

    resp = admin_client.post(f"/admin/integrations/links/{link_id}/update",
                              data={"label": "Tailscale", "url": "http://100.64.0.1:5000"},
                              follow_redirects=True)
    assert resp.status_code == 200
    assert db.get_status_portal_link(link_id)["label"] == "Tailscale"

    resp = admin_client.post(f"/admin/integrations/links/{link_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert db.list_status_portal_links() == []


def test_admin_integrations_add_link_requires_both_fields(admin_client):
    import db
    admin_client.post("/admin/integrations/links/add", data={"label": "", "url": ""})
    assert db.list_status_portal_links() == []


def test_admin_integrations_update_link_404s_for_an_unknown_id(admin_client):
    resp = admin_client.post("/admin/integrations/links/999999/update",
                              data={"label": "x", "url": "http://x"})
    assert resp.status_code == 404


def test_admin_integrations_delete_link_404s_for_an_unknown_id(admin_client):
    resp = admin_client.post("/admin/integrations/links/999999/delete")
    assert resp.status_code == 404


def test_admin_integrations_saves_notify_settings(admin_client):
    import db
    resp = admin_client.post("/admin/integrations/notify",
                              data={"notify_url": "http://status-portal.local", "notify_api_key": "sp-key",
                                    "notify_on_new_request": "on", "notify_on_status_change": "on"},
                              follow_redirects=True)
    assert resp.status_code == 200
    assert db.get_status_portal_notify_url() == "http://status-portal.local"
    assert db.get_status_portal_notify_api_key() == "sp-key"
    assert db.notify_on_new_request_enabled() is True
    assert db.notify_on_status_change_enabled() is True


def test_admin_integrations_saves_notify_settings_with_both_toggles_off(admin_client):
    """An unchecked checkbox sends no form field at all - this must be read
    as "off," not silently ignored and left at its previous value."""
    import db
    admin_client.post("/admin/integrations/notify",
                       data={"notify_url": "http://status-portal.local", "notify_api_key": "sp-key"})
    assert db.notify_on_new_request_enabled() is False
    assert db.notify_on_status_change_enabled() is False


def test_home_links_render_on_the_public_page_when_configured(client):
    import db
    db.add_status_portal_link("LAN", "http://192.168.1.10:5000")
    resp = client.get("/")
    assert b"LAN" in resp.data
    assert b"192.168.1.10" in resp.data


def test_home_links_hidden_when_none_configured(client):
    resp = client.get("/")
    assert b"home-links" not in resp.data


def test_submit_request_notifies_status_portal(visitor_session, monkeypatch):
    import db
    import status_portal_client

    calls = []
    monkeypatch.setattr(status_portal_client, "notify_new_request", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr("steam.fetch_app_summary", lambda appid: {
        "appid": appid, "name": "Half-Life", "icon_url": "", "short_description": ""})

    visitor_session.post("/request", data={"appid": "70"})
    assert calls == [(("Half-Life", "alice"), {})]


def test_admin_update_request_notifies_status_portal_only_on_a_real_status_change(admin_client, monkeypatch):
    import db
    import status_portal_client

    calls = []
    monkeypatch.setattr(status_portal_client, "notify_status_changed",
                         lambda *a, **k: calls.append((a, k)))
    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")

    admin_client.post(f"/admin/requests/{rid}/status", data={"status": "pending", "admin_note": ""})
    assert calls == []

    admin_client.post(f"/admin/requests/{rid}/status", data={"status": "approved", "admin_note": ""})
    assert calls == [(("jf-1", "Half-Life", "approved"), {})]


def test_submit_request_respects_the_new_request_toggle_when_off(visitor_session, monkeypatch):
    import db
    import status_portal_client

    db.set_notify_on_new_request_enabled(False)
    calls = []
    monkeypatch.setattr(status_portal_client, "notify_new_request", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr("steam.fetch_app_summary", lambda appid: {
        "appid": appid, "name": "Half-Life", "icon_url": "", "short_description": ""})

    visitor_session.post("/request", data={"appid": "70"})
    assert calls == []


def test_admin_update_request_respects_the_status_change_toggle_when_off(admin_client, monkeypatch):
    import db
    import status_portal_client

    db.set_notify_on_status_change_enabled(False)
    calls = []
    monkeypatch.setattr(status_portal_client, "notify_status_changed",
                         lambda *a, **k: calls.append((a, k)))
    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")

    admin_client.post(f"/admin/requests/{rid}/status", data={"status": "approved", "admin_note": ""})
    assert calls == []


def test_admin_integrations_notify_test_reports_success(admin_client, monkeypatch):
    import status_portal_client
    monkeypatch.setattr(status_portal_client, "send_test_notification",
                         lambda: {"ok": True, "message": "Delivered - status-portal returned 200."})
    resp = admin_client.post("/admin/integrations/notify/test", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Delivered" in resp.data


def test_admin_integrations_notify_test_reports_failure(admin_client, monkeypatch):
    import status_portal_client
    monkeypatch.setattr(status_portal_client, "send_test_notification",
                         lambda: {"ok": False, "message": "Not configured - set a notify URL and API key first."})
    resp = admin_client.post("/admin/integrations/notify/test", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Not configured" in resp.data


def test_admin_integrations_notify_test_requires_login(client):
    resp = client.post("/admin/integrations/notify/test")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]
