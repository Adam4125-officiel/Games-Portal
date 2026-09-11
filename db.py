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


# ---------------------------------------------------------------------------
# Manual backup/restore (see app.py's /admin/about/backup-db and
# /admin/about/restore-db). Simpler than a WAL-mode setup would need: this
# database runs SQLite's default rollback-journal mode (nothing here ever
# sets PRAGMA journal_mode), so there's no -wal/-shm sidecar dance - only a
# possible stray -journal left behind by an interrupted write.
# ---------------------------------------------------------------------------
def backup_to_file(dest_path):
    """Writes a consistent snapshot of the live database to dest_path via
    SQLite's own online backup API - safe to call while another connection is
    mid-write, unlike a plain file copy, which could catch a torn transaction."""
    source = sqlite3.connect(DB_PATH)
    dest = sqlite3.connect(dest_path)
    with dest:
        source.backup(dest)
    source.close()
    dest.close()


# Every SQLite file starts with this exact 16-byte string - checked first
# because it rejects the overwhelmingly common mistake (the wrong file, a
# renamed non-database) instantly, without handing untrusted bytes to SQLite.
SQLITE_HEADER = b"SQLite format 3\x00"

# Tables a file must contain before this app accepts it as *its own* backup.
# The header and an integrity check together only prove "a valid SQLite
# database," which plenty of unrelated files also are - restoring one of
# those would wipe this app's own data and likely leave it unable to start.
# Deliberately excludes installed_games (the scanner's table, added after
# requests/settings already existed) so a backup taken before the scanner
# existed stays restorable.
RESTORE_REQUIRED_TABLES = ("settings", "requests")


def validate_backup_file(path):
    """Returns None if `path` is a well-formed SQLite database that looks
    like this app's own, otherwise a string explaining why not - meant to be
    shown to the admin verbatim. Runs entirely against a temporary copy;
    nothing here touches the live database."""
    try:
        with open(path, "rb") as f:
            header = f.read(len(SQLITE_HEADER))
    except OSError as e:
        return f"Could not read the uploaded file: {e}"
    if header != SQLITE_HEADER:
        return "That file isn't a SQLite database (its header doesn't match)."

    conn = None
    try:
        # Read-only, via a URI, so validating a file can never have side
        # effects even if the path were somehow wrong.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            detail = result[0] if result else "no result"
            return f"That database failed SQLite's integrity check ({detail})."
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError as e:
        return f"That file couldn't be opened as a database: {e}"
    finally:
        if conn is not None:
            conn.close()

    missing = [t for t in RESTORE_REQUIRED_TABLES if t not in names]
    if missing:
        return ("That's a valid SQLite database, but it isn't a games-portal backup - "
                f"it has no {', '.join(missing)} table(s).")
    return None


def restore_from_file(src_path):
    """Replaces the live database with `src_path`. Assumes it has already
    been validated - this does the dangerous part, not the deciding.
    os.replace() is atomic on both platforms, so a crash mid-restore leaves
    either the old database or the new one, never half of either."""
    os.replace(src_path, DB_PATH)
    try:
        os.remove(DB_PATH + "-journal")
    except FileNotFoundError:
        pass


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

    # installed_games predates multi-folder support and used to key rows by
    # folder_name alone (globally unique) - that breaks the moment two
    # configured root folders (two disks, say) each have a same-named
    # subfolder. Rather than an in-place ALTER TABLE to change a UNIQUE
    # constraint (SQLite has no direct syntax for that; it's a full
    # rebuild-the-table dance either way), this table is dropped and
    # recreated on the old shape - deliberately safe to do, unlike
    # _ensure_column()'s data-preserving approach below: every row here is
    # scanner.py's own rebuildable cache, not real user data (this table is
    # already excluded from the backup/restore required-tables check for
    # exactly that reason), and the very next scan repopulates it.
    old_columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(installed_games)").fetchall()}
    if old_columns and "root_path" not in old_columns:
        conn.execute("DROP TABLE installed_games")

    # One row per subfolder scanner.py has ever seen under one of its
    # configured root folders. Deliberately not part of the backup/restore
    # required-tables check in validate_backup_file() below - a backup taken
    # before the scanner existed must stay restorable.
    c.execute("""
        CREATE TABLE IF NOT EXISTS installed_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root_path TEXT NOT NULL,
            folder_name TEXT NOT NULL,
            steam_appid INTEGER,
            status TEXT NOT NULL DEFAULT 'unmatched',  -- matched | pending_review | unmatched
            candidate_appid INTEGER,
            candidate_name TEXT,
            candidate_score REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(root_path, folder_name)
        )
    """)
    # CREATE TABLE IF NOT EXISTS is a silent no-op against a table that already
    # exists under an older/incomplete shape - a real install has hit this
    # before (see docs/HISTORY.md): an earlier in-app update once applied a
    # since-superseded version of this table, and CREATE TABLE IF NOT EXISTS
    # never touched the live one. _ensure_column() is idempotent, so this is
    # safe to run on every startup regardless of whether the table was just
    # freshly (re)created above.
    _ensure_column(conn, "installed_games", "steam_appid", "INTEGER")
    _ensure_column(conn, "installed_games", "status", "TEXT NOT NULL DEFAULT 'unmatched'")
    _ensure_column(conn, "installed_games", "candidate_appid", "INTEGER")
    _ensure_column(conn, "installed_games", "candidate_name", "TEXT")
    _ensure_column(conn, "installed_games", "candidate_score", "REAL")
    _ensure_column(conn, "installed_games", "created_at", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "installed_games", "updated_at", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "installed_games", "last_seen_at", "TEXT NOT NULL DEFAULT ''")

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


def upsert_scanned_folder(root_path, folder_name, status, steam_appid=None, candidate_appid=None,
                           candidate_name=None, candidate_score=None):
    """Inserts or updates one folder's row by (root_path, folder_name) - its
    unique key now that more than one root folder can be configured (two
    disks can each have a same-named subfolder). Called once per folder, per
    scan pass, by scanner.scan_once()."""
    if status not in INSTALLED_GAME_STATUSES:
        raise ValueError(f"Unknown installed_games status: {status!r}")
    conn = get_db()
    ts = now_iso()
    conn.execute("""
        INSERT INTO installed_games
            (root_path, folder_name, steam_appid, status, candidate_appid, candidate_name,
             candidate_score, created_at, updated_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(root_path, folder_name) DO UPDATE SET
            steam_appid=excluded.steam_appid,
            status=excluded.status,
            candidate_appid=excluded.candidate_appid,
            candidate_name=excluded.candidate_name,
            candidate_score=excluded.candidate_score,
            updated_at=excluded.updated_at,
            last_seen_at=excluded.last_seen_at
    """, (root_path, folder_name, steam_appid, status, candidate_appid, candidate_name,
          candidate_score, ts, ts, ts))
    conn.commit()
    row = conn.execute("SELECT id FROM installed_games WHERE root_path=? AND folder_name=?",
                        (root_path, folder_name)).fetchone()
    conn.close()
    return row["id"]


def list_scanned_folders(status=None):
    conn = get_db()
    if status:
        rows = conn.execute(
            "SELECT * FROM installed_games WHERE status=? ORDER BY root_path, folder_name",
            (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM installed_games ORDER BY root_path, folder_name").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_scanned_folder(row_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM installed_games WHERE id=?", (row_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_folder_matched(row_id, folder_name, steam_appid):
    """Called after scanner.confirm_match() successfully renames a folder on
    disk to embed its confirmed AppID tag - updates the row's folder_name
    (root_path never changes; a confirmed match is always renamed in place,
    never moved between roots) and clears the now-stale fuzzy-candidate
    fields."""
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


def prune_scanned_folders(configured_roots, scanned_roots, seen_pairs):
    """Removes rows that are stale in one of two distinct ways:

    - the row's root_path is no longer configured at all (the admin removed
      it from the games-folders list) - a deliberate configuration change,
      always safe to prune;
    - the row's root_path *is* still configured and was successfully listed
      this scan pass (scanned_roots), so we positively know what's currently
      there, but this particular (root_path, folder_name) wasn't in it.

    A row whose root is still configured but wasn't successfully scanned
    this pass (a disk that's temporarily offline, say) is left completely
    untouched either way - never treated as "gone" over a transient failure.
    `seen_pairs` is a set of (root_path, folder_name) tuples actually found
    this pass, across every root that was successfully listed."""
    conn = get_db()
    rows = conn.execute("SELECT id, root_path, folder_name FROM installed_games").fetchall()
    stale_ids = []
    for row in rows:
        root, name = row["root_path"], row["folder_name"]
        if root not in configured_roots:
            stale_ids.append(row["id"])
        elif root in scanned_roots and (root, name) not in seen_pairs:
            stale_ids.append(row["id"])
    if stale_ids:
        placeholders = ",".join("?" * len(stale_ids))
        conn.execute(f"DELETE FROM installed_games WHERE id IN ({placeholders})", stale_ids)
        conn.commit()
    conn.close()
