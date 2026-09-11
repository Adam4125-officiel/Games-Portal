import pytest
import requests

import db
import scanner
import steam


# ---------------------------------------------------------------------------
# Tag handling
# ---------------------------------------------------------------------------
def test_extract_tagged_appid_finds_the_tag_anywhere_in_the_name():
    assert scanner.extract_tagged_appid("Half-Life 2 {steamapp-220}") == 220
    assert scanner.extract_tagged_appid("{steamapp-70} Half-Life") == 70
    assert scanner.extract_tagged_appid("Half-Life 2") is None
    assert scanner.extract_tagged_appid("Half-Life 2 [steamapp-220]") is None  # wrong brackets


def test_strip_tag_removes_it_and_trims_whitespace():
    assert scanner.strip_tag("Half-Life 2 {steamapp-220}") == "Half-Life 2"
    assert scanner.strip_tag("Half-Life 2") == "Half-Life 2"


def test_tag_folder_name_replaces_rather_than_doubles_an_existing_tag():
    assert scanner.tag_folder_name("Half-Life 2", 220) == "Half-Life 2 {steamapp-220}"
    retagged = scanner.tag_folder_name("Half-Life 2 {steamapp-999}", 220)
    assert retagged == "Half-Life 2 {steamapp-220}"


def test_clean_for_search_strips_brackets_and_separators():
    assert scanner._clean_for_search("Cyberpunk.2077-FLT") == "Cyberpunk 2077 FLT"
    assert scanner._clean_for_search("Hollow_Knight [GOG] (v1.5)") == "Hollow Knight"


# ---------------------------------------------------------------------------
# Settings (DB-backed, admin-edited from /admin/scanner - see app.py)
# ---------------------------------------------------------------------------
def test_games_folders_round_trips_and_cleans_input(isolated_db):
    scanner.set_games_folders("  /mnt/games \n\n/mnt/games2\n \n/mnt/games\n")
    # Blank lines dropped, whitespace trimmed, duplicates removed, order kept.
    assert scanner.games_folders() == ["/mnt/games", "/mnt/games2"]


def test_disabled_scanner_is_a_no_op(isolated_db):
    assert scanner.games_folders() == []
    assert scanner.is_enabled() is False
    result = scanner.scan_once()
    assert result == {"scanned": 0, "matched": 0, "pending_review": 0, "unmatched": 0}
    assert db.list_scanned_folders() == []


def test_scan_interval_and_threshold_have_sane_defaults_and_are_settable(isolated_db):
    assert scanner.scan_interval_seconds() == scanner.DEFAULT_SCAN_INTERVAL_MINUTES * 60
    assert scanner.fuzzy_match_threshold() == scanner.DEFAULT_FUZZY_MATCH_THRESHOLD

    scanner.set_scan_interval_minutes(5)
    scanner.set_fuzzy_match_threshold(90)
    assert scanner.scan_interval_seconds() == 5 * 60
    assert scanner.fuzzy_match_threshold() == 90


def test_scan_interval_has_a_floor(isolated_db):
    scanner.set_scan_interval_minutes(0)  # set_scan_interval_minutes itself floors to 1
    assert scanner.scan_interval_seconds() == scanner.MIN_SCAN_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# scan_once()
# ---------------------------------------------------------------------------
@pytest.fixture
def one_root(tmp_path, isolated_db):
    scanner.set_games_folders(str(tmp_path))
    return tmp_path


def _fake_search(results_by_query):
    def search(term):
        return results_by_query.get(term, [])
    return search


def test_a_tagged_folder_is_matched_without_ever_calling_steam(one_root, monkeypatch):
    (one_root / "Half-Life 2 {steamapp-220}").mkdir()

    def boom(term):
        raise AssertionError("steam.search must not be called for a tagged folder")
    monkeypatch.setattr(steam, "search", boom)

    result = scanner.scan_once()
    assert result["matched"] == 1
    rows = db.list_scanned_folders(status="matched")
    assert rows[0]["steam_appid"] == 220
    assert rows[0]["root_path"] == str(one_root)


def test_an_untagged_folder_above_the_threshold_becomes_pending_review(one_root, monkeypatch):
    (one_root / "Half Life 2").mkdir()
    monkeypatch.setattr(steam, "search", _fake_search({
        "Half Life 2": [{"appid": 220, "name": "Half-Life 2", "icon_url": "", "short_description": ""}],
    }))
    scanner.set_fuzzy_match_threshold(50)

    result = scanner.scan_once()
    assert result["pending_review"] == 1
    row = db.list_scanned_folders(status="pending_review")[0]
    assert row["candidate_appid"] == 220
    assert row["candidate_name"] == "Half-Life 2"
    assert row["candidate_score"] >= 50


def test_fuzzy_matching_ignores_case_differences(one_root, monkeypatch):
    """Caught by hand-testing against realistic folder names: rapidfuzz's
    fuzz.WRatio is case-sensitive by default, which scored an exact match
    that only differed by case at 30/100 - well under any sane threshold -
    until scanner._best_fuzzy_candidate started passing a normalizing
    processor. This is the regression guard for that."""
    (one_root / "elden ring").mkdir()
    monkeypatch.setattr(steam, "search", _fake_search({
        "elden ring": [{"appid": 1245620, "name": "ELDEN RING", "icon_url": "", "short_description": ""}],
    }))
    scanner.set_fuzzy_match_threshold(82)

    result = scanner.scan_once()
    assert result["pending_review"] == 1
    row = db.list_scanned_folders(status="pending_review")[0]
    assert row["candidate_score"] >= 82


def test_an_untagged_folder_below_the_threshold_is_left_unmatched(one_root, monkeypatch):
    (one_root / "Totally Unrelated Folder Name").mkdir()
    monkeypatch.setattr(steam, "search", _fake_search({
        "Totally Unrelated Folder Name": [
            {"appid": 999, "name": "Something Completely Different", "icon_url": "", "short_description": ""}],
    }))
    scanner.set_fuzzy_match_threshold(95)

    result = scanner.scan_once()
    assert result["unmatched"] == 1
    assert db.list_scanned_folders(status="unmatched")


def test_a_folder_with_no_steam_results_is_unmatched(one_root, monkeypatch):
    (one_root / "Some Game").mkdir()
    monkeypatch.setattr(steam, "search", _fake_search({}))

    result = scanner.scan_once()
    assert result["unmatched"] == 1


def test_a_steam_failure_leaves_an_existing_folders_state_unchanged(one_root, monkeypatch):
    (one_root / "Half Life 2").mkdir()
    db.upsert_scanned_folder(str(one_root), "Half Life 2", "pending_review",
                              candidate_appid=220, candidate_name="Half-Life 2", candidate_score=90.0)

    def flaky_search(term):
        raise requests.ConnectionError("Steam is down")
    monkeypatch.setattr(steam, "search", flaky_search)

    scanner.scan_once()
    row = db.list_scanned_folders()[0]
    assert row["status"] == "pending_review"
    assert row["candidate_appid"] == 220


def test_an_unreadable_root_does_not_touch_its_existing_rows(isolated_db):
    db.upsert_scanned_folder("/does/not/exist", "Some Game", "matched", steam_appid=70)
    scanner.set_games_folders("/does/not/exist")

    result = scanner.scan_once()
    assert result["errors"]
    assert db.list_scanned_folders()[0]["status"] == "matched"


def test_a_folder_removed_from_disk_is_pruned_from_the_next_scan(one_root, monkeypatch):
    monkeypatch.setattr(steam, "search", _fake_search({}))
    folder = one_root / "Half-Life 2 {steamapp-220}"
    folder.mkdir()
    scanner.scan_once()
    assert len(db.list_scanned_folders()) == 1

    folder.rmdir()
    scanner.scan_once()
    assert db.list_scanned_folders() == []


def test_matched_appids_reflects_only_matched_rows(one_root, monkeypatch):
    (one_root / "Half-Life 2 {steamapp-220}").mkdir()
    (one_root / "Portal 2 {steamapp-620}").mkdir()
    scanner.scan_once()
    assert scanner.matched_appids() == {220, 620}


# ---------------------------------------------------------------------------
# Multiple root folders (e.g. one per disk)
# ---------------------------------------------------------------------------
def test_two_roots_can_each_have_a_same_named_folder(tmp_path, isolated_db, monkeypatch):
    root1 = tmp_path / "disk1"
    root2 = tmp_path / "disk2"
    (root1 / "Portal {steamapp-400}").mkdir(parents=True)
    (root2 / "Portal {steamapp-400}").mkdir(parents=True)  # same folder name, different disk
    scanner.set_games_folders(f"{root1}\n{root2}")

    result = scanner.scan_once()
    assert result["scanned"] == 2
    assert result["matched"] == 2
    rows = db.list_scanned_folders(status="matched")
    assert {row["root_path"] for row in rows} == {str(root1), str(root2)}


def test_one_unreadable_root_does_not_block_scanning_the_others(tmp_path, isolated_db, monkeypatch):
    good_root = tmp_path / "disk1"
    bad_root = tmp_path / "does-not-exist"
    (good_root / "Half-Life 2 {steamapp-220}").mkdir(parents=True)
    scanner.set_games_folders(f"{good_root}\n{bad_root}")

    result = scanner.scan_once()
    assert result["matched"] == 1
    assert result["errors"]
    assert str(bad_root) in result["errors"][0]


def test_removing_a_root_from_config_prunes_its_rows(tmp_path, isolated_db, monkeypatch):
    monkeypatch.setattr(steam, "search", _fake_search({}))
    root1 = tmp_path / "disk1"
    root2 = tmp_path / "disk2"
    (root1 / "Half-Life 2 {steamapp-220}").mkdir(parents=True)
    (root2 / "Portal 2 {steamapp-620}").mkdir(parents=True)
    scanner.set_games_folders(f"{root1}\n{root2}")
    scanner.scan_once()
    assert len(db.list_scanned_folders()) == 2

    # Admin removes disk2 from the configured list entirely (not "disk unplugged" -
    # a deliberate configuration change, safe to prune even without re-listing it).
    scanner.set_games_folders(str(root1))
    scanner.scan_once()
    rows = db.list_scanned_folders()
    assert len(rows) == 1
    assert rows[0]["root_path"] == str(root1)


# ---------------------------------------------------------------------------
# confirm_match()
# ---------------------------------------------------------------------------
@pytest.fixture
def unmatched_folder(one_root):
    (one_root / "Half Life 2").mkdir()
    db.upsert_scanned_folder(str(one_root), "Half Life 2", "pending_review",
                              candidate_appid=220, candidate_name="Half-Life 2",
                              candidate_score=91.0)
    row = [r for r in db.list_scanned_folders() if r["folder_name"] == "Half Life 2"][0]
    return one_root, row["id"]


def test_confirm_match_renames_the_folder_and_marks_it_matched(unmatched_folder, monkeypatch):
    root, row_id = unmatched_folder
    monkeypatch.setattr(steam, "fetch_app_summary",
                         lambda appid: {"appid": appid, "name": "Half-Life 2", "icon_url": "",
                                        "short_description": ""})

    result = scanner.confirm_match(row_id, 220)
    assert result["folder_name"] == "Half Life 2 {steamapp-220}"
    assert result["root_path"] == str(root)
    assert not (root / "Half Life 2").exists()
    assert (root / "Half Life 2 {steamapp-220}").is_dir()

    row = db.get_scanned_folder(row_id)
    assert row["status"] == "matched"
    assert row["steam_appid"] == 220
    assert row["candidate_appid"] is None


def test_confirm_match_refuses_an_invalid_appid(unmatched_folder, monkeypatch):
    root, row_id = unmatched_folder
    monkeypatch.setattr(steam, "fetch_app_summary", lambda appid: None)

    with pytest.raises(scanner.ScannerError, match="doesn't resolve"):
        scanner.confirm_match(row_id, 999999999)
    assert (root / "Half Life 2").is_dir()  # untouched


def test_confirm_match_refuses_when_the_folder_is_gone(unmatched_folder, monkeypatch):
    root, row_id = unmatched_folder
    (root / "Half Life 2").rmdir()
    monkeypatch.setattr(steam, "fetch_app_summary",
                         lambda appid: {"appid": appid, "name": "x", "icon_url": "", "short_description": ""})

    with pytest.raises(scanner.ScannerError, match="no longer exists"):
        scanner.confirm_match(row_id, 220)


def test_confirm_match_refuses_a_rename_that_would_collide(unmatched_folder, monkeypatch):
    root, row_id = unmatched_folder
    (root / "Half Life 2 {steamapp-220}").mkdir()
    monkeypatch.setattr(steam, "fetch_app_summary",
                         lambda appid: {"appid": appid, "name": "x", "icon_url": "", "short_description": ""})

    with pytest.raises(scanner.ScannerError, match="already exists"):
        scanner.confirm_match(row_id, 220)


def test_confirm_match_refuses_a_folder_name_that_escapes_its_root(one_root, monkeypatch):
    row_id = db.upsert_scanned_folder(str(one_root), "../escape", "unmatched")
    monkeypatch.setattr(steam, "fetch_app_summary",
                         lambda appid: {"appid": appid, "name": "x", "icon_url": "", "short_description": ""})

    with pytest.raises(scanner.ScannerError, match="unsafe"):
        scanner.confirm_match(row_id, 220)


def test_confirm_match_refuses_a_root_no_longer_configured(unmatched_folder, monkeypatch):
    root, row_id = unmatched_folder
    scanner.set_games_folders("")  # admin removed every configured folder
    monkeypatch.setattr(steam, "fetch_app_summary",
                         lambda appid: {"appid": appid, "name": "x", "icon_url": "", "short_description": ""})

    with pytest.raises(scanner.ScannerError, match="isn't configured"):
        scanner.confirm_match(row_id, 220)


def test_confirm_match_refuses_an_unknown_row(one_root, monkeypatch):
    with pytest.raises(scanner.ScannerError, match="isn't in the scanner's list"):
        scanner.confirm_match(999999, 220)
