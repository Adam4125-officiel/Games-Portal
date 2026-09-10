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
# Backup and restore
# ---------------------------------------------------------------------------
def test_backup_to_file_produces_a_working_copy(isolated_db, tmp_path):
    import db
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    dest = tmp_path / "backup.db"
    db.backup_to_file(str(dest))
    assert dest.exists()

    import sqlite3
    conn = sqlite3.connect(str(dest))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM requests").fetchone()
    conn.close()
    assert row["name"] == "Half-Life"


def test_validate_backup_file_rejects_a_non_sqlite_file(tmp_path):
    import db
    bogus = tmp_path / "not-a-db.txt"
    bogus.write_text("hello, this is not a database")
    error = db.validate_backup_file(str(bogus))
    assert error is not None
    assert "isn't a SQLite database" in error


def test_validate_backup_file_rejects_a_foreign_sqlite_database(tmp_path):
    import db
    import sqlite3
    foreign = tmp_path / "other.db"
    conn = sqlite3.connect(str(foreign))
    conn.execute("CREATE TABLE totally_unrelated (id INTEGER)")
    conn.commit()
    conn.close()

    error = db.validate_backup_file(str(foreign))
    assert error is not None
    assert "isn't a Games Portal backup" in error


def test_validate_backup_file_accepts_a_real_backup(isolated_db, tmp_path):
    import db
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")
    dest = tmp_path / "backup.db"
    db.backup_to_file(str(dest))
    assert db.validate_backup_file(str(dest)) is None


def test_restore_from_file_replaces_the_live_database(isolated_db, tmp_path):
    import db
    db.create_request(70, "Half-Life", "", "", "jf-1", "Alice")

    replacement = tmp_path / "replacement.db"
    import sqlite3
    conn = sqlite3.connect(str(replacement))
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, steam_appid INTEGER, name TEXT, "
                 "icon_url TEXT, short_description TEXT, requested_by_id TEXT, requested_by_name TEXT, "
                 "status TEXT, admin_note TEXT, created_at TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO requests (id, steam_appid, name, icon_url, short_description, "
                 "requested_by_id, requested_by_name, status, admin_note, created_at, updated_at) "
                 "VALUES (1, 220, 'Half-Life 2', '', '', 'jf-2', 'Bob', 'pending', '', 'x', 'x')")
    conn.commit()
    conn.close()

    db.restore_from_file(str(replacement))

    rows = db.list_requests()
    assert len(rows) == 1
    assert rows[0]["name"] == "Half-Life 2"
