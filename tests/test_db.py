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
