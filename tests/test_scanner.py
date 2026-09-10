import os

import pytest

import scanner


@pytest.fixture
def games_dir(tmp_path, monkeypatch):
    root = tmp_path / "games"
    root.mkdir()
    monkeypatch.setattr(scanner.config, "GAMES_FOLDER", str(root))
    return root


# ---------------------------------------------------------------------------
# AppID extraction
# ---------------------------------------------------------------------------
def test_extract_appid_from_bracket_tag(games_dir):
    folder = games_dir / "Half-Life 2 [220]"
    folder.mkdir()
    assert scanner._extract_appid(str(folder), folder.name) == 220


def test_extract_appid_from_paren_tag(games_dir):
    folder = games_dir / "Half-Life 2 (220)"
    folder.mkdir()
    assert scanner._extract_appid(str(folder), folder.name) == 220


def test_extract_appid_from_sidecar_file(games_dir):
    folder = games_dir / "Half-Life 2"
    folder.mkdir()
    (folder / ".steam-appid").write_text("220")
    assert scanner._extract_appid(str(folder), folder.name) == 220


def test_extract_appid_sidecar_wins_over_stale_tag(games_dir):
    folder = games_dir / "Half-Life 2 [999]"
    folder.mkdir()
    (folder / ".steam-appid").write_text("220")
    assert scanner._extract_appid(str(folder), folder.name) == 220


def test_extract_appid_returns_none_without_a_tag_or_sidecar(games_dir):
    folder = games_dir / "Half-Life 2"
    folder.mkdir()
    assert scanner._extract_appid(str(folder), folder.name) is None


def test_untagged_name_strips_the_tag():
    assert scanner._untagged_name("Half-Life 2 [220]") == "Half-Life 2"
    assert scanner._untagged_name("Half-Life 2") == "Half-Life 2"


# ---------------------------------------------------------------------------
# scan_once
# ---------------------------------------------------------------------------
def test_scan_disabled_without_a_configured_folder(isolated_db, monkeypatch):
    monkeypatch.setattr(scanner.config, "GAMES_FOLDER", "")
    result = scanner.scan_once()
    assert result["ok"] is False


def test_scan_exact_match_marks_the_request_done(isolated_db, games_dir):
    import db
    rid = db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    (games_dir / "Half-Life 2 [220]").mkdir()

    result = scanner.scan_once()
    assert result["exact_matches"] == 1
    assert db.get_request(rid)["status"] == "done"
    assert db.is_installed(220) is True


def test_scan_exact_match_without_a_pending_request_just_records_installed(isolated_db, games_dir):
    import db
    (games_dir / "Some Game [12345]").mkdir()
    scanner.scan_once()
    assert db.is_installed(12345) is True


def test_scan_fuzzy_match_creates_a_pending_suggestion_not_an_auto_resolve(isolated_db, games_dir):
    import db
    rid = db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    (games_dir / "Half Life 2").mkdir()  # close enough, no exact tag

    result = scanner.scan_once()
    assert result["suggestions"] == 1
    # Never auto-resolved:
    assert db.get_request(rid)["status"] == "pending"
    matches = db.list_pending_scan_matches()
    assert len(matches) == 1
    assert matches[0]["request_id"] == rid


def test_scan_does_not_suggest_below_the_threshold(isolated_db, games_dir, monkeypatch):
    import db
    db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    (games_dir / "Totally Unrelated Folder Name").mkdir()
    monkeypatch.setattr(scanner.config, "SCANNER_FUZZY_THRESHOLD", 75)

    scanner.scan_once()
    assert db.list_pending_scan_matches() == []


def test_scan_does_not_resuggest_a_rejected_pairing(isolated_db, games_dir):
    import db
    rid = db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    folder = games_dir / "Half Life 2"
    folder.mkdir()

    scanner.scan_once()
    match = db.list_pending_scan_matches()[0]
    scanner.reject_match(match["id"])

    scanner.scan_once()
    assert db.list_pending_scan_matches() == []


def test_scan_ignores_rejected_requests_for_exact_matches(isolated_db, games_dir):
    import db
    rid = db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    db.update_request_status(rid, "rejected", "")
    (games_dir / "Half-Life 2 [220]").mkdir()

    scanner.scan_once()
    # Still recorded as installed (for the "already installed" badge)...
    assert db.is_installed(220) is True
    # ...but the rejection is not silently overridden.
    assert db.get_request(rid)["status"] == "rejected"


# ---------------------------------------------------------------------------
# confirm_match / reject_match
# ---------------------------------------------------------------------------
def test_confirm_match_renames_the_folder_and_marks_done(isolated_db, games_dir):
    import db
    rid = db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    folder = games_dir / "Half Life 2"
    folder.mkdir()
    scanner.scan_once()
    match = db.list_pending_scan_matches()[0]

    new_name = scanner.confirm_match(match["id"])
    assert new_name == "Half Life 2 [220]"
    assert not folder.exists()
    assert (games_dir / new_name).is_dir()
    assert db.get_request(rid)["status"] == "done"
    assert db.is_installed(220) is True
    assert db.list_pending_scan_matches() == []


def test_confirm_match_is_idempotent_to_a_second_scan(isolated_db, games_dir):
    import db
    db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    folder = games_dir / "Half Life 2"
    folder.mkdir()
    scanner.scan_once()
    match = db.list_pending_scan_matches()[0]
    scanner.confirm_match(match["id"])

    # A re-scan of the renamed folder should find it as an exact match, not
    # generate a new suggestion.
    result = scanner.scan_once()
    assert result["suggestions"] == 0
    assert result["exact_matches"] == 1


def test_confirm_match_fails_gracefully_for_a_missing_folder(isolated_db, games_dir):
    import db
    db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    folder = games_dir / "Half Life 2"
    folder.mkdir()
    scanner.scan_once()
    match = db.list_pending_scan_matches()[0]

    import shutil
    shutil.rmtree(folder)
    with pytest.raises(scanner.ScanError):
        scanner.confirm_match(match["id"])


def test_confirm_match_unknown_id_raises(isolated_db, games_dir):
    with pytest.raises(scanner.ScanError):
        scanner.confirm_match(999999)


def test_reject_match_marks_it_rejected_not_deleted(isolated_db, games_dir):
    import db
    db.create_request(220, "Half-Life 2", "", "", "jf-1", "Alice")
    (games_dir / "Half Life 2").mkdir()
    scanner.scan_once()
    match = db.list_pending_scan_matches()[0]

    scanner.reject_match(match["id"])
    assert db.list_pending_scan_matches() == []
    assert db.get_scan_match(match["id"])["status"] == "rejected"


def test_reject_match_unknown_id_raises(isolated_db, games_dir):
    with pytest.raises(scanner.ScanError):
        scanner.reject_match(999999)
