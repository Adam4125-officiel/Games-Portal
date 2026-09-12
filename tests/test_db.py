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


def test_delete_request(isolated_db):
    import db
    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    db.delete_request(rid)
    assert db.get_request(rid) is None
    assert db.list_requests() == []


def test_delete_request_is_a_no_op_for_an_unknown_id(isolated_db):
    import db
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    db.delete_request(999999)  # must not raise
    assert len(db.list_requests()) == 1


def test_list_requests_filters_by_requested_by_id(isolated_db):
    import db
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    db.create_request(220, "Half-Life 2", "", "", "jf-2", "Bob")

    mine = db.list_requests(requested_by_id="jf-1")
    assert len(mine) == 1
    assert mine[0]["name"] == "Half-Life"


def test_list_requests_combines_status_and_requested_by_id_filters(isolated_db):
    import db
    rid = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    db.update_request_status(rid, "approved", "")

    approved_mine = db.list_requests(status="approved", requested_by_id="jf-1")
    assert len(approved_mine) == 1
    assert approved_mine[0]["name"] == "Half-Life"


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
# Backup / restore
# ---------------------------------------------------------------------------
def test_backup_to_file_produces_an_independently_openable_database(isolated_db, tmp_path):
    import sqlite3
    import db
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")

    dest = str(tmp_path / "backup.db")
    db.backup_to_file(dest)

    conn = sqlite3.connect(dest)
    rows = conn.execute("SELECT name FROM requests").fetchall()
    conn.close()
    assert rows == [("Half-Life",)]


def test_validate_backup_file_rejects_a_non_sqlite_file(tmp_path):
    import db
    not_a_db = tmp_path / "not-a-db.db"
    not_a_db.write_text("just some text, not a database")

    error = db.validate_backup_file(str(not_a_db))
    assert error is not None
    assert "isn't a SQLite database" in error


def test_validate_backup_file_rejects_a_database_missing_required_tables(tmp_path):
    import sqlite3
    import db
    other_db = tmp_path / "unrelated.db"
    conn = sqlite3.connect(str(other_db))
    conn.execute("CREATE TABLE something_else (id INTEGER)")
    conn.commit()
    conn.close()

    error = db.validate_backup_file(str(other_db))
    assert error is not None
    assert "isn't a games-portal backup" in error


def test_validate_backup_file_accepts_a_real_backup(isolated_db, tmp_path):
    import db
    dest = str(tmp_path / "backup.db")
    db.backup_to_file(dest)
    assert db.validate_backup_file(dest) is None


def test_restore_from_file_replaces_the_live_database(isolated_db, tmp_path):
    import db
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")

    # A second, independent database with different data - standing in for an
    # uploaded backup taken at some other point in time.
    other_path = tmp_path / "test_portal_2.db"
    original_db_path = db.DB_PATH
    db.DB_PATH = str(other_path)
    db.init_db()
    db.create_request(220, "Half-Life 2", "", "", "jf-2", "Bob")
    db.DB_PATH = original_db_path

    staged = str(tmp_path / "staged-for-restore.db")
    import shutil
    shutil.copy2(other_path, staged)

    db.restore_from_file(staged)

    rows = db.list_requests()
    assert len(rows) == 1
    assert rows[0]["name"] == "Half-Life 2"


# ---------------------------------------------------------------------------
# Regression: a real production crash. An install that had already applied an
# earlier, differently-shaped version of the scanner's table (before this
# feature's current column set was settled) crashed every page load with
# sqlite3.OperationalError: no such column: status - CREATE TABLE IF NOT
# EXISTS is a silent no-op against a table that already exists, so the
# missing column was never added on any of that install's later restarts.
# ---------------------------------------------------------------------------
def test_init_db_repairs_an_installed_games_table_missing_a_column(tmp_path, monkeypatch):
    """The actual real-world crash this guards against: an install already had
    a root_path-shaped installed_games table (this schema), just missing one
    column added after it - CREATE TABLE IF NOT EXISTS is a no-op there, so
    only _ensure_column() repairs it, in place, preserving existing rows."""
    import sqlite3
    import db
    db_path = str(tmp_path / "legacy.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE installed_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root_path TEXT NOT NULL,
            folder_name TEXT NOT NULL,
            UNIQUE(root_path, folder_name)
        )
    """)
    conn.execute("INSERT INTO installed_games (root_path, folder_name) VALUES (?, ?)",
                 ("/games", "Half-Life 2 {steamapp-220}"))
    conn.commit()
    conn.close()

    db.init_db()  # must not raise, and must repair the table in place

    row = db.get_scanned_folder(1)
    assert row["folder_name"] == "Half-Life 2 {steamapp-220}"  # pre-existing data survives
    assert row["status"] == "unmatched"  # the missing column now exists with a sane default

    # The exact call shape from the original production traceback now works.
    db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "matched", steam_appid=220)
    assert db.matched_appids() == {220}


def test_init_db_recreates_an_installed_games_table_that_predates_multi_folder_support(
        tmp_path, monkeypatch):
    """The table's very first shape (before root_path existed at all, keyed by
    folder_name alone) can't be patched with _ensure_column() - a UNIQUE
    constraint can't be altered in place in SQLite. Since every row here is
    scanner.py's own rebuildable cache (never real user data - this table is
    deliberately excluded from the backup/restore required-tables check),
    the safe fix is to drop and recreate it fresh rather than attempt a
    manual table-rebuild migration for data that doesn't need to survive."""
    import sqlite3
    import db
    db_path = str(tmp_path / "legacy.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE installed_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_name TEXT NOT NULL UNIQUE,
            steam_appid INTEGER,
            status TEXT NOT NULL DEFAULT 'unmatched',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    db.init_db()  # must not raise

    assert db.list_scanned_folders() == []
    row_id = db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "matched", steam_appid=220)
    assert db.get_scanned_folder(row_id)["root_path"] == "/games"


# ---------------------------------------------------------------------------
# Deleted-game handling (see scanner.prune_scanned_folders and
# app.py's admin_scanner_forget_deleted)
# ---------------------------------------------------------------------------
def test_prune_scanned_folders_marks_a_missing_matched_row_deleted(isolated_db):
    import db
    db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "matched", steam_appid=220)
    db.prune_scanned_folders(["/games"], {"/games"}, seen_pairs=set())
    row = db.list_scanned_folders()[0]
    assert row["status"] == "deleted"
    assert row["steam_appid"] == 220
    assert db.matched_appids() == set()


def test_prune_scanned_folders_deletes_a_missing_unmatched_row_outright(isolated_db):
    import db
    db.upsert_scanned_folder("/games", "Some Unmatched Game", "unmatched")
    db.prune_scanned_folders(["/games"], {"/games"}, seen_pairs=set())
    assert db.list_scanned_folders() == []


def test_prune_scanned_folders_leaves_an_already_deleted_row_alone(isolated_db):
    import db
    row_id = db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "deleted", steam_appid=220)
    db.prune_scanned_folders(["/games"], {"/games"}, seen_pairs=set())
    assert db.get_scanned_folder(row_id)["status"] == "deleted"


def test_delete_scanned_folder_forgets_a_row(isolated_db):
    import db
    row_id = db.upsert_scanned_folder("/games", "Half-Life 2 {steamapp-220}", "deleted", steam_appid=220)
    db.delete_scanned_folder(row_id)
    assert db.get_scanned_folder(row_id) is None


# ---------------------------------------------------------------------------
# Blacklist (see app.py's submit_request, the enforcement point)
# ---------------------------------------------------------------------------
def test_add_to_blacklist_and_lookup(isolated_db):
    import db
    entry_id = db.add_to_blacklist(220, "Half-Life 2", "Too big for the drive")
    assert entry_id is not None
    assert db.is_blacklisted(220) is True
    assert db.is_blacklisted(999) is False
    entry = db.get_blacklist_entry(220)
    assert entry["name"] == "Half-Life 2"
    assert entry["reason"] == "Too big for the drive"


def test_add_to_blacklist_refuses_a_duplicate_appid(isolated_db):
    import db
    assert db.add_to_blacklist(220, "Half-Life 2") is not None
    assert db.add_to_blacklist(220, "Half-Life 2") is None
    assert len(db.list_blacklist()) == 1


def test_blacklist_map_covers_only_requested_appids(isolated_db):
    import db
    db.add_to_blacklist(220, "Half-Life 2", "reason a")
    db.add_to_blacklist(70, "Half-Life", "reason b")
    assert db.blacklist_map([220, 999]) == {220: "reason a"}
    assert db.blacklist_map([]) == {}


def test_remove_from_blacklist(isolated_db):
    import db
    entry_id = db.add_to_blacklist(220, "Half-Life 2")
    db.remove_from_blacklist(entry_id)
    assert db.is_blacklisted(220) is False


# ---------------------------------------------------------------------------
# Request limits - global default + per-user override (see app.py's
# _request_limit_error, the enforcement point)
# ---------------------------------------------------------------------------
def test_global_request_limit_defaults_to_unlimited(isolated_db):
    import db
    assert db.get_global_request_limit() == ("none", 0)


def test_set_and_get_global_request_limit(isolated_db):
    import db
    db.set_global_request_limit("daily", 3)
    assert db.get_global_request_limit() == ("daily", 3)


def test_set_global_request_limit_rejects_an_unknown_period(isolated_db):
    import db
    import pytest
    with pytest.raises(ValueError):
        db.set_global_request_limit("hourly", 3)


def test_user_request_limit_override_roundtrip(isolated_db):
    import db
    assert db.get_user_request_limit("jf-1") is None
    db.set_user_request_limit("jf-1", "Alice", "weekly", 5)
    assert db.get_user_request_limit("jf-1") == ("weekly", 5)

    overrides = db.list_user_request_limits()
    assert len(overrides) == 1
    assert overrides[0]["requested_by_name"] == "Alice"


def test_set_user_request_limit_upserts_rather_than_duplicates(isolated_db):
    import db
    db.set_user_request_limit("jf-1", "Alice", "weekly", 5)
    db.set_user_request_limit("jf-1", "Alice", "monthly", 10)
    assert db.get_user_request_limit("jf-1") == ("monthly", 10)
    assert len(db.list_user_request_limits()) == 1


def test_clear_user_request_limit(isolated_db):
    import db
    db.set_user_request_limit("jf-1", "Alice", "weekly", 5)
    db.clear_user_request_limit("jf-1")
    assert db.get_user_request_limit("jf-1") is None


def test_distinct_requesters_deduplicates_by_id(isolated_db):
    import db
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    db.create_request(620, "Portal 2", "", "", "jf-2", "Bob")

    requesters = db.distinct_requesters()
    assert {r["requested_by_id"] for r in requesters} == {"jf-1", "jf-2"}


def test_count_requests_since_excludes_rejected(isolated_db):
    import db
    id1 = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    db.update_request_status(id1, "rejected", "")

    assert db.count_requests_since("jf-1", "2000-01-01T00:00:00+00:00") == 1


def test_count_requests_since_respects_the_window_start(isolated_db):
    import db
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    assert db.count_requests_since("jf-1", "2999-01-01T00:00:00+00:00") == 0


def test_count_unresolved_requests_excludes_done_and_rejected(isolated_db):
    import db
    id1 = db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    id2 = db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    db.create_request(620, "Portal 2", "", "", "jf-1", "Alice")
    db.update_request_status(id1, "done", "")
    db.update_request_status(id2, "rejected", "")

    assert db.count_unresolved_requests() == 1


def test_get_or_create_health_api_key_generates_once_and_persists(isolated_db):
    import db
    assert db.get_health_api_key() is None
    key = db.get_or_create_health_api_key()
    assert key
    assert db.get_or_create_health_api_key() == key


def test_regenerate_health_api_key_changes_the_key(isolated_db):
    import db
    first = db.get_or_create_health_api_key()
    second = db.regenerate_health_api_key()
    assert second != first
    assert db.get_health_api_key() == second


def test_status_portal_link_crud(isolated_db):
    import db
    link_id = db.add_status_portal_link("LAN", "http://192.168.1.10:5000")
    assert db.list_status_portal_links() == [
        {"id": link_id, "label": "LAN", "url": "http://192.168.1.10:5000",
         "created_at": db.get_status_portal_link(link_id)["created_at"]}
    ]

    db.update_status_portal_link(link_id, "Tailscale", "http://100.64.0.1:5000")
    updated = db.get_status_portal_link(link_id)
    assert updated["label"] == "Tailscale"
    assert updated["url"] == "http://100.64.0.1:5000"

    db.delete_status_portal_link(link_id)
    assert db.list_status_portal_links() == []
    assert db.get_status_portal_link(link_id) is None


def test_status_portal_notify_settings_roundtrip(isolated_db):
    import db
    assert db.get_status_portal_notify_url() == ""
    assert db.get_status_portal_notify_api_key() == ""

    db.set_status_portal_notify_url("http://status-portal.local")
    db.set_status_portal_notify_api_key("sp-key-123")
    assert db.get_status_portal_notify_url() == "http://status-portal.local"
    assert db.get_status_portal_notify_api_key() == "sp-key-123"
