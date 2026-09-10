"""
config.py — All configuration in one place, read from environment variables
(or a local .env file via python-dotenv). Nothing else in this app should read
os.environ directly - add new settings here instead.
"""
import os
import secrets

from dotenv import load_dotenv

load_dotenv()

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


def _load_or_create_secret_key():
    env_key = os.environ.get("PORTAL_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    try:
        with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
            stored = f.read().strip()
        if stored:
            return stored
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(SECRET_KEY_FILE), exist_ok=True)
        fd = os.open(SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key)
    except OSError:
        pass
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
# Games-folder scanner (see scanner.py)
# ---------------------------------------------------------------------------
# One subfolder per installed game, e.g. /mnt/games. Blank disables the
# scanner entirely - nothing here ever runs, and the games-installed badge
# never shows, until this is set.
GAMES_FOLDER = os.environ.get("PORTAL_GAMES_FOLDER", "").strip()

# How often the background scan re-walks GAMES_FOLDER, in seconds.
SCANNER_INTERVAL_SECONDS = int(os.environ.get("PORTAL_SCANNER_INTERVAL_SECONDS", "1800"))

# Fuzzy name-match confidence (0-100, rapidfuzz's own scale) a folder name
# must clear against a request's title before it's even surfaced to the
# admin as "possible match?" - below this, nothing happens at all. This is
# not the bar for auto-resolving (nothing here ever auto-resolves a fuzzy
# match; see ROADMAP.md/CLAUDE.md), just for bothering the admin with it.
SCANNER_FUZZY_THRESHOLD = int(os.environ.get("PORTAL_SCANNER_FUZZY_THRESHOLD", "75"))
