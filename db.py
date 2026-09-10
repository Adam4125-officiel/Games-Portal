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
# Backup and restore (see app.py's /admin/backup)
# ---------------------------------------------------------------------------
def backup_to_file(dest_path):
    """Writes a consistent snapshot of the live database to dest_path via
    SQLite's own online backup API - a plain file copy of DB_PATH could catch
    a torn/partial write mid-transaction; Connection.backup() can't."""
    source = sqlite3.connect(DB_PATH)
    dest = sqlite3.connect(dest_path)
    with dest:
        source.backup(dest)
    source.close()
    dest.close()


# Every SQLite file starts with this exact 16-byte string. Checked first
# because it rejects the overwhelmingly common mistake (a renamed text file,
# the wrong file entirely) instantly, without handing the bytes to SQLite.
SQLITE_HEADER = b"SQLite format 3\x00"

# Tables a file must contain before this app accepts it as *its own* backup.
# The header and an integrity check together only prove "a valid SQLite
# database" - restoring some other app's database (or a Jellyfin library) would
# silently wipe this portal and likely leave it unable to start.
RESTORE_REQUIRED_TABLES = ("settings", "requests")


def validate_backup_file(path):
    """None if `path` is a well-formed SQLite database that looks like this
    app's own, otherwise a string explaining why not - the caller's whole job
    is telling the admin what was wrong with their file, and "that isn't a
    database" vs. "that's a database, but not this app's" is exactly what they
    need to hear. Runs entirely read-only against a staged copy; nothing here
    ever touches the live database."""
    try:
        with open(path, "rb") as f:
            header = f.read(len(SQLITE_HEADER))
    except OSError as e:
        return f"Could not read the uploaded file: {e}"
    if header != SQLITE_HEADER:
        return "That file isn't a SQLite database (its header doesn't match)."

    conn = None
    try:
        # Read-only, and via a URI so SQLite cannot create or modify anything
        # even if the path is wrong - a validation step must never have side
        # effects.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            detail = result[0] if result else "no result"
            return f"That database failed SQLite's integrity check ({detail})."
        names = {row[0] for row in
                 conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError as e:
        return f"That file couldn't be opened as a database: {e}"
    finally:
        if conn is not None:
            conn.close()

    missing = [t for t in RESTORE_REQUIRED_TABLES if t not in names]
    if missing:
        return ("That's a valid SQLite database, but it isn't a Games Portal backup - "
                f"it has no {', '.join(missing)} table(s).")
    return None


def restore_from_file(src_path):
    """Replaces the live database with `src_path`. Assumes it has already been
    validated - this does the dangerous part, not the deciding.

    Just an atomic rename: unlike status-portal, nothing here runs in WAL mode
    or keeps a pooled connection open across requests (see get_db() - every
    call opens and closes its own connection immediately), so there's no
    -wal/-shm sidecar to reconcile and no stale open handle to release first.
    os.replace() is atomic on both platforms, so a crash mid-restore leaves
    either the old database or the new one - never half of either - and the
    very next get_db() call anywhere in the app simply opens the replaced
    file, with no process restart required for it to take effect."""
    os.replace(src_path, DB_PATH)
    # Defensive: a backup produced by some other WAL-mode SQLite process could
    # in principle leave sidecars behind once opened for validation, even
    # though this app's own connections never create them.
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            os.remove(DB_PATH + suffix)
        except OSError:
            pass
