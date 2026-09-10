def test_setting_roundtrip(isolated_db):
    import db
    assert db.get_setting("missing_key") is None
    assert db.get_setting("missing_key", "default") == "default"
    db.set_setting("admin_password_hash", "hash123")
    assert db.get_setting("admin_password_hash") == "hash123"
    db.set_setting("admin_password_hash", "hash456")
    assert db.get_setting("admin_password_hash") == "hash456"


def test_create_and_list_requests(isolated_db):
    import db
    rid = db.create_request(70, "Half-Life", "http://example.com/icon.jpg", "A classic.",
                             "jf-1", "Alice")
    row = db.get_request(rid)
    assert row["name"] == "Half-Life"
    assert row["status"] == "pending"

    all_requests = db.list_requests()
    assert len(all_requests) == 1

    pending = db.list_requests(status="pending")
    assert len(pending) == 1
    done = db.list_requests(status="done")
    assert len(done) == 0


def test_update_request_status(isolated_db):
    import db
    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    db.update_request_status(rid, "approved", "Looks good")
    row = db.get_request(rid)
    assert row["status"] == "approved"
    assert row["admin_note"] == "Looks good"


def test_update_request_status_rejects_unknown_status(isolated_db):
    import db
    import pytest
    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    with pytest.raises(ValueError):
        db.update_request_status(rid, "not-a-real-status", "")


def test_active_request_appids_excludes_rejected(isolated_db):
    import db
    rid1 = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    rid2 = db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    db.update_request_status(rid2, "rejected", "")

    active = db.active_request_appids([70, 220, 999])
    assert active == {70: "pending"}


def test_get_active_request_for_appid(isolated_db):
    import db
    assert db.get_active_request_for_appid(70) is None
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    found = db.get_active_request_for_appid(70)
    assert found["name"] == "Half-Life"


# ---------------------------------------------------------------------------
# Installed games
# ---------------------------------------------------------------------------
def test_upsert_installed_game_and_lookup(isolated_db):
    import db
    assert db.is_installed(220) is False
    db.upsert_installed_game(220, "Half-Life 2 [220]", "/games/Half-Life 2 [220]")
    assert db.is_installed(220) is True
    assert db.installed_appids([220, 999]) == {220}
    games = db.list_installed_games()
    assert len(games) == 1
    assert games[0]["folder_name"] == "Half-Life 2 [220]"


def test_upsert_installed_game_overwrites_on_conflict(isolated_db):
    import db
    db.upsert_installed_game(220, "old name", "/games/old")
    db.upsert_installed_game(220, "new name", "/games/new")
    games = db.list_installed_games()
    assert len(games) == 1
    assert games[0]["folder_name"] == "new name"


def test_installed_appids_with_no_appids_returns_empty(isolated_db):
    import db
    assert db.installed_appids([]) == set()


# ---------------------------------------------------------------------------
# Scan matches
# ---------------------------------------------------------------------------
def test_upsert_and_list_pending_scan_matches(isolated_db):
    import db
    rid = db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    db.upsert_scan_match("Half Life 2", "/games/Half Life 2", rid, 90)
    matches = db.list_pending_scan_matches()
    assert len(matches) == 1
    assert matches[0]["request_name"] == "Half-Life 2"
    assert matches[0]["score"] == 90


def test_upsert_scan_match_refreshes_score_on_conflict(isolated_db):
    import db
    rid = db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    db.upsert_scan_match("Half Life 2", "/games/x", rid, 80)
    db.upsert_scan_match("Half Life 2", "/games/x", rid, 95)
    matches = db.list_pending_scan_matches()
    assert len(matches) == 1
    assert matches[0]["score"] == 95


def test_set_scan_match_status_and_rejected_pairs(isolated_db):
    import db
    rid = db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    db.upsert_scan_match("Half Life 2", "/games/x", rid, 90)
    match = db.list_pending_scan_matches()[0]

    db.set_scan_match_status(match["id"], "rejected")
    assert db.list_pending_scan_matches() == []
    assert db.get_rejected_pairs("/games/x") == {rid}


def test_set_scan_match_status_rejects_unknown_status(isolated_db):
    import db
    import pytest
    rid = db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    db.upsert_scan_match("Half Life 2", "/games/x", rid, 90)
    match = db.list_pending_scan_matches()[0]
    with pytest.raises(ValueError):
        db.set_scan_match_status(match["id"], "bogus")


def test_delete_scan_matches_for_folder_only_touches_pending(isolated_db):
    import db
    rid1 = db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    rid2 = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    db.upsert_scan_match("folder", "/games/x", rid1, 90)
    db.upsert_scan_match("folder", "/games/x", rid2, 85)
    matches = db.list_pending_scan_matches()
    db.set_scan_match_status(matches[0]["id"], "rejected")

    db.delete_scan_matches_for_folder("/games/x")
    assert db.list_pending_scan_matches() == []
    # The rejected one survives the delete (only pending rows are removed).
    assert db.get_rejected_pairs("/games/x") == {matches[0]["request_id"]}


def test_get_scan_match_missing_returns_none(isolated_db):
    import db
    assert db.get_scan_match(999999) is None
