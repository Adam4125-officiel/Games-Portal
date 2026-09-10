import notifications


def test_admin_email_recipients_prefers_the_db_setting(isolated_db, monkeypatch):
    import db
    monkeypatch.setattr(notifications.config, "SMTP_TO", "fallback@example.com")
    db.set_setting("admin_notify_email", "a@example.com, b@example.com")
    assert notifications.admin_email_recipients() == ["a@example.com", "b@example.com"]


def test_admin_email_recipients_falls_back_to_env_var(isolated_db, monkeypatch):
    monkeypatch.setattr(notifications.config, "SMTP_TO", "fallback@example.com")
    assert notifications.admin_email_recipients() == ["fallback@example.com"]


def test_normalize_recipients_cleans_mixed_separators():
    raw = "a@example.com,\n b@example.com , \nc@example.com"
    assert notifications.normalize_recipients(raw) == "a@example.com, b@example.com, c@example.com"


def test_email_configured_requires_host_from_and_recipients(isolated_db, monkeypatch):
    monkeypatch.setattr(notifications.config, "SMTP_HOST", "")
    monkeypatch.setattr(notifications.config, "SMTP_FROM", "portal@example.com")
    monkeypatch.setattr(notifications.config, "SMTP_TO", "a@example.com")
    assert notifications.email_configured() is False

    monkeypatch.setattr(notifications.config, "SMTP_HOST", "smtp.example.com")
    assert notifications.email_configured() is True


def test_notify_admin_is_a_silent_no_op_with_nothing_configured(isolated_db, monkeypatch):
    monkeypatch.setattr(notifications.config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(notifications.config, "SMTP_HOST", "")
    # Must not raise even though nothing is configured.
    notifications.notify_admin("title", "message")


def test_notify_admin_posts_to_discord_when_configured(isolated_db, monkeypatch):
    monkeypatch.setattr(notifications.config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(notifications.config, "SMTP_HOST", "")
    calls = []
    monkeypatch.setattr(notifications.requests, "post", lambda url, **k: calls.append((url, k)))
    notifications.notify_admin("New request", "Someone asked for a game.")
    assert len(calls) == 1
    assert calls[0][0] == "https://discord.example/webhook"
    assert "New request" in calls[0][1]["json"]["content"]


def test_notify_admin_swallows_discord_failures(isolated_db, monkeypatch):
    monkeypatch.setattr(notifications.config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(notifications.config, "SMTP_HOST", "")

    def boom(*a, **k):
        raise notifications.requests.ConnectionError("down")

    monkeypatch.setattr(notifications.requests, "post", boom)
    notifications.notify_admin("title", "message")  # must not raise


def test_send_email_rejects_non_email_recipients_without_raising(isolated_db, monkeypatch):
    monkeypatch.setattr(notifications.config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(notifications.config, "SMTP_FROM", "portal@example.com")
    result = notifications.send_email("subject", "body", recipients=["not-an-email"])
    assert result is False


def test_send_email_sends_through_smtp(isolated_db, monkeypatch):
    monkeypatch.setattr(notifications.config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(notifications.config, "SMTP_FROM", "portal@example.com")
    monkeypatch.setattr(notifications.config, "SMTP_USERNAME", "")

    sent = []

    class FakeSMTP:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(notifications, "_smtp_connection", lambda: FakeSMTP())
    result = notifications.send_email("Hello", "World", recipients=["someone@example.com"])
    assert result is True
    assert len(sent) == 1
    assert sent[0]["To"] == "someone@example.com"


def test_send_email_returns_false_on_smtp_failure(isolated_db, monkeypatch):
    monkeypatch.setattr(notifications.config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(notifications.config, "SMTP_FROM", "portal@example.com")

    def boom():
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr(notifications, "_smtp_connection", boom)
    result = notifications.send_email("Hello", "World", recipients=["someone@example.com"])
    assert result is False
