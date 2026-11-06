"""
serve_waitress.py — PRODUCTION entry point.
Uses waitress (a proper WSGI server) instead of Flask's dev server.

Run with:
    python serve_waitress.py

Run THIS script at system startup (systemd, Task Scheduler, supervisord...),
not app.py.
"""
import logging
import socket

from waitress import create_server

import config
import db
import scanner
import status_portal_client
import updater
from app import app, LOG_FORMAT

_logger = logging.getLogger(__name__)


def build_server(wsgi_app, port, threads):
    """Listen on IPv4 *and* IPv6, falling back to IPv4 alone.

    IPv4 only (host="0.0.0.0") made every check from status-portal take a flat
    ~2 seconds on Windows: "localhost" (and the machine's own hostname) resolve
    to IPv6 first, nothing answered on ::1, and Windows retries a refused
    connection for about 2s before trying 127.0.0.1. A browser races both
    families (Happy Eyeballs), so the page still felt instant - only the
    health check paid it, and got marked slow on every cycle. See
    docs/HISTORY.md, 2026-11-06.

    waitress sets IPV6_V6ONLY on its IPv6 socket, so the two binds don't
    collide. A host with IPv6 switched off (or a container without it) can't
    bind [::]; that must not stop the portal starting, so it falls back to
    the old IPv4-only listener with a warning.
    """
    if _ipv6_available(port):
        return create_server(wsgi_app, listen=f"0.0.0.0:{port} [::]:{port}", threads=threads)
    return create_server(wsgi_app, host="0.0.0.0", port=port, threads=threads)


def _ipv6_available(port):
    """Can [::]:port be bound, IPv6-only, the way waitress will bind it?

    Probed on a throwaway socket rather than by catching create_server()'s
    failure: that would leave its already-bound IPv4 socket listening, and the
    IPv4-only retry would then fail with "address in use". Any other failure
    (the port really is taken) still surfaces from create_server() itself."""
    if not socket.has_ipv6:
        _logger.warning("This Python has no IPv6 support - serving on IPv4 only.")
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            probe.bind(("::", port))
    except OSError as e:
        _logger.warning("Couldn't listen on IPv6 (%s) - serving on IPv4 only.", e)
        return False
    return True


if __name__ == "__main__":
    # WARNING by default, not INFO - unlike app.py (the dev server), a
    # production log shouldn't be this chatty by default. Both waitress's own
    # logger and this app's inherit from the root logger configured here,
    # since neither sets its own level. Override with PORTAL_LOG_LEVEL.
    logging.basicConfig(level=config.LOG_LEVEL or "WARNING", format=LOG_FORMAT)
    db.init_db()
    # If the previous shutdown was an in-app update restarting into a new
    # version, this is where that gets confirmed (or reported as not having
    # taken effect).
    updater.check_pending_marker()
    updater.start_background_checker()
    scanner.start_background_scanner()
    status_portal_client.start_background_worker()
    server = build_server(app, config.PORT, config.WAITRESS_THREADS)
    print(f"games-portal started on port {config.PORT}")
    server.print_listen("Serving on http://{}:{}")
    server.run()
