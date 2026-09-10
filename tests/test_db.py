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
# Email validation
# ---------------------------------------------------------------------------
def test_looks_like_email():
    import db
    assert db.looks_like_email("someone@example.com") is True
    assert db.looks_like_email("not-an-email") is False
    assert db.looks_like_email("") is False
    assert db.looks_like_email(None) is False


# ---------------------------------------------------------------------------
# Seerr contacts
# ---------------------------------------------------------------------------
def test_replace_seerr_contacts_full_replace(isolated_db):
    import db
    db.replace_seerr_contacts([
        {"jellyfin_user_id": "jf-1", "seerr_user_id": "1", "display_name": "Alice",
         "email": "alice@example.com", "discord_id": "999"},
        {"jellyfin_user_id": "jf-2", "seerr_user_id": "2", "display_name": "Bob",
         "email": "", "discord_id": ""},
    ])
    assert db.get_seerr_contact("jf-1")["email"] == "alice@example.com"
    counts = db.count_seerr_contacts()
    assert counts["total"] == 2
    assert counts["with_contact"] == 1

    # A second replace wipes the first wholesale.
    db.replace_seerr_contacts([
        {"jellyfin_user_id": "jf-3", "seerr_user_id": "3", "display_name": "Carol",
         "email": "carol@example.com", "discord_id": ""},
    ])
    assert db.get_seerr_contact("jf-1") is None
    assert db.get_seerr_contact("jf-3")["display_name"] == "Carol"


def test_get_seerr_contact_missing_returns_none(isolated_db):
    import db
    assert db.get_seerr_contact("nope") is None
    assert db.get_seerr_contact("") is None


def test_seerr_contacts_synced_at_tracks_the_latest_sync(isolated_db):
    import db
    assert db.seerr_contacts_synced_at() is None
    db.replace_seerr_contacts([{"jellyfin_user_id": "jf-1", "seerr_user_id": "1",
                               "display_name": "Alice", "email": "", "discord_id": ""}])
    assert db.seerr_contacts_synced_at() is not None
