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
