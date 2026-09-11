"""
config.py — All configuration in one place, read from environment variables
(or a local .env file via python-dotenv). Nothing else in this app should read
os.environ directly - add new settings here instead.
"""
import logging
import os
import secrets
import time

from dotenv import load_dotenv

load_dotenv()

_logger = logging.getLogger(__name__)

APP_ROOT = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
# A plain tracked file rather than a literal here, so a release built with
# `git archive` (which strips .git entirely) still carries a version - see
# CLAUDE.md's release process. Bumping VERSION is a required step of cutting one;
# nothing derives it automatically.
def _read_version():
    try:
        with open(os.path.join(APP_ROOT, "VERSION"), "r", encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


VERSION = _read_version()

# True when running from a git working tree rather than an extracted release zip.
IS_GIT_CHECKOUT = os.path.isdir(os.path.join(APP_ROOT, ".git"))
VERSION_DISPLAY = VERSION + ("+dev" if IS_GIT_CHECKOUT else "")

# ---------------------------------------------------------------------------
# Session signing key
# ---------------------------------------------------------------------------
# PORTAL_SECRET_KEY wins when set. Without it, a key is generated once and
# persisted to instance/secret_key (0600) so sessions survive a restart - a
# fresh random key per process would silently sign everyone out (admin and
# visitors alike) every time the app restarts.
SECRET_KEY_FILE = os.path.join(APP_ROOT, "instance", "secret_key")

# How many times (and how long between) to retry a failed read of an
# *existing* secret_key file before concluding it's unreadable and generating
# a new one. Guards against a transient lock - a cloud-synced folder (OneDrive,
# Dropbox) or antivirus real-time scanning briefly holding the file open right
# after it's written is a real, reported cause on Windows (a Desktop folder is
# a common default OneDrive sync target) - rather than treating one momentary
# failure as "the key is gone" and silently signing everyone out.
SECRET_KEY_READ_RETRY_ATTEMPTS = 3
SECRET_KEY_READ_RETRY_DELAY_SECONDS = 0.3


def _read_secret_key_with_retry():
    """The persisted key, or None if it genuinely doesn't exist yet (the
    normal first-run case - fails fast, no point retrying that) or couldn't
    be read even after retrying past a transient failure."""
    if not os.path.isfile(SECRET_KEY_FILE):
        return None
    last_error = None
    for attempt in range(SECRET_KEY_READ_RETRY_ATTEMPTS):
        try:
            with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
                stored = f.read().strip()
            if stored:
                return stored
            break  # exists but empty - not a lock, retrying won't help
        except OSError as e:
            last_error = e
            if attempt + 1 < SECRET_KEY_READ_RETRY_ATTEMPTS:
                time.sleep(SECRET_KEY_READ_RETRY_DELAY_SECONDS)
    if last_error:
        _logger.warning(
            "Could not read the persisted session secret key at %s after %d attempt(s) (%s) - "
            "generating a new one for this process, which will sign everyone out. If this keeps "
            "happening: something is locking that file - a cloud-synced folder (OneDrive, "
            "Dropbox) or antivirus real-time scanning are common causes on Windows. Moving this "
            "app's folder outside any synced location usually fixes it.",
            SECRET_KEY_FILE, SECRET_KEY_READ_RETRY_ATTEMPTS, last_error)
    return None


def _load_or_create_secret_key():
    env_key = os.environ.get("PORTAL_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    stored = _read_secret_key_with_retry()
    if stored:
        return stored
    key = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(SECRET_KEY_FILE), exist_ok=True)
        fd = os.open(SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key)
    except OSError as e:
        # Must never be silent: a key that fails to persist still works fine for
        # *this* process, so nothing looks wrong right now - but the next process
        # start (a crash, a Docker restart, anything) generates yet another
        # ephemeral key the same way, silently signing out every admin and
        # visitor session every single time. From the outside that presents as
        # unexplained "random disconnects," not as an obvious startup failure -
        # exactly the class of bug this log line exists to surface immediately
        # instead of leaving someone to rediscover it session by session.
        _logger.error(
            "Could not persist a new session secret key to %s (%s). Using a "
            "one-off key for THIS PROCESS ONLY - every admin and visitor "
            "session will be invalidated the next time this process restarts, "
            "and again every time after that until this becomes writable. "
            "Check the ownership/permissions of %s.",
            SECRET_KEY_FILE, e, os.path.dirname(SECRET_KEY_FILE))
    return key


SECRET_KEY = _load_or_create_secret_key()

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORTAL_PORT", "5001"))
SESSION_COOKIE_MAX_AGE_DAYS = 30
WAITRESS_THREADS = int(os.environ.get("PORTAL_WAITRESS_THREADS", "8"))

# Set to true only if a reverse proxy (nginx, Caddy, Cloudflare Tunnel...) sits in
# front of this app - enables trusting its X-Forwarded-* headers. Leave false if
# the app's port is reachable directly.
BEHIND_PROXY = os.environ.get("PORTAL_BEHIND_PROXY", "false").lower() == "true"

# Set to true once this is served over HTTPS - marks the session cookie Secure.
# Leave false for plain-HTTP LAN/Tailscale-only setups.
FORCE_HTTPS_COOKIES = os.environ.get("PORTAL_FORCE_HTTPS_COOKIES", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Steam search (see steam.py)
# ---------------------------------------------------------------------------
# Search is the one place in this app where an outbound call genuinely happens
# inside a request handler: the query isn't known until somebody types it, so no
# amount of background polling can answer it ahead of time. The timeout is the
# safety story - a slow Steam response must degrade to "search is unavailable
# right now" quickly, not hold a request-handling thread. See CLAUDE.md's
# "No slow I/O in a request handler" rule for the general policy this is the
# sanctioned exception to.
STEAM_SEARCH_TIMEOUT_SECONDS = int(os.environ.get("PORTAL_STEAM_SEARCH_TIMEOUT_SECONDS", "6"))

# storesearch (the results list) returns no description - only appdetails does,
# and that's one outbound call per app. Fetched concurrently, capped at this many
# workers, each with its own short timeout below - so one slow/unreachable app
# can never hold up the rest of the page, and a burst of 10 results can't turn
# into 10 sequential round trips.
STEAM_DETAILS_WORKERS = int(os.environ.get("PORTAL_STEAM_DETAILS_WORKERS", "5"))
STEAM_DETAILS_TIMEOUT_SECONDS = int(os.environ.get("PORTAL_STEAM_DETAILS_TIMEOUT_SECONDS", "4"))

# How many storesearch results to show (and fetch details for) per query. Also
# bounds the worst case for STEAM_DETAILS_WORKERS above - keep this and that
# number in mind together when raising either.
STEAM_SEARCH_RESULT_LIMIT = int(os.environ.get("PORTAL_STEAM_SEARCH_RESULT_LIMIT", "10"))

# Per-session rate limit on searches: a search box wired to a third-party API is
# otherwise a free denial-of-service amplifier, even an unauthenticated one.
SEARCH_RATE_LIMIT = int(os.environ.get("PORTAL_SEARCH_RATE_LIMIT", "30"))
SEARCH_RATE_WINDOW_SECONDS = int(os.environ.get("PORTAL_SEARCH_RATE_WINDOW_SECONDS", "60"))

# ---------------------------------------------------------------------------
# Jellyfin-backed visitor sign-in (see jellyfin_auth.py)
# ---------------------------------------------------------------------------
# Blank disables visitor sign-in entirely (and with it, requesting a game - see
# app.py's user_login_required). Deploy-time config, not a DB-backed admin
# toggle: unlike status-portal, this app has no integrations table yet, and one
# Jellyfin server is the overwhelmingly common case for a single-admin portal.
JELLYFIN_URL = os.environ.get("PORTAL_JELLYFIN_URL", "").strip()

# HTTP timeout for the sign-in call to Jellyfin. Its own value rather than
# reusing the Steam search timeout above - someone is sitting there waiting on
# this one, and a Jellyfin busy transcoding can be slow to answer.
JELLYFIN_AUTH_TIMEOUT_SECONDS = int(os.environ.get("PORTAL_JELLYFIN_AUTH_TIMEOUT_SECONDS", "10"))

# ---------------------------------------------------------------------------
# Self-update (see updater.py / update.py)
# ---------------------------------------------------------------------------
# How often the background checker re-asks GitHub what the latest release is,
# in seconds. 6h by default: GitHub's unauthenticated API allows 60 requests/hour
# per IP and nothing here benefits from knowing about a new release sooner than
# that. Whether checking runs at all is a routine admin toggle (the
# update_check_enabled DB setting, default on) rather than this - this is just
# the interval once it's on.
UPDATE_CHECK_INTERVAL_SECONDS = int(os.environ.get("PORTAL_UPDATE_CHECK_INTERVAL_SECONDS", "21600"))

# Kill-switch for the in-app "Update now" button (the standalone update.py script
# over SSH is unaffected and always works). Deliberately an env var rather than a
# DB setting: the risk it mitigates is "someone got into the admin panel", and a
# toggle that same attacker could flip from that same panel would mitigate
# nothing. Changing this needs filesystem access to the host plus a restart.
# Defaults to enabled; set it to false to require SSH access for every update.
ENABLE_INAPP_UPDATE = os.environ.get("PORTAL_ENABLE_INAPP_UPDATE", "true").lower() != "false"

# ---------------------------------------------------------------------------
# Games-folder scanner (see scanner.py)
# ---------------------------------------------------------------------------
# Blank disables the scanner entirely - same "blank means off" pattern as
# JELLYFIN_URL above. Deploy-time config, not a DB setting: a filesystem path
# is not something to hand to a form field.
GAMES_FOLDER = os.environ.get("PORTAL_GAMES_FOLDER", "").strip()

# How often the background scanner re-walks GAMES_FOLDER, in seconds. Cheap on
# its own (a local os.listdir), but an untagged folder's fuzzy-match step calls
# out to Steam - this interval is the throttle on that, not on the filesystem
# walk itself.
SCAN_INTERVAL_SECONDS = int(os.environ.get("PORTAL_SCAN_INTERVAL_SECONDS", "3600"))

# rapidfuzz.fuzz.WRatio score (0-100) an untagged folder's best Steam search
# match must clear to surface as "possible match?" for the admin - never to
# auto-resolve. Below this, a folder is left unmatched rather than nagging the
# admin with a low-confidence guess every scan.
SCAN_FUZZY_MATCH_THRESHOLD = int(os.environ.get("PORTAL_SCAN_FUZZY_MATCH_THRESHOLD", "82"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Blank (the default) means "let the entry point decide" - app.py (the dev
# server) defaults to INFO, serve_waitress.py (production) defaults to
# WARNING, since a production log shouldn't be as chatty as a dev session's
# by default. Set explicitly to override either one, e.g. INFO in production
# while troubleshooting something. Invalid values are ignored (fall back to
# the entry point's own default) rather than crashing startup over a typo.
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_raw_log_level = os.environ.get("PORTAL_LOG_LEVEL", "").strip().upper()
LOG_LEVEL = _raw_log_level if _raw_log_level in _VALID_LOG_LEVELS else ""
