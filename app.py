"""
app.py — Flask routes (public + admin). Dev server entry point.
Run with: python app.py
Admin panel: /admin (password is set on first launch)
"""
import io
import logging
import os
import secrets
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
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


# ---------------------------------------------------------------------------
# Backup and restore
# ---------------------------------------------------------------------------
KEEP_DB_SAFETY_BACKUPS = 5
# Refuses an upload past this size before ever touching SQLite with it - a
# generous ceiling for a database that's a handful of KB per request row.
MAX_RESTORE_UPLOAD_BYTES = 64 * 1024 * 1024


def _db_safety_backup_dir():
    """Computed from db.DB_PATH on every call rather than cached at import
    time - tests monkeypatch db.DB_PATH per-test, and a cached path would keep
    pointing at the real instance/db_backups/ regardless, quietly leaking test
    snapshot files into the real project directory."""
    return os.path.join(os.path.dirname(db.DB_PATH), "db_backups")


def _list_db_safety_backups():
    """Newest first. Snapshots taken automatically right before each restore -
    see _db_safety_snapshot()."""
    backup_dir = _db_safety_backup_dir()
    try:
        names = [n for n in os.listdir(backup_dir) if n.endswith(".db")]
    except OSError:
        return []
    entries = []
    for name in names:
        path = os.path.join(backup_dir, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        entries.append({"name": name, "path": path,
                        "size_mb": round(stat.st_size / (1024 * 1024), 2),
                        "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
    return sorted(entries, key=lambda e: e["created_at"], reverse=True)


def _prune_db_safety_backups():
    for entry in _list_db_safety_backups()[KEEP_DB_SAFETY_BACKUPS:]:
        try:
            os.remove(entry["path"])
        except OSError:
            _logger.warning("Could not prune old database snapshot %s", entry["name"])


def _db_safety_snapshot():
    """A consistent snapshot of the database as it is *right now*, taken right
    before it's replaced - the whole reason a bad restore isn't unrecoverable.
    Happens after the upload has already been validated (no point snapshotting
    for a file about to be rejected) and before a single byte of the live
    database is touched."""
    backup_dir = _db_safety_backup_dir()
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(backup_dir, f"portal-before-restore-{stamp}.db")
    db.backup_to_file(path)
    return path


@app.route("/admin/backup")
@login_required
def admin_backup():
    return render_template("admin_backup.html", db_backups=_list_db_safety_backups())


@app.route("/admin/backup/download")
@login_required
def admin_backup_download():
    if not os.path.isfile(db.DB_PATH):
        abort(404)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_db_path = os.path.join(tmp_dir, "portal.db")
        db.backup_to_file(tmp_db_path)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db_path, arcname="portal.db")
    buffer.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                      download_name=f"games-portal-backup-{stamp}.zip", max_age=0)


def _write_uploaded_database(upload, dest_path):
    """The uploaded file -> a plain .db at `dest_path`. Returns None, or a
    reason. Accepts either the zip the backup button produces or a bare .db,
    because an admin who unzipped it to look inside shouldn't be told their
    own backup is invalid. Nothing here inspects the *contents* - that's
    db.validate_backup_file()'s job; this only gets the bytes safely onto
    disk, which for a zip means never trusting the declared size and never
    joining a member name to a path (the classic zip-slip) - the single
    member is streamed to a filename this function chose, never the archive's
    own name."""
    filename = (upload.filename or "").lower()
    try:
        if filename.endswith(".zip"):
            with zipfile.ZipFile(upload.stream) as zf:
                members = [m for m in zf.infolist()
                          if not m.is_dir() and m.filename.lower().endswith(".db")]
                if not members:
                    return "That zip doesn't contain a .db file."
                if len(members) > 1:
                    return f"That zip contains {len(members)} .db files - expected exactly one."
                member = members[0]
                if member.file_size > MAX_RESTORE_UPLOAD_BYTES:
                    return f"The database inside that zip is too large ({member.file_size // (1024 * 1024)} MB)."
                written = 0
                with zf.open(member) as src, open(dest_path, "wb") as out:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_RESTORE_UPLOAD_BYTES:
                            return "The database inside that zip is too large."
                        out.write(chunk)
        else:
            upload.save(dest_path)
    except zipfile.BadZipFile:
        return "That file isn't a readable zip."
    except OSError as e:
        return f"Could not read the uploaded file: {e}"
    return None


@app.route("/admin/backup/restore", methods=["POST"])
@login_required
def admin_restore_db():
    """Replaces the live database with an uploaded backup.

    The order below is the safety machinery and is not rearrangeable:
      1. stage the upload to a temp file - the live database is untouched;
      2. validate it's a well-formed SQLite database *and* one of ours;
      3. snapshot the current database, so a regretted restore is recoverable;
      4. atomically replace (db.restore_from_file).

    Unlike status-portal's equivalent this never restarts the process
    afterwards: this app's db.py never pools a connection or runs in WAL mode
    (see restore_from_file's docstring), so the very next request's
    db.get_db() call already sees the replaced file - there's no stale
    connection or cached state a restart would need to clear."""
    upload = request.files.get("backup")
    if not upload or not upload.filename:
        flash("Choose a backup file to restore.", "error")
        return redirect(url_for("admin_backup"))

    os.makedirs(os.path.dirname(db.DB_PATH), exist_ok=True)
    fd, staged = tempfile.mkstemp(prefix="restore-", suffix=".db",
                                  dir=os.path.dirname(db.DB_PATH))
    os.close(fd)
    try:
        error = _write_uploaded_database(upload, staged)
        if error is None:
            error = db.validate_backup_file(staged)
        if error:
            flash(f"Restore refused: {error} Your database has not been touched.", "error")
            return redirect(url_for("admin_backup"))

        try:
            snapshot = _db_safety_snapshot()
        except Exception as e:
            _logger.exception("Could not snapshot the database before restoring")
            flash(f"Restore aborted: couldn't back up your current database first ({e}). "
                  "Nothing has been changed.", "error")
            return redirect(url_for("admin_backup"))

        try:
            db.restore_from_file(staged)
        except Exception as e:
            _logger.exception("Database restore failed")
            flash(f"Restore failed: {e}. Your previous database was saved to "
                  f"{os.path.basename(snapshot)}.", "error")
            return redirect(url_for("admin_backup"))
        staged = None
        _prune_db_safety_backups()
    finally:
        if staged and os.path.exists(staged):
            try:
                os.remove(staged)
            except OSError:
                pass

    _logger.warning("Database restored from an uploaded backup; previous database saved to %s",
                     os.path.basename(snapshot))
    flash(f"Database restored. Your previous database was saved as "
          f"{os.path.basename(snapshot)} in instance/db_backups/.", "success")
    return redirect(url_for("admin_backup"))


if __name__ == "__main__":
    db.init_db()
    print(f"games-portal (dev) started on http://127.0.0.1:{config.PORT}")
    app.run(host="127.0.0.1", port=config.PORT, debug=True)
