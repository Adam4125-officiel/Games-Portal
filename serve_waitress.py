"""
serve_waitress.py — PRODUCTION entry point.
Uses waitress (a proper WSGI server) instead of Flask's dev server.

Run with:
    python serve_waitress.py

Run THIS script at system startup (systemd, Task Scheduler, supervisord...),
not app.py.
"""
from waitress import serve

import config
import db
import scanner
import updater
from app import app

if __name__ == "__main__":
    db.init_db()
    # If the previous shutdown was an in-app update restarting into a new
    # version, this is where that gets confirmed (or reported as not having
    # taken effect).
    updater.check_pending_marker()
    updater.start_background_checker()
    scanner.start_background_scanner()
    print(f"games-portal started on http://0.0.0.0:{config.PORT}")
    serve(app, host="0.0.0.0", port=config.PORT, threads=config.WAITRESS_THREADS)
