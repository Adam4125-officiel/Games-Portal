"""Regression coverage for config._load_or_create_secret_key()'s persistence -
see docs/HISTORY.md for the "random disconnects" investigation this came out of.
A key that fails to persist still works for the process that generated it, so
nothing looks wrong in the moment; the bug only shows up as every session being
invalidated on the *next* restart, which is exactly the kind of thing that must
never fail silently."""
import logging

import config


def test_secret_key_persistence_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY_FILE", str(tmp_path / "secret_key"))
    monkeypatch.delenv("PORTAL_SECRET_KEY", raising=False)

    first = config._load_or_create_secret_key()
    second = config._load_or_create_secret_key()

    assert first == second
    assert (tmp_path / "secret_key").is_file()


def test_a_persist_failure_is_logged_rather_than_silently_swallowed(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config, "SECRET_KEY_FILE", str(tmp_path / "nested" / "secret_key"))
    monkeypatch.delenv("PORTAL_SECRET_KEY", raising=False)

    def broken_open(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(config.os, "open", broken_open)

    with caplog.at_level(logging.ERROR):
        key = config._load_or_create_secret_key()

    assert key  # still returns a usable (if ephemeral) key for this process
    assert any("Could not persist" in record.message for record in caplog.records)


def test_a_transient_read_failure_is_retried_before_generating_a_new_key(tmp_path, monkeypatch):
    """A cloud-synced folder (OneDrive, Dropbox) or antivirus real-time
    scanning can briefly lock a just-written file on Windows - a momentary
    read failure must not be treated as "the key is gone," which would
    silently sign out every existing session for no real reason."""
    key_path = tmp_path / "secret_key"
    key_path.write_text("original-persisted-key", encoding="utf-8")
    monkeypatch.setattr(config, "SECRET_KEY_FILE", str(key_path))
    monkeypatch.setattr(config, "SECRET_KEY_READ_RETRY_DELAY_SECONDS", 0)  # keep the test fast
    monkeypatch.delenv("PORTAL_SECRET_KEY", raising=False)

    real_open = open
    calls = {"n": 0}

    def flaky_open(path, *args, **kwargs):
        if str(path) == str(key_path):
            calls["n"] += 1
            if calls["n"] < config.SECRET_KEY_READ_RETRY_ATTEMPTS:
                raise OSError("file is in use by another process")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)

    key = config._load_or_create_secret_key()

    assert key == "original-persisted-key"  # rode out the transient failure
    assert calls["n"] == config.SECRET_KEY_READ_RETRY_ATTEMPTS


def test_a_persistent_read_failure_falls_back_to_a_new_key_and_warns(tmp_path, monkeypatch, caplog):
    key_path = tmp_path / "secret_key"
    key_path.write_text("original-persisted-key", encoding="utf-8")
    monkeypatch.setattr(config, "SECRET_KEY_FILE", str(key_path))
    monkeypatch.setattr(config, "SECRET_KEY_READ_RETRY_DELAY_SECONDS", 0)
    monkeypatch.delenv("PORTAL_SECRET_KEY", raising=False)

    real_open = open

    def flaky_open(path, *args, **kwargs):
        if str(path) == str(key_path):
            raise OSError("file is in use by another process")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)

    with caplog.at_level(logging.WARNING):
        key = config._load_or_create_secret_key()

    assert key != "original-persisted-key"  # gave up and generated a new one
    assert any("Could not read" in record.message for record in caplog.records)
