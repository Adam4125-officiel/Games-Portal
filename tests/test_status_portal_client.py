import pytest

import status_portal_client


class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def test_notify_new_request_enqueues_a_job(isolated_db):
    status_portal_client.notify_new_request("Half-Life", "Alice")
    job = status_portal_client._queue.get_nowait()
    assert job["path"] == "/api/notify/admin"
    assert job["payload"]["event"] == "game_request_new"
    assert "Half-Life" in job["payload"]["subject"]
    assert "Alice" in job["payload"]["body"]


def test_notify_status_changed_enqueues_a_job_when_a_jellyfin_id_is_set(isolated_db):
    status_portal_client.notify_status_changed("jf-1", "Half-Life", "approved")
    job = status_portal_client._queue.get_nowait()
    assert job["path"] == "/api/notify/user"
    assert job["payload"] == {
        "jellyfin_user_id": "jf-1", "event": "gamesportal_request",
        "subject": '"Half-Life" is now approved', "body": 'Your request for "Half-Life" is now approved.',
    }


def test_notify_status_changed_skips_when_no_jellyfin_id(isolated_db):
    status_portal_client.notify_status_changed("", "Half-Life", "approved")
    status_portal_client.notify_status_changed(None, "Half-Life", "approved")
    assert status_portal_client._queue.empty()


def test_deliver_skips_when_not_configured(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(status_portal_client.requests, "post", lambda *a, **k: calls.append((a, k)))
    status_portal_client._deliver({"path": "/api/notify/admin", "payload": {"subject": "x", "body": "y"}})
    assert calls == []


def test_deliver_posts_with_the_api_key_header_when_configured(isolated_db, monkeypatch):
    import db
    db.set_status_portal_notify_url("http://status-portal.local/")
    db.set_status_portal_notify_api_key("sp-key")

    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _FakeResponse(status_code=200)

    monkeypatch.setattr(status_portal_client.requests, "post", fake_post)
    status_portal_client._deliver({"path": "/api/notify/admin", "payload": {"subject": "x", "body": "y"}})

    assert len(calls) == 1
    assert calls[0]["url"] == "http://status-portal.local/api/notify/admin"
    assert calls[0]["headers"] == {"X-Api-Key": "sp-key"}
    assert calls[0]["json"] == {"subject": "x", "body": "y"}


def test_deliver_logs_but_does_not_raise_on_an_error_response(isolated_db, monkeypatch):
    import db
    db.set_status_portal_notify_url("http://status-portal.local")
    db.set_status_portal_notify_api_key("sp-key")
    monkeypatch.setattr(status_portal_client.requests, "post",
                         lambda *a, **k: _FakeResponse(status_code=401, text="bad key"))

    status_portal_client._deliver({"path": "/api/notify/admin", "payload": {}})


def test_deliver_propagates_a_network_exception_for_the_worker_loop_to_catch(isolated_db, monkeypatch):
    """_deliver() itself doesn't catch a network failure - _worker_loop's own
    try/except around _deliver() is what turns this into a logged-and-dropped
    job rather than a crashed thread. Confirmed here at the unit level since
    the worker thread itself isn't something these tests spin up."""
    import db
    db.set_status_portal_notify_url("http://status-portal.local")
    db.set_status_portal_notify_api_key("sp-key")
    monkeypatch.setattr(status_portal_client.requests, "post",
                         lambda *a, **k: (_ for _ in ()).throw(
                             status_portal_client.requests.exceptions.ConnectionError("unreachable")))

    with pytest.raises(status_portal_client.requests.exceptions.ConnectionError):
        status_portal_client._deliver({"path": "/api/notify/admin", "payload": {}})


def test_process_one_swallows_a_delivery_failure(isolated_db, monkeypatch):
    """The actual guarantee status_portal_client.py exists to provide: a
    delivery failure is logged and dropped, never raised out of the
    background worker (_worker_loop calls _process_one per job - see its
    docstring for why the exception handling is split out like this)."""
    import db
    db.set_status_portal_notify_url("http://status-portal.local")
    db.set_status_portal_notify_api_key("sp-key")
    monkeypatch.setattr(status_portal_client.requests, "post",
                         lambda *a, **k: (_ for _ in ()).throw(
                             status_portal_client.requests.exceptions.ConnectionError("unreachable")))

    status_portal_client.notify_new_request("Half-Life", "Alice")
    job = status_portal_client._queue.get_nowait()
    status_portal_client._process_one(job)  # must not raise
