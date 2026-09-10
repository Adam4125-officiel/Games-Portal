"""
db.py — The entire database layer (SQLite). No ORM, plain SQL, hand-rolled
schema management: see CLAUDE.md's "No ORM, no migration framework" rule.
"""
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "portal.db")

REQUEST_STATUSES = ("pending", "approved", "downloading", "done", "rejected")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(conn, table, column, ddl):
    """Adds `column` to `table` if an existing database predates it -
    CREATE TABLE IF NOT EXISTS only helps for a brand-new database; an
    existing table never gets new columns that way. See CLAUDE.md."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            steam_appid INTEGER NOT NULL,
            name TEXT NOT NULL,
            icon_url TEXT NOT NULL DEFAULT '',
            short_description TEXT NOT NULL DEFAULT '',
            requested_by_id TEXT NOT NULL,
            requested_by_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            admin_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # Games the scanner found an exact AppID for (a folder-name tag or a
    # sidecar file) - see scanner.py. One row per installed game; a re-scan
    # just upserts the same appid again.
    c.execute("""
        CREATE TABLE IF NOT EXISTS installed_games (
            steam_appid INTEGER PRIMARY KEY,
            folder_name TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            detected_at TEXT NOT NULL
        )
    """)

    # Fuzzy folder-name -> request suggestions awaiting admin review. Never
    # auto-resolved - see scanner.py's confirm_match()/reject_match(). A
    # rejected row is kept (status='rejected'), not deleted, so the same
    # folder+request pairing isn't re-suggested on every subsequent scan.
    c.execute("""
        CREATE TABLE IF NOT EXISTS scan_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_name TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            request_id INTEGER NOT NULL,
            score INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            UNIQUE(folder_path, request_id)
        )
    """)

    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Settings (admin password hash)
# ---------------------------------------------------------------------------
def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
def create_request(steam_appid, name, icon_url, short_description, requested_by_id, requested_by_name):
    conn = get_db()
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO requests (steam_appid, name, icon_url, short_description, "
        "requested_by_id, requested_by_name, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (steam_appid, name, icon_url, short_description, requested_by_id, requested_by_name, ts, ts))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def get_active_request_for_appid(steam_appid):
    """The most recent non-rejected request for this app, or None. Used to stop
    the same game being requested twice while a request for it is already
    pending/in-flight - a rejected request doesn't block a fresh one."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM requests WHERE steam_appid=? AND status != 'rejected' "
        "ORDER BY created_at DESC LIMIT 1", (steam_appid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def active_request_appids(appids):
    """Which of these appids already have a non-rejected request, mapped to its
    status - one query for a whole page of search results instead of one per
    result."""
    if not appids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(appids))
    rows = conn.execute(
        f"SELECT steam_appid, status FROM requests "
        f"WHERE steam_appid IN ({placeholders}) AND status != 'rejected'", appids).fetchall()
    conn.close()
    return {row["steam_appid"]: row["status"] for row in rows}


def list_requests(status=None):
    conn = get_db()
    if status:
        rows = conn.execute("SELECT * FROM requests WHERE status=? ORDER BY created_at DESC",
                             (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM requests ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_request(request_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_request_status(request_id, status, admin_note):
    if status not in REQUEST_STATUSES:
        raise ValueError(f"Unknown request status: {status!r}")
    conn = get_db()
    conn.execute("UPDATE requests SET status=?, admin_note=?, updated_at=? WHERE id=?",
                 (status, admin_note, now_iso(), request_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Installed games (exact AppID matches - see scanner.py)
# ---------------------------------------------------------------------------
def upsert_installed_game(steam_appid, folder_name, folder_path):
    conn = get_db()
    conn.execute(
        "INSERT INTO installed_games (steam_appid, folder_name, folder_path, detected_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(steam_appid) DO UPDATE SET "
        "folder_name=excluded.folder_name, folder_path=excluded.folder_path, "
        "detected_at=excluded.detected_at",
        (steam_appid, folder_name, folder_path, now_iso()))
    conn.commit()
    conn.close()


def installed_appids(appids):
    """Which of these appids are installed - one query per page of search
    results, mirroring active_request_appids()."""
    if not appids:
        return set()
    conn = get_db()
    placeholders = ",".join("?" * len(appids))
    rows = conn.execute(
        f"SELECT steam_appid FROM installed_games WHERE steam_appid IN ({placeholders})", appids).fetchall()
    conn.close()
    return {row["steam_appid"] for row in rows}


def list_installed_games():
    conn = get_db()
    rows = conn.execute("SELECT * FROM installed_games ORDER BY detected_at DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def is_installed(steam_appid):
    conn = get_db()
    row = conn.execute("SELECT 1 FROM installed_games WHERE steam_appid=?", (steam_appid,)).fetchone()
    conn.close()
    return row is not None


# ---------------------------------------------------------------------------
# Scan matches (fuzzy suggestions awaiting admin review - see scanner.py)
# ---------------------------------------------------------------------------
def upsert_scan_match(folder_name, folder_path, request_id, score):
    """Creates a pending suggestion, or refreshes the score of an existing
    one - but never resurrects one the admin already rejected for this exact
    folder+request pairing (see the UNIQUE constraint and scanner.py's
    scan_once(), which checks status before calling this at all)."""
    conn = get_db()
    conn.execute(
        "INSERT INTO scan_matches (folder_name, folder_path, request_id, score, status, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?) "
        "ON CONFLICT(folder_path, request_id) DO UPDATE SET score=excluded.score",
        (folder_name, folder_path, request_id, score, now_iso()))
    conn.commit()
    conn.close()


def list_pending_scan_matches():
    conn = get_db()
    rows = conn.execute(
        "SELECT scan_matches.*, requests.name AS request_name, requests.icon_url AS request_icon_url, "
        "requests.steam_appid AS request_appid "
        "FROM scan_matches JOIN requests ON requests.id = scan_matches.request_id "
        "WHERE scan_matches.status='pending' ORDER BY scan_matches.score DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_scan_match(match_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM scan_matches WHERE id=?", (match_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_rejected_pairs(folder_path):
    """Every request_id already rejected for this exact folder, so a re-scan
    doesn't re-suggest a pairing the admin already said no to."""
    conn = get_db()
    rows = conn.execute(
        "SELECT request_id FROM scan_matches WHERE folder_path=? AND status='rejected'",
        (folder_path,)).fetchall()
    conn.close()
    return {row["request_id"] for row in rows}


def set_scan_match_status(match_id, status):
    if status not in ("pending", "confirmed", "rejected"):
        raise ValueError(f"Unknown scan match status: {status!r}")
    conn = get_db()
    conn.execute("UPDATE scan_matches SET status=? WHERE id=?", (status, match_id))
    conn.commit()
    conn.close()


def delete_scan_matches_for_folder(folder_path):
    """Called when a folder gets an exact AppID match (a rename, or a
    sidecar file added) - any pending fuzzy suggestions for that same folder
    are now moot."""
    conn = get_db()
    conn.execute("DELETE FROM scan_matches WHERE folder_path=? AND status='pending'", (folder_path,))
    conn.commit()
    conn.close()
