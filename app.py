"""
app.py — Flask routes (public + admin). Dev server entry point.
Run with: python app.py
Admin panel: /admin (password is set on first launch)
"""
import logging
import secrets
import time
from datetime import timedelta
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db
import jellyfin_auth
import steam

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
                            statuses=db.REQUEST_STATUSES)


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


if __name__ == "__main__":
    db.init_db()
    print(f"games-portal (dev) started on http://127.0.0.1:{config.PORT}")
    app.run(host="127.0.0.1", port=config.PORT, debug=True)
