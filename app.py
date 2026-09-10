"""
app.py — Flask routes (public + admin). Dev server entry point.
Run with: python app.py
Admin panel: /admin (password is set on first launch)
"""
import logging
import os
import secrets
import sys
import threading
import time
from datetime import timedelta
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db
import jellyfin_auth
import steam
import updater

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.FORCE_HTTPS_COOKIES,
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(days=config.SESSION_COOKIE_MAX_AGE_DAYS),
)

if config.BEHIND_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data: https://shared.akamai.steamstatic.com https://cdn.akamai.steamstatic.com; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    response.headers["Server"] = "games-portal"
    return response


@app.errorhandler(400)
def handle_bad_request(e):
    return render_template("error.html", code=400, message="Invalid or expired form submission. "
                            "Please reload the page and try again."), 400


@app.errorhandler(404)
def handle_not_found(e):
    return render_template("error.html", code=404, message="Page not found."), 404


@app.errorhandler(500)
def handle_server_error(e):
    _logger.exception("Unhandled exception in request %s %s", request.method, request.path)
    return render_template("error.html", code=500, message="Something went wrong."), 500


# ---------------------------------------------------------------------------
# CSRF protection - every state-changing route here is a POST. A per-session
# token, embedded as a hidden field in every form and checked against the
# session on every POST. Bypassed when app.testing is set (the test client posts
# raw form dicts, never simulating a browser rendering the hidden field).
# ---------------------------------------------------------------------------
def _get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = _get_csrf_token


@app.context_processor
def _inject_globals():
    return {"user": session.get("portal_user"), "jellyfin_enabled": jellyfin_auth.is_enabled()}


@app.before_request
def _check_csrf():
    if app.testing or request.method != "POST":
        return
    submitted = request.form.get("csrf_token", "")
    if not submitted or submitted != session.get("csrf_token"):
        abort(400)


# ---------------------------------------------------------------------------
# Admin auth - a single admin, password set on first launch (see is_first_run).
# ---------------------------------------------------------------------------
def is_first_run():
    return db.get_setting("admin_password_hash") is None


def _start_admin_session():
    session.permanent = True
    session["logged_in"] = True


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


_login_state = {"failures": 0, "locked_until": 0.0}
LOGIN_LOCKOUT_THRESHOLD = 5
LOGIN_LOCKOUT_SECONDS = 300


def _login_locked():
    return time.time() < _login_state["locked_until"]


def _register_login_failure():
    _login_state["failures"] += 1
    if _login_state["failures"] >= LOGIN_LOCKOUT_THRESHOLD:
        _login_state["locked_until"] = time.time() + LOGIN_LOCKOUT_SECONDS
        _login_state["failures"] = 0


def _register_login_success():
    _login_state["failures"] = 0
    _login_state["locked_until"] = 0.0


# ---------------------------------------------------------------------------
# Visitor auth (Jellyfin-backed) - a second, entirely separate identity. See
# jellyfin_auth.py. Different session key (portal_user vs logged_in), different
# decorator, different lockout counter - structurally separate so a mistake in
# one can't grant the other.
# ---------------------------------------------------------------------------
def _start_user_session(user):
    session.permanent = True
    session["portal_user"] = {"id": user["id"], "name": user["name"]}


def _end_user_session():
    session.pop("portal_user", None)


def current_user():
    return session.get("portal_user")


def user_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("portal_user"):
            flash("Sign in with your Jellyfin account to request a game.", "error")
            return redirect(url_for("user_login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


_user_login_state = {"failures": 0, "locked_until": 0.0}
USER_LOGIN_LOCKOUT_THRESHOLD = 10
USER_LOGIN_LOCKOUT_SECONDS = 300


def _user_login_locked():
    return time.time() < _user_login_state["locked_until"]


def _register_user_login_failure():
    _user_login_state["failures"] += 1
    if _user_login_state["failures"] >= USER_LOGIN_LOCKOUT_THRESHOLD:
        _user_login_state["locked_until"] = time.time() + USER_LOGIN_LOCKOUT_SECONDS
        _user_login_state["failures"] = 0


def _register_user_login_success():
    _user_login_state["failures"] = 0
    _user_login_state["locked_until"] = 0.0


def _safe_next_url(raw):
    """Only ever redirects back into this app - an unvalidated `next` is an open
    redirect."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return None


# ---------------------------------------------------------------------------
# Public: search + request
# ---------------------------------------------------------------------------
def _search_rate_limited():
    now = time.time()
    window_start = session.get("search_window_start", 0)
    if now - window_start > config.SEARCH_RATE_WINDOW_SECONDS:
        session["search_window_start"] = now
        session["search_count"] = 0
    if session.get("search_count", 0) >= config.SEARCH_RATE_LIMIT:
        return True
    session["search_count"] = session.get("search_count", 0) + 1
    return False


@app.route("/")
def index():
    query = request.args.get("q", "").strip()
    results = []
    search_error = None
    if query:
        if _search_rate_limited():
            search_error = "Too many searches - please wait a minute and try again."
        else:
            try:
                results = steam.search(query)
                steam.enrich_with_descriptions(results)
            except Exception as e:
                _logger.warning("Steam search failed for %r: %s", query, e)
                search_error = "Steam search is unavailable right now. Please try again shortly."

    existing = db.active_request_appids([r["appid"] for r in results])
    for r in results:
        r["existing_status"] = existing.get(r["appid"])

    return render_template("index.html", query=query, results=results, search_error=search_error)


@app.route("/request", methods=["POST"])
@user_login_required
def submit_request():
    raw_appid = request.form.get("appid", "")
    next_url = _safe_next_url(request.form.get("next")) or url_for("index")
    if not raw_appid.isdigit():
        flash("Invalid game.", "error")
        return redirect(next_url)
    appid = int(raw_appid)

    if db.get_active_request_for_appid(appid):
        flash("That game has already been requested.", "error")
        return redirect(next_url)

    summary = steam.fetch_app_summary(appid)
    if summary is None:
        flash("Could not look up that game on Steam - please try again.", "error")
        return redirect(next_url)

    user = current_user()
    db.create_request(summary["appid"], summary["name"], summary["icon_url"],
                       summary["short_description"], user["id"], user["name"])
    flash(f'Requested "{summary["name"]}".', "success")
    return redirect(next_url)


# ---------------------------------------------------------------------------
# Visitor sign-in (Jellyfin-backed). Entirely separate from /admin/login below.
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def user_login():
    if not jellyfin_auth.is_enabled():
        abort(404)
    next_url = _safe_next_url(request.args.get("next") or request.form.get("next"))
    if session.get("portal_user"):
        return redirect(next_url or url_for("index"))

    if request.method == "POST":
        if _user_login_locked():
            flash("Too many failed sign-ins. Try again in a few minutes.", "error")
            return render_template("login.html", next_url=next_url)
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            flash("Enter your Jellyfin username and password.", "error")
            return render_template("login.html", next_url=next_url)

        result = jellyfin_auth.authenticate(username, password)
        if result["ok"]:
            _register_user_login_success()
            _start_user_session(result["user"])
            _logger.info("Jellyfin user '%s' signed in", result["user"]["name"])
            return redirect(next_url or url_for("index"))

        reason = result.get("reason")
        if reason == "unreachable":
            flash("Can't reach Jellyfin right now, so sign-in isn't available. "
                  "This isn't a problem with your password - please try again shortly.", "error")
        elif reason == "disabled":
            _register_user_login_failure()
            flash("That Jellyfin account is disabled.", "error")
        elif reason == "not_configured":
            flash("Jellyfin sign-in isn't configured on this portal.", "error")
        else:
            _register_user_login_failure()
            flash("Incorrect username or password.", "error")

    return render_template("login.html", next_url=next_url)


@app.route("/logout")
def user_logout():
    _end_user_session()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@app.route("/admin")
@login_required
def admin_dashboard():
    return redirect(url_for("admin_requests"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    first_run = is_first_run()

    if request.method == "POST":
        if not first_run and _login_locked():
            flash("Too many failed attempts. Try again in a few minutes.", "error")
            return render_template("admin_login.html", first_run=first_run)

        password = request.form.get("password", "")
        if first_run:
            confirm = request.form.get("confirm", "")
            if len(password) < 6:
                flash("Password must be at least 6 characters.", "error")
            elif password != confirm:
                flash("Passwords do not match.", "error")
            else:
                db.set_setting("admin_password_hash", generate_password_hash(password))
                _start_admin_session()
                return redirect(url_for("admin_dashboard"))
        else:
            stored = db.get_setting("admin_password_hash")
            if stored and check_password_hash(stored, password):
                _register_login_success()
                _start_admin_session()
                return redirect(_safe_next_url(request.args.get("next")) or url_for("admin_dashboard"))
            _register_login_failure()
            flash("Incorrect password.", "error")

    return render_template("admin_login.html", first_run=first_run)


@app.route("/admin/logout")
def admin_logout():
    session.pop("logged_in", None)
    return redirect(url_for("index"))


@app.route("/admin/requests")
@login_required
def admin_requests():
    status_filter = request.args.get("status", "")
    requests_list = db.list_requests(status=status_filter or None)
    return render_template("admin_requests.html", requests=requests_list, status_filter=status_filter,
                            statuses=db.REQUEST_STATUSES, active="requests")


@app.route("/admin/requests/<int:request_id>/status", methods=["POST"])
@login_required
def admin_update_request(request_id):
    if db.get_request(request_id) is None:
        abort(404)
    status = request.form.get("status", "")
    note = request.form.get("admin_note", "").strip()[:500]
    if status not in db.REQUEST_STATUSES:
        flash("Unknown status.", "error")
    else:
        db.update_request_status(request_id, status, note)
        flash("Request updated.", "success")
    return redirect(url_for("admin_requests", status=request.args.get("status", "")))


# ---------------------------------------------------------------------------
# About / self-update (see updater.py for everything that actually happens)
# ---------------------------------------------------------------------------
@app.route("/admin/about")
@login_required
def admin_about():
    import platform
    return render_template(
        "admin_about.html",
        active="about",
        version_display=config.VERSION_DISPLAY,
        is_git_checkout=config.IS_GIT_CHECKOUT,
        channel=updater.get_channel(),
        check_enabled=updater.update_check_enabled(),
        check_interval_hours=round(config.UPDATE_CHECK_INTERVAL_SECONDS / 3600, 1),
        update_status=updater.get_cached_update_status(),
        inapp_update_enabled=config.ENABLE_INAPP_UPDATE,
        repo_url=updater.REPO_URL,
        releases_url=updater.RELEASES_PAGE_URL,
        backups=list(reversed(updater.list_backups()))[:5],
        python_version=platform.python_version(),
        platform_name=platform.platform(),
        app_root=config.APP_ROOT,
        db_path=db.DB_PATH,
    )


@app.route("/admin/about/check", methods=["POST"])
@login_required
def admin_about_check():
    result = updater.refresh_update_cache_if_stale(force=True)
    if result and result["ok"]:
        if result["update_available"]:
            flash(f"Update available: {result['current']} → {result['latest']}.", "success")
        else:
            flash("Up to date.", "success")
    elif result:
        flash(f"Couldn't check for updates: {result['error']}", "error")
    return redirect(url_for("admin_about"))


@app.route("/admin/about/settings", methods=["POST"])
@login_required
def admin_about_settings():
    channel = request.form.get("update_channel", "")
    if channel not in updater.CHANNELS:
        flash("Unknown channel.", "error")
        return redirect(url_for("admin_about"))
    if channel != updater.get_channel():
        updater.set_channel(channel)
        # A cached "latest available" fetched for the *other* channel would be
        # actively misleading next to the newly-selected one.
        updater.clear_update_cache()
    updater.set_update_check_enabled(bool(request.form.get("update_check_enabled")))
    flash("Preferences saved.", "success")
    return redirect(url_for("admin_about"))


@app.route("/admin/about/update", methods=["POST"])
@login_required
def admin_update():
    """Installs the latest release and restarts the app into it.

    Gated the same way every other state-changing admin route in this app is -
    login + CSRF, plus a client-side confirm() (see static/js/admin_update.js).
    Unlike status-portal's equivalent button, there's no step-up 2FA here: this
    app has no 2FA system yet. config.ENABLE_INAPP_UPDATE is the other gate,
    lives in an env var rather than a DB setting precisely so an attacker who
    owns the admin panel can't just switch it back on.

    Runs updater.perform_update() synchronously - a deliberate, documented
    instance of the "explicit one-shot admin action the user knows will be
    slow" exception to CLAUDE.md's no-slow-I/O rule, not an automatic
    background path."""
    if not config.ENABLE_INAPP_UPDATE:
        flash("In-app updates are disabled (PORTAL_ENABLE_INAPP_UPDATE=false). "
              "Use the update.py script over SSH instead.", "error")
        return redirect(url_for("admin_about"))
    if config.IS_GIT_CHECKOUT:
        flash("This is a git checkout, not an installed release - updating would overwrite "
              "tracked files. Use `git pull` instead.", "error")
        return redirect(url_for("admin_about"))

    lines = []

    def progress(message):
        lines.append(message)
        _logger.info("[update] %s", message)

    try:
        result = updater.perform_update(progress=progress)
    except updater.UpdateError as e:
        _logger.error("In-app update failed: %s", e)
        flash(f"Update failed: {e}", "error")
        return redirect(url_for("admin_about"))
    except Exception as e:
        _logger.exception("In-app update crashed")
        flash(f"Update failed unexpectedly: {e} (see the server logs)", "error")
        return redirect(url_for("admin_about"))

    if not result["applied"]:
        flash(f"Nothing to update - {result['reason']}.", "success")
        return redirect(url_for("admin_about"))

    # Written before the restart so the next successful start can confirm it
    # came up on the new version - and so `python update.py rollback` knows
    # which backup to use if it doesn't. See updater.write_pending_marker() for
    # what this can and cannot detect.
    updater.write_pending_marker(result["backup"], result["latest"])
    flash(f"Updated {result['current']} → {result['latest']}. Restarting now - this page will be "
          f"briefly unreachable. If it doesn't come back, run "
          f"`python update.py rollback` on the server.", "success")
    _restart_process()
    return redirect(url_for("admin_about"))


# ---------------------------------------------------------------------------
# Restarting the process in place (used by admin_update above)
# ---------------------------------------------------------------------------
def _release_dev_server_socket():
    """Closes the listening socket the Werkzeug reloader would otherwise hand
    to the re-exec'd process via the WERKZEUG_SERVER_FD environment variable.

    Ported from status-portal, which hit this for real: os.execv() replaces the
    process image but keeps open file descriptors, and when the reloader is
    active it marks its listening socket inheritable through that env var. The
    re-executed process then tries to bind the same port a second time and
    dies with "Address already in use" - a restart button that kills the
    portal instead of restarting it. This app's dev entrypoint runs with
    debug=False specifically to avoid the reloader being active in the first
    place (see the bottom of this file) - this is belt-and-braces for if that
    ever changes. Production (serve_waitress.py) never sets this var at all,
    so there's nothing to close there either way.

    Failure here is deliberately swallowed: not being able to close a socket
    must never be the reason a restart doesn't happen."""
    raw_fd = os.environ.pop("WERKZEUG_SERVER_FD", None)
    if raw_fd is None:
        return
    try:
        os.close(int(raw_fd))
    except (ValueError, OSError):
        _logger.info("Could not close the inherited development-server socket", exc_info=True)


def _restart_process():
    """Replaces the running process image in place via os.execv - same PID,
    works identically whether launched as `python app.py`, `python
    serve_waitress.py`, or either wrapped in a systemd unit/Task Scheduler
    entry, and needs no supervisor process. Delayed briefly on a background
    thread so the triggering HTTP response has a moment to actually reach the
    browser first."""
    def _do():
        time.sleep(1)
        _release_dev_server_socket()
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_do, daemon=True).start()


if __name__ == "__main__":
    db.init_db()
    # If the previous shutdown was an in-app update restarting into a new
    # version, this is where that gets confirmed (or reported as not having
    # taken effect).
    updater.check_pending_marker()
    updater.start_background_checker()
    print(f"games-portal (dev) started on http://127.0.0.1:{config.PORT}")
    # debug=False deliberately: the Werkzeug reloader's WERKZEUG_SERVER_FD
    # handoff and this app's own os.execv()-based self-restart (see
    # _restart_process above) don't mix - see _release_dev_server_socket()'s
    # docstring for the incident that taught status-portal this the hard way.
    app.run(host="127.0.0.1", port=config.PORT, debug=False)
