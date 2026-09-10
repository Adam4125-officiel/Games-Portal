"""
Tests for updater.py.

Everything that touches the filesystem runs against a throwaway "app directory"
under tmp_path - config.APP_ROOT and every path derived from it are
monkeypatched, so no test can ever write into the real repository. Nothing here
makes a network call (requests is always mocked) and nothing here ever restarts
anything.
"""
import io
import os
import zipfile

import pytest

import config
import db
import updater


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_app_dir(tmp_path, monkeypatch, isolated_db):
    """A stand-in app directory with the files an update would replace, plus
    the two things an update must never touch."""
    root = tmp_path / "app"
    (root / "templates").mkdir(parents=True)
    (root / "instance").mkdir()
    (root / "app.py").write_text("old app\n")
    (root / "VERSION").write_text("1.0.0\n")
    (root / "requirements.txt").write_text("Flask>=3.0\n")
    (root / "templates" / "index.html").write_text("<p>old</p>")
    # The protected pair - assertions below check these are byte-identical afterwards.
    (root / "instance" / "portal.db").write_text("PRECIOUS DATABASE")
    (root / ".env").write_text("PORTAL_SECRET_KEY=secret")

    monkeypatch.setattr(config, "APP_ROOT", str(root))
    monkeypatch.setattr(config, "VERSION", "1.0.0")
    monkeypatch.setattr(config, "VERSION_DISPLAY", "1.0.0")
    monkeypatch.setattr(config, "IS_GIT_CHECKOUT", False)
    monkeypatch.setattr(updater, "INSTANCE_DIR", str(root / "instance"))
    monkeypatch.setattr(updater, "BACKUP_ROOT", str(root / "instance" / "update_backups"))
    monkeypatch.setattr(updater, "PENDING_MARKER_PATH", str(root / "instance" / "update_pending.json"))
    updater._update_cache["result"] = None
    updater._update_cache["refreshed_monotonic"] = None
    return root


def make_zip(files, prefix=""):
    """A release archive in memory. prefix="" mimics `git archive` (files at
    the root, which is how this project's releases are built); a non-empty
    prefix mimics GitHub's auto-generated zipball."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(prefix + name, content)
    return buffer.getvalue()


def release_payload(version, prerelease=False, data=b"", name=None):
    return {
        "tag_name": f"v{version}",
        "name": name or f"v{version}",
        "prerelease": prerelease,
        "draft": False,
        "published_at": "2026-08-10T12:00:00Z",
        "html_url": f"https://github.com/x/y/releases/tag/v{version}",
        "body": "notes",
        "assets": [{
            "name": f"games-portal-v{version}.zip",
            "browser_download_url": f"https://github.com/x/y/releases/download/v{version}/a.zip",
            "size": len(data),
        }],
        "zipball_url": f"https://api.github.com/repos/x/y/zipball/v{version}",
    }


class FakeResponse:
    def __init__(self, json_data=None, content=b"", url="https://objects.githubusercontent.com/a.zip"):
        self._json = json_data
        self.content = content
        self.url = url

    def raise_for_status(self):
        return None

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


# ---------------------------------------------------------------------------
# Version parsing / comparison
# ---------------------------------------------------------------------------
def test_parse_version_orders_prereleases_below_their_final_release():
    rc2 = updater.parse_version("v1.5.0-rc.2")
    final = updater.parse_version("v1.5.0")
    previous = updater.parse_version("v1.4.9")
    assert previous < rc2 < final


def test_parse_version_sorts_garbage_to_the_bottom_instead_of_raising():
    assert updater.parse_version("not-a-version") < updater.parse_version("v0.0.1")
    assert updater.parse_version(None) == (0, 0, 0, 0, 0)
    assert updater.parse_version("") == (0, 0, 0, 0, 0)


# ---------------------------------------------------------------------------
# Channel / settings
# ---------------------------------------------------------------------------
def test_stable_channel_ignores_prereleases_and_unstable_includes_them(monkeypatch, isolated_db):
    releases = [release_payload("2.0.0-rc.1", prerelease=True), release_payload("1.0.0")]
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(json_data=releases))
    stable = updater.fetch_releases("stable")
    assert [r["version"] for r in stable] == ["1.0.0"]
    unstable = updater.fetch_releases("unstable")
    assert [r["version"] for r in unstable] == ["2.0.0-rc.1", "1.0.0"]


def test_latest_release_is_picked_by_version_not_publish_order(monkeypatch, isolated_db):
    # Listed oldest-first by the (fake) API, which is the opposite of GitHub's
    # usual order - the point is that this must not matter.
    releases = [release_payload("1.0.0"), release_payload("2.0.0")]
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(json_data=releases))
    assert updater.fetch_latest_release("stable")["version"] == "2.0.0"


def test_drafts_are_never_offered(monkeypatch, isolated_db):
    releases = [dict(release_payload("9.9.9"), draft=True), release_payload("1.0.0")]
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(json_data=releases))
    assert updater.fetch_latest_release("stable")["version"] == "1.0.0"


def test_get_channel_falls_back_to_stable_when_the_database_is_unreadable(monkeypatch, isolated_db):
    monkeypatch.setattr(db, "get_setting", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert updater.get_channel() == "stable"


def test_set_channel_rejects_an_unknown_channel(isolated_db):
    with pytest.raises(updater.UpdateError):
        updater.set_channel("bogus")


def test_reading_settings_never_creates_a_stray_database(tmp_path, monkeypatch):
    fake_path = str(tmp_path / "does_not_exist.db")
    monkeypatch.setattr(db, "DB_PATH", fake_path)
    assert updater.get_channel() == "stable"
    assert updater.update_check_enabled() is True
    assert not os.path.exists(fake_path)


def test_set_channel_without_a_database_is_an_explainable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "does_not_exist.db"))
    with pytest.raises(updater.UpdateError):
        updater.set_channel("unstable")


# ---------------------------------------------------------------------------
# check_for_update
# ---------------------------------------------------------------------------
def test_check_for_update_reports_an_available_update(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: FakeResponse(json_data=[release_payload("2.0.0")]))
    result = updater.check_for_update("stable")
    assert result["ok"] and result["update_available"] and not result["ahead"]
    assert result["latest"] == "2.0.0"


def test_check_for_update_reports_up_to_date(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: FakeResponse(json_data=[release_payload("1.0.0")]))
    result = updater.check_for_update("stable")
    assert result["ok"] and not result["update_available"] and not result["ahead"]


def test_check_for_update_reports_running_ahead_separately_from_up_to_date(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: FakeResponse(json_data=[release_payload("0.9.0")]))
    result = updater.check_for_update("stable")
    assert result["ok"] and result["ahead"] and not result["update_available"]


def test_check_for_update_degrades_gracefully_when_github_is_unreachable(monkeypatch, fake_app_dir):
    def boom(*a, **k):
        raise updater.requests.ConnectionError("no route to host")
    monkeypatch.setattr(updater.requests, "get", boom)
    result = updater.check_for_update("stable")
    assert result["ok"] is False
    assert result["error"]


def test_cache_is_only_refreshed_once_per_ttl(monkeypatch, fake_app_dir):
    calls = []

    def fake_get(*a, **k):
        calls.append(1)
        return FakeResponse(json_data=[release_payload("1.0.0")])

    monkeypatch.setattr(updater.requests, "get", fake_get)
    updater.refresh_update_cache_if_stale(ttl_seconds=3600)
    updater.refresh_update_cache_if_stale(ttl_seconds=3600)
    assert len(calls) == 1
    updater.refresh_update_cache_if_stale(ttl_seconds=3600, force=True)
    assert len(calls) == 2


def test_background_refresh_is_skipped_when_automatic_checking_is_off(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: FakeResponse(json_data=[release_payload("1.0.0")]))
    updater.set_update_check_enabled(False)
    assert updater.refresh_update_cache_if_stale() is None
    assert updater.get_cached_update_status() is None


# ---------------------------------------------------------------------------
# Download validation
# ---------------------------------------------------------------------------
def test_download_refuses_plain_http_and_unknown_hosts():
    with pytest.raises(updater.UpdateError):
        updater._validate_download_url("http://github.com/a.zip", "test")
    with pytest.raises(updater.UpdateError):
        updater._validate_download_url("https://evil.example.com/a.zip", "test")


def test_download_rejects_a_redirect_to_an_unexpected_host(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: FakeResponse(content=b"x", url="https://evil.example.com/a.zip"))
    with pytest.raises(updater.UpdateError):
        updater._download_asset({"url": "https://github.com/a.zip"}, lambda m: None)


def test_download_rejects_a_size_mismatch(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(content=b"12345"))
    with pytest.raises(updater.UpdateError):
        updater._download_asset({"url": "https://github.com/a.zip", "size": 999}, lambda m: None)


def test_download_rejects_a_sha256_mismatch(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(content=b"12345"))
    with pytest.raises(updater.UpdateError):
        updater._download_asset(
            {"url": "https://github.com/a.zip", "digest": "sha256:" + "0" * 64}, lambda m: None)


def test_download_accepts_a_matching_sha256(monkeypatch, fake_app_dir):
    import hashlib
    payload = b"hello world"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(content=payload))
    data, got_digest = updater._download_asset(
        {"url": "https://github.com/a.zip", "size": len(payload), "digest": f"sha256:{digest}"},
        lambda m: None)
    assert data == payload and got_digest == digest


def test_download_is_capped_so_a_runaway_response_cannot_fill_the_disk(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater, "MAX_DOWNLOAD_BYTES", 10)
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(content=b"x" * 100))
    with pytest.raises(updater.UpdateError):
        updater._download_asset({"url": "https://github.com/a.zip"}, lambda m: None)


# ---------------------------------------------------------------------------
# Archive inspection
# ---------------------------------------------------------------------------
def test_git_archive_layout_is_read_as_is(fake_app_dir):
    data = make_zip({"VERSION": b"2.0.0\n", "app.py": b"new app\n"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = updater._archive_members(zf)
    assert {name for _, name in members} == {"VERSION", "app.py"}


def test_github_zipball_top_level_directory_is_stripped(fake_app_dir):
    data = make_zip({"VERSION": b"2.0.0\n", "app.py": b"new app\n"}, prefix="owner-repo-abc123/")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = updater._archive_members(zf)
    assert {name for _, name in members} == {"VERSION", "app.py"}


@pytest.mark.parametrize("bad_name", ["../evil.py", "a/../../evil.py", "/etc/passwd"])
def test_archive_with_a_parent_directory_path_is_refused(fake_app_dir, bad_name):
    data = make_zip({bad_name: b"evil"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        with pytest.raises(updater.UpdateError):
            updater._archive_members(zf)


@pytest.mark.parametrize("protected", ["instance/portal.db", "instance/update_backups/x", ".env"])
def test_archive_containing_a_protected_path_aborts_the_whole_update(fake_app_dir, protected):
    data = make_zip({"VERSION": b"2.0.0", protected: b"evil"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        with pytest.raises(updater.UpdateError):
            updater._archive_members(zf)


def test_archive_member_limit_is_enforced(fake_app_dir, monkeypatch):
    monkeypatch.setattr(updater, "MAX_ARCHIVE_MEMBERS", 2)
    data = make_zip({"a": b"1", "b": b"2", "c": b"3"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        with pytest.raises(updater.UpdateError):
            updater._archive_members(zf)


def test_empty_archive_is_refused(fake_app_dir):
    data = make_zip({})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        with pytest.raises(updater.UpdateError):
            updater._archive_members(zf)


# ---------------------------------------------------------------------------
# perform_update
# ---------------------------------------------------------------------------
def _stub_release_fetch(monkeypatch, version, archive_files, prerelease=False):
    data = make_zip(archive_files)
    payload = release_payload(version, prerelease=prerelease, data=data)

    def fake_get(url, **kwargs):
        if "api.github.com" in url:
            return FakeResponse(json_data=[payload])
        return FakeResponse(content=data)

    monkeypatch.setattr(updater.requests, "get", fake_get)
    return data


def test_update_refuses_to_overwrite_a_git_checkout(monkeypatch, fake_app_dir):
    monkeypatch.setattr(config, "IS_GIT_CHECKOUT", True)
    _stub_release_fetch(monkeypatch, "2.0.0", {"VERSION": b"2.0.0"})
    with pytest.raises(updater.UpdateError):
        updater.perform_update(progress=lambda m: None)


def test_update_is_a_no_op_when_already_up_to_date(monkeypatch, fake_app_dir):
    _stub_release_fetch(monkeypatch, "1.0.0", {"VERSION": b"1.0.0"})
    result = updater.perform_update(progress=lambda m: None)
    assert result["applied"] is False


def test_update_is_a_no_op_when_running_ahead_of_the_channel(monkeypatch, fake_app_dir):
    _stub_release_fetch(monkeypatch, "0.5.0", {"VERSION": b"0.5.0"})
    result = updater.perform_update(progress=lambda m: None)
    assert result["applied"] is False


def test_update_replaces_files_and_never_touches_instance_or_env(monkeypatch, fake_app_dir):
    _stub_release_fetch(monkeypatch, "2.0.0", {
        "VERSION": b"2.0.0\n",
        "app.py": b"new app\n",
        "requirements.txt": b"Flask>=3.0\n",
    })
    result = updater.perform_update(progress=lambda m: None, install_deps=False)
    assert result["applied"] is True
    assert (fake_app_dir / "app.py").read_text() == "new app\n"
    assert (fake_app_dir / "VERSION").read_text() == "2.0.0\n"
    assert (fake_app_dir / "instance" / "portal.db").read_text() == "PRECIOUS DATABASE"
    assert (fake_app_dir / ".env").read_text() == "PORTAL_SECRET_KEY=secret"


def test_update_is_safe_to_run_twice(monkeypatch, fake_app_dir):
    _stub_release_fetch(monkeypatch, "2.0.0", {"VERSION": b"2.0.0\n", "app.py": b"new app\n"})
    updater.perform_update(progress=lambda m: None, install_deps=False)
    result = updater.perform_update(progress=lambda m: None, install_deps=False, force=True)
    assert result["applied"] is True
    assert (fake_app_dir / "app.py").read_text() == "new app\n"


def test_update_takes_a_backup_of_every_file_it_replaces(monkeypatch, fake_app_dir):
    _stub_release_fetch(monkeypatch, "2.0.0", {"VERSION": b"2.0.0\n", "app.py": b"new app\n"})
    result = updater.perform_update(progress=lambda m: None, install_deps=False)
    backups = updater.list_backups()
    assert len(backups) == 1
    assert backups[0]["name"] == result["backup"]
    assert "app.py" in backups[0]["replaced"]
    backup_app_py = os.path.join(backups[0]["path"], "app.py")
    assert open(backup_app_py).read() == "old app\n"


def test_a_failed_write_part_way_through_rolls_the_whole_update_back(monkeypatch, fake_app_dir):
    _stub_release_fetch(monkeypatch, "2.0.0", {
        "VERSION": b"2.0.0\n", "app.py": b"new app\n", "extra.py": b"new extra\n",
    })
    real_atomic_write = updater._atomic_write
    calls = {"n": 0}

    def flaky_write(destination, data, mode=None):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("disk exploded")
        return real_atomic_write(destination, data, mode)

    monkeypatch.setattr(updater, "_atomic_write", flaky_write)
    with pytest.raises(updater.UpdateError):
        updater.perform_update(progress=lambda m: None, install_deps=False)
    # Rolled back: original content intact, no leftover pending marker.
    assert (fake_app_dir / "app.py").read_text() == "old app\n"
    assert updater.read_pending_marker() is None


def test_dependencies_are_installed_only_when_requirements_changed(monkeypatch, fake_app_dir):
    calls = []
    monkeypatch.setattr(updater, "_install_dependencies", lambda progress: calls.append(1))
    _stub_release_fetch(monkeypatch, "2.0.0", {"VERSION": b"2.0.0\n", "requirements.txt": b"Flask>=3.0\n"})
    updater.perform_update(progress=lambda m: None)
    assert calls == []

    updater._update_cache["result"] = None
    _stub_release_fetch(monkeypatch, "2.0.1", {"VERSION": b"2.0.1\n", "requirements.txt": b"Flask>=4.0\n"})
    monkeypatch.setattr(config, "VERSION", "2.0.0")
    result = updater.perform_update(progress=lambda m: None, force=True)
    assert calls == [1]
    assert result["deps_changed"] is True


def test_a_failed_dependency_install_rolls_the_update_back(monkeypatch, fake_app_dir):
    def boom(progress):
        raise updater.UpdateError("pip exploded")
    monkeypatch.setattr(updater, "_install_dependencies", boom)
    _stub_release_fetch(monkeypatch, "2.0.0", {"VERSION": b"2.0.0\n", "requirements.txt": b"Flask>=4.0\n"})
    with pytest.raises(updater.UpdateError):
        updater.perform_update(progress=lambda m: None)
    assert (fake_app_dir / "requirements.txt").read_text() == "Flask>=3.0\n"


def test_install_deps_can_be_skipped(monkeypatch, fake_app_dir):
    calls = []
    monkeypatch.setattr(updater, "_install_dependencies", lambda progress: calls.append(1))
    _stub_release_fetch(monkeypatch, "2.0.0", {"VERSION": b"2.0.0\n", "requirements.txt": b"Flask>=4.0\n"})
    updater.perform_update(progress=lambda m: None, install_deps=False)
    assert calls == []


def test_old_backups_are_pruned(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater, "KEEP_BACKUPS", 2)
    for i, version in enumerate(["2.0.0", "3.0.0", "4.0.0"]):
        updater._update_cache["result"] = None
        _stub_release_fetch(monkeypatch, version, {"VERSION": f"{version}\n".encode()})
        updater.perform_update(progress=lambda m: None, install_deps=False, force=True)
        monkeypatch.setattr(config, "VERSION", version)
    assert len(updater.list_backups()) == 2


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------
def test_rollback_restores_replaced_files_and_removes_added_ones(monkeypatch, fake_app_dir):
    _stub_release_fetch(monkeypatch, "2.0.0", {
        "VERSION": b"2.0.0\n", "app.py": b"new app\n", "brand_new_file.py": b"added\n",
    })
    updater.perform_update(progress=lambda m: None, install_deps=False)
    updater.rollback(progress=lambda m: None)
    assert (fake_app_dir / "app.py").read_text() == "old app\n"
    assert not (fake_app_dir / "brand_new_file.py").exists()


def test_rollback_can_target_a_named_backup(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater, "_install_dependencies", lambda progress: None)
    _stub_release_fetch(monkeypatch, "2.0.0", {"VERSION": b"2.0.0\n", "app.py": b"v2\n"})
    result1 = updater.perform_update(progress=lambda m: None, install_deps=False)
    monkeypatch.setattr(config, "VERSION", "2.0.0")
    updater._update_cache["result"] = None
    _stub_release_fetch(monkeypatch, "3.0.0", {"VERSION": b"3.0.0\n", "app.py": b"v3\n"})
    updater.perform_update(progress=lambda m: None, install_deps=False, force=True)

    updater.rollback(backup_name=result1["backup"], progress=lambda m: None)
    assert (fake_app_dir / "app.py").read_text() == "old app\n"


def test_rollback_with_no_backups_is_an_explainable_error(fake_app_dir):
    with pytest.raises(updater.UpdateError):
        updater.rollback()


def test_rollback_rejects_an_unknown_backup_name(monkeypatch, fake_app_dir):
    _stub_release_fetch(monkeypatch, "2.0.0", {"VERSION": b"2.0.0\n"})
    updater.perform_update(progress=lambda m: None, install_deps=False)
    with pytest.raises(updater.UpdateError):
        updater.rollback(backup_name="does-not-exist")


# ---------------------------------------------------------------------------
# Pending marker
# ---------------------------------------------------------------------------
def test_pending_marker_is_cleared_when_the_app_comes_back_on_the_new_version(fake_app_dir, monkeypatch):
    updater.write_pending_marker("some-backup", "2.0.0")
    monkeypatch.setattr(config, "VERSION", "2.0.0")
    result = updater.check_pending_marker()
    assert result["status"] == "confirmed"
    assert updater.read_pending_marker() is None


def test_pending_marker_survives_and_logs_when_the_version_did_not_change(fake_app_dir, caplog):
    updater.write_pending_marker("some-backup", "2.0.0")
    result = updater.check_pending_marker()
    assert result["status"] == "mismatch"
    assert updater.read_pending_marker() is not None


def test_check_pending_marker_is_a_no_op_without_a_marker(fake_app_dir):
    assert updater.check_pending_marker() is None


def test_rollback_clears_a_pending_marker(monkeypatch, fake_app_dir):
    _stub_release_fetch(monkeypatch, "2.0.0", {"VERSION": b"2.0.0\n"})
    result = updater.perform_update(progress=lambda m: None, install_deps=False)
    updater.write_pending_marker(result["backup"], "2.0.0")
    updater.rollback(progress=lambda m: None)
    assert updater.read_pending_marker() is None
