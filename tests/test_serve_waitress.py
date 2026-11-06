"""serve_waitress.build_server(): IPv4 + IPv6, or IPv4 alone when IPv6 can't
be bound. IPv4-only was a flat ~2s per status-portal check on Windows - see
docs/HISTORY.md, 2026-11-06."""
import socket
import threading
import time

import pytest
import requests

import serve_waitress


def _hello(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listen_families(server):
    sockets = [s.socket for s in getattr(server, "map", {}).values()
               if hasattr(s, "socket") and getattr(s, "accepting", False)]
    if not sockets:
        sockets = [server.socket]
    return {s.family for s in sockets}


def test_listens_on_both_families_when_ipv6_is_available():
    if not serve_waitress._ipv6_available(_free_port()):
        pytest.skip("no IPv6 on this machine")
    port = _free_port()
    server = serve_waitress.build_server(_hello, port, threads=1)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        time.sleep(0.2)
        for host in ("127.0.0.1", "[::1]"):
            assert requests.get(f"http://{host}:{port}/", timeout=2).text == "ok"
    finally:
        server.close()


def test_falls_back_to_ipv4_when_ipv6_cannot_be_bound(monkeypatch, caplog):
    real_socket = socket.socket

    def no_ipv6(family=socket.AF_INET, *args, **kwargs):
        if family == socket.AF_INET6:
            raise OSError("Address family not supported by protocol")
        return real_socket(family, *args, **kwargs)

    monkeypatch.setattr(serve_waitress.socket, "socket", no_ipv6)
    port = _free_port()
    server = serve_waitress.build_server(_hello, port, threads=1)
    try:
        assert _listen_families(server) == {socket.AF_INET}
        assert "IPv4 only" in caplog.text
    finally:
        server.close()
