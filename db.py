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

    # One row per subfolder scanner.py has ever seen under PORTAL_GAMES_FOLDER.
    # Deliberately not part of the backup/restore required-tables check in
    # validate_backup_file() below - a backup taken before the scanner existed
    # must stay restorable.
    c.execute("""
        CREATE TABLE IF NOT EXISTS installed_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_name TEXT NOT NULL UNIQUE,
            steam_appid INTEGER,
            status TEXT NOT NULL DEFAULT 'unmatched',  -- matched | pending_review | unmatched
            candidate_appid INTEGER,
            candidate_name TEXT,
            candidate_score REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
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


def list_requests(status=None, requested_by_id=None):
    """All requests, newest first - optionally narrowed to one status and/or
    one requester. The `requested_by_id` filter is what keeps the "my requests"
    page (app.py's /my-requests) scoped to a single visitor's own data."""
    clauses, params = [], []
    if status:
        clauses.append("status=?")
        params.append(status)
    if requested_by_id:
        clauses.append("requested_by_id=?")
        params.append(requested_by_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = get_db()
    rows = conn.execute(f"SELECT * FROM requests {where} ORDER BY created_at DESC", params).fetchall()
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


def delete_request(request_id):
    conn = get_db()
    conn.execute("DELETE FROM requests WHERE id=?", (request_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Installed-games scanner (see scanner.py)
# ---------------------------------------------------------------------------
INSTALLED_GAME_STATUSES = ("matched", "pending_review", "unmatched")


def upsert_scanned_folder(folder_name, status, steam_appid=None, candidate_appid=None,
                           candidate_name=None, candidate_score=None):
    """Inserts or updates one folder's row by folder_name (its unique key) -
    called once per folder, per scan pass, by scanner.scan_once()."""
    if status not in INSTALLED_GAME_STATUSES:
        raise ValueError(f"Unknown installed_games status: {status!r}")
    conn = get_db()
    ts = now_iso()
    cur = conn.execute("""
        INSERT INTO installed_games
            (folder_name, steam_appid, status, candidate_appid, candidate_name,
             candidate_score, created_at, updated_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(folder_name) DO UPDATE SET
            steam_appid=excluded.steam_appid,
            status=excluded.status,
            candidate_appid=excluded.candidate_appid,
            candidate_name=excluded.candidate_name,
            candidate_score=excluded.candidate_score,
            updated_at=excluded.updated_at,
            last_seen_at=excluded.last_seen_at
    """, (folder_name, steam_appid, status, candidate_appid, candidate_name,
          candidate_score, ts, ts, ts))
    conn.commit()
    row = conn.execute("SELECT id FROM installed_games WHERE folder_name=?", (folder_name,)).fetchone()
    conn.close()
    return row["id"]


def list_scanned_folders(status=None):
    conn = get_db()
    if status:
        rows = conn.execute("SELECT * FROM installed_games WHERE status=? ORDER BY folder_name",
                             (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM installed_games ORDER BY folder_name").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_scanned_folder(row_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM installed_games WHERE id=?", (row_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_folder_matched(row_id, folder_name, steam_appid):
    """Called after scanner.confirm_match() successfully renames a folder on
    disk to embed its confirmed AppID tag - updates the row's folder_name to
    match (folder_name is the table's unique key) and clears the now-stale
    fuzzy-candidate fields."""
    conn = get_db()
    ts = now_iso()
    conn.execute("""
        UPDATE installed_games
        SET folder_name=?, steam_appid=?, status='matched',
            candidate_appid=NULL, candidate_name=NULL, candidate_score=NULL,
            updated_at=?, last_seen_at=?
        WHERE id=?
    """, (folder_name, steam_appid, ts, ts, row_id))
    conn.commit()
    conn.close()


def matched_appids():
    """Every Steam AppID currently recognized as installed - used by app.py to
    hide the Request button / refuse a duplicate request for a game already
    present on disk."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT steam_appid FROM installed_games WHERE status='matched'").fetchall()
    conn.close()
    return {row["steam_appid"] for row in rows}


def prune_scanned_folders(seen_folder_names):
    """Removes rows for folders no longer present under GAMES_FOLDER. Called
    once per scan pass, after every currently-present folder has already been
    upserted - never with a possibly-incomplete listing (see
    scanner.scan_once()'s own guard against an os.listdir() failure, which
    skips the whole pass, prune included, rather than treating an empty/failed
    listing as "nothing is installed any more")."""
    conn = get_db()
    rows = conn.execute("SELECT id, folder_name FROM installed_games").fetchall()
    stale_ids = [row["id"] for row in rows if row["folder_name"] not in seen_folder_names]
    if stale_ids:
        placeholders = ",".join("?" * len(stale_ids))
        conn.execute(f"DELETE FROM installed_games WHERE id IN ({placeholders})", stale_ids)
        conn.commit()
    conn.close()
