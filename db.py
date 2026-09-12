"""
db.py — The entire database layer (SQLite). No ORM, plain SQL, hand-rolled
schema management: see CLAUDE.md's "No ORM, no migration framework" rule.
"""
import os
import secrets
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
    # Cached Steam details for a matched row, shown on the public /collections
    # page - fetched once when a folder becomes matched (scanner.py reuses
    # them on later scans rather than re-fetching from Steam every cycle for
    # a folder that never changes) and left alone entirely for anything not
    # yet matched.
    _ensure_column(conn, "installed_games", "name", "TEXT")
    _ensure_column(conn, "installed_games", "icon_url", "TEXT")
    _ensure_column(conn, "installed_games", "short_description", "TEXT")
    # When this row first became 'matched' - deliberately NOT updated_at,
    # which every scan touches regardless of whether anything actually
    # changed. Powers the public "Recently added" strip (see
    # recently_matched_games() below); scanner.py is responsible for only
    # ever advancing this on a genuine new match, never on a routine rescan
    # of something already matched.
    _ensure_column(conn, "installed_games", "matched_at", "TEXT")
    # Steam's appdetails genre list, comma-joined (e.g. "Action,Adventure") -
    # cached the same way name/icon_url/short_description are (fetched once,
    # reused on later scans). NULL means "never fetched under this schema",
    # distinct from "" (fetched, Steam listed no genres) - see
    # scanner._cached_or_fetched_details()'s docstring for why that
    # distinction matters (it's what forces exactly one backfill fetch for
    # rows matched before this column existed).
    _ensure_column(conn, "installed_games", "genres", "TEXT")

    # Configured root folders (see scanner.py) - one row per folder, admin-
    # managed from /admin/scanner. `path` is the real, server-side filesystem
    # path scanner.py actually walks. `client_path`, separately, is what a
    # *visitor* is told on /collections - the server's own path is frequently
    # meaningless (or even a bit revealing) to someone else on the network:
    # the admin might scan `D:\Games` on the server itself, while a visitor
    # reaches the same share as `\\HOMESERVER\Games` or a totally different
    # mapped drive letter on their own machine. Left blank, the server path is
    # shown as a fallback (visible in the admin UI as clearly the server's own
    # path, not asserted to be what a visitor should type).
    c.execute("""
        CREATE TABLE IF NOT EXISTS games_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL DEFAULT '',
            client_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)

    # Admin-maintained: a Steam AppID nobody should be able to request, with an
    # optional reason shown back to the admin (never to visitors - see
    # app.py's submit_request). Real admin-authored data, not a rebuildable
    # cache, but deliberately NOT added to RESTORE_REQUIRED_TABLES above - the
    # same reasoning as installed_games: a backup taken before this table
    # existed must stay restorable.
    c.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            steam_appid INTEGER NOT NULL UNIQUE,
            name TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)

    # Per-visitor override of the global request-limit setting (see
    # get_global_request_limit()/set_setting below) - one row per Jellyfin
    # user id that has ever been given a specific override. A user with no
    # row here is simply governed by the global limit.
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_request_limits (
            requested_by_id TEXT PRIMARY KEY,
            requested_by_name TEXT NOT NULL DEFAULT '',
            period TEXT NOT NULL,
            limit_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # One row per (label, url) pointing back at status-portal - this server's
    # "home". Purely a display list for the admin-configured header link (see
    # app.py's /admin/integrations and base.html) - status-portal itself never
    # reads this table; it's not part of the health/notify contract.
    c.execute("""
        CREATE TABLE IF NOT EXISTS status_portal_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            url TEXT NOT NULL,
            created_at TEXT NOT NULL
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
INSTALLED_GAME_STATUSES = ("matched", "pending_review", "unmatched", "deleted")


def upsert_scanned_folder(root_path, folder_name, status, steam_appid=None, candidate_appid=None,
                           candidate_name=None, candidate_score=None, name=None, icon_url=None,
                           short_description=None, matched_at=None, genres=None):
    """Inserts or updates one folder's row by (root_path, folder_name) - its
    unique key now that more than one root folder can be configured (two
    disks can each have a same-named subfolder). Called once per folder, per
    scan pass, by scanner.scan_once().

    name/icon_url/short_description/genres are the cached Steam details shown
    on the public /collections page for a matched row - only ever passed for a
    'matched' status; left NULL (and therefore untouched by this INSERT's
    own defaults) for pending_review/unmatched rows, which have no confirmed
    game to describe yet. matched_at is likewise only meaningful for
    'matched' - the caller (scanner.py) is responsible for resolving it to
    either a preserved old value (already matched, nothing new) or a fresh
    timestamp (a genuinely new match), never recomputing it here - this
    function just stores whatever it's given."""
    if status not in INSTALLED_GAME_STATUSES:
        raise ValueError(f"Unknown installed_games status: {status!r}")
    conn = get_db()
    ts = now_iso()
    conn.execute("""
        INSERT INTO installed_games
            (root_path, folder_name, steam_appid, status, candidate_appid, candidate_name,
             candidate_score, name, icon_url, short_description, matched_at, genres,
             created_at, updated_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(root_path, folder_name) DO UPDATE SET
            steam_appid=excluded.steam_appid,
            status=excluded.status,
            candidate_appid=excluded.candidate_appid,
            candidate_name=excluded.candidate_name,
            candidate_score=excluded.candidate_score,
            name=excluded.name,
            icon_url=excluded.icon_url,
            short_description=excluded.short_description,
            matched_at=excluded.matched_at,
            genres=excluded.genres,
            updated_at=excluded.updated_at,
            last_seen_at=excluded.last_seen_at
    """, (root_path, folder_name, steam_appid, status, candidate_appid, candidate_name,
          candidate_score, name, icon_url, short_description, matched_at, genres, ts, ts, ts))
    conn.commit()
    row = conn.execute("SELECT id FROM installed_games WHERE root_path=? AND folder_name=?",
                        (root_path, folder_name)).fetchone()
    conn.close()
    return row["id"]


def delete_scanned_folder(row_id):
    """Permanently forgets one installed_games row - used by the admin's
    "Forget" action on a 'deleted' row (see app.py's
    admin_scanner_forget_deleted). Not used for the ordinary matched/
    pending_review/unmatched lifecycle, which prune_scanned_folders() handles
    on its own."""
    conn = get_db()
    conn.execute("DELETE FROM installed_games WHERE id=?", (row_id,))
    conn.commit()
    conn.close()


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


def mark_folder_matched(row_id, folder_name, steam_appid, name=None, icon_url=None,
                         short_description=None, matched_at=None, genres=None):
    """Called after scanner.confirm_match() successfully renames a folder on
    disk to embed its confirmed AppID tag - updates the row's folder_name
    (root_path never changes; a confirmed match is always renamed in place,
    never moved between roots), caches the Steam details shown on
    /collections, stamps matched_at (see upsert_scanned_folder's docstring -
    the caller resolves whether this is a preserved or fresh timestamp), and
    clears the now-stale fuzzy-candidate fields."""
    conn = get_db()
    ts = now_iso()
    conn.execute("""
        UPDATE installed_games
        SET folder_name=?, steam_appid=?, status='matched',
            candidate_appid=NULL, candidate_name=NULL, candidate_score=NULL,
            name=?, icon_url=?, short_description=?, matched_at=?, genres=?,
            updated_at=?, last_seen_at=?
        WHERE id=?
    """, (folder_name, steam_appid, name, icon_url, short_description, matched_at, genres,
          ts, ts, row_id))
    conn.commit()
    conn.close()


def recently_matched_games(limit=8):
    """The most recently matched games, newest first - powers the public
    "Recently added" strip on the search page. Only rows with a matched_at
    stamp are eligible (a row created before that column existed has none
    yet - it'll get one the next time scanner.py actually re-resolves it,
    not retroactively)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM installed_games WHERE status='matched' AND matched_at IS NOT NULL "
        "ORDER BY matched_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


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
    """Reconciles installed_games against what this scan pass actually found
    on disk, in one of three ways:

    - the row's root_path is no longer configured at all (the admin removed
      it from the games-folders list) - a deliberate configuration change,
      always safe to delete outright regardless of status;
    - the row's root_path *is* still configured and was successfully listed
      this scan pass (scanned_roots), so we positively know what's currently
      there, but this particular (root_path, folder_name) wasn't in it, and
      the row was 'matched' - its folder was actually deleted server-side.
      Marked 'deleted' rather than removed: matched_appids() only counts
      'matched' rows, so a deleted game immediately becomes re-requestable
      again, but the admin keeps a visible record of it (and if the same
      folder name reappears with its tag intact, the very next scan finds it
      via existing_by_key and flips it straight back to 'matched') - see
      scanner.scan_once() and app.py's /admin/scanner "deleted" panel;
    - same as above but the row wasn't 'matched' (pending_review/unmatched) -
      there's no confirmed game identity worth preserving, so it's deleted
      outright, same as before this distinction existed. A row already
      'deleted' that's still missing is left alone rather than touched again.

    A row whose root is still configured but wasn't successfully scanned
    this pass (a disk that's temporarily offline, say) is left completely
    untouched either way - never treated as "gone" over a transient failure.
    `seen_pairs` is a set of (root_path, folder_name) tuples actually found
    this pass, across every root that was successfully listed."""
    conn = get_db()
    rows = conn.execute("SELECT id, root_path, folder_name, status FROM installed_games").fetchall()
    delete_ids = []
    newly_deleted_ids = []
    for row in rows:
        root, name, status = row["root_path"], row["folder_name"], row["status"]
        if root not in configured_roots:
            delete_ids.append(row["id"])
        elif root in scanned_roots and (root, name) not in seen_pairs:
            if status == "matched":
                newly_deleted_ids.append(row["id"])
            elif status != "deleted":
                delete_ids.append(row["id"])
    if newly_deleted_ids:
        ts = now_iso()
        placeholders = ",".join("?" * len(newly_deleted_ids))
        conn.execute(f"UPDATE installed_games SET status='deleted', updated_at=? "
                     f"WHERE id IN ({placeholders})", [ts, *newly_deleted_ids])
    if delete_ids:
        placeholders = ",".join("?" * len(delete_ids))
        conn.execute(f"DELETE FROM installed_games WHERE id IN ({placeholders})", delete_ids)
    if newly_deleted_ids or delete_ids:
        conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Configured games folders (see scanner.py). Deleting a folder here doesn't
# cascade-delete its installed_games rows immediately - the next scan prunes
# them naturally, the same as a folder that simply stopped being configured
# (see prune_scanned_folders() above), so there's nothing extra to do here.
# ---------------------------------------------------------------------------
def add_games_folder(path, label="", client_path=""):
    """Returns the new row's id, or None if this path is already configured
    (its UNIQUE constraint) - the caller reports that as a friendly message
    rather than a raw IntegrityError."""
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO games_folders (path, label, client_path, created_at) VALUES (?, ?, ?, ?)",
            (path, label, client_path, now_iso()))
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def list_games_folders():
    conn = get_db()
    rows = conn.execute("SELECT * FROM games_folders ORDER BY label, path").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_games_folder(folder_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM games_folders WHERE id=?", (folder_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_games_folder(folder_id, label, client_path):
    """Only label/client_path are editable - path itself is immutable once
    added (changing what scanner.py actually walks is a big enough change
    that it should be a remove-and-re-add, not a quiet edit)."""
    conn = get_db()
    conn.execute("UPDATE games_folders SET label=?, client_path=? WHERE id=?",
                 (label, client_path, folder_id))
    conn.commit()
    conn.close()


def delete_games_folder(folder_id):
    conn = get_db()
    conn.execute("DELETE FROM games_folders WHERE id=?", (folder_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Blacklist - AppIDs the admin never wants requested again, with an optional
# reason (admin-facing only, never shown to visitors). See app.py's
# submit_request (the enforcement point) and /admin/blacklist.
# ---------------------------------------------------------------------------
def add_to_blacklist(steam_appid, name, reason=""):
    """Returns the new row's id, or None if this appid is already
    blacklisted (its UNIQUE constraint) - the caller reports that as a
    friendly message rather than a raw IntegrityError."""
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO blacklist (steam_appid, name, reason, created_at) VALUES (?, ?, ?, ?)",
            (steam_appid, name, reason, now_iso()))
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def list_blacklist():
    conn = get_db()
    rows = conn.execute("SELECT * FROM blacklist ORDER BY name, steam_appid").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_blacklist_entry(steam_appid):
    conn = get_db()
    row = conn.execute("SELECT * FROM blacklist WHERE steam_appid=?", (steam_appid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def blacklist_map(appids):
    """appid -> reason for every one of `appids` that's blacklisted - one
    query for a whole page of search results instead of one per result, same
    shape as active_request_appids() above."""
    if not appids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(appids))
    rows = conn.execute(
        f"SELECT steam_appid, reason FROM blacklist WHERE steam_appid IN ({placeholders})", appids).fetchall()
    conn.close()
    return {row["steam_appid"]: row["reason"] for row in rows}


def is_blacklisted(steam_appid):
    return get_blacklist_entry(steam_appid) is not None


def remove_from_blacklist(blacklist_id):
    conn = get_db()
    conn.execute("DELETE FROM blacklist WHERE id=?", (blacklist_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Request limits - a global default (settings key below) plus a per-user
# override table, mirroring Seerr's "global quota, per-user override" shape.
# See app.py's submit_request (the enforcement point) and /admin/limits.
# ---------------------------------------------------------------------------
REQUEST_LIMIT_PERIODS = ("none", "daily", "weekly", "monthly")

GLOBAL_REQUEST_LIMIT_PERIOD_SETTING = "global_request_limit_period"
GLOBAL_REQUEST_LIMIT_COUNT_SETTING = "global_request_limit_count"


def get_global_request_limit():
    """(period, count) - period 'none' means unlimited (the default), in
    which case count is meaningless and not read."""
    period = get_setting(GLOBAL_REQUEST_LIMIT_PERIOD_SETTING, "none")
    if period not in REQUEST_LIMIT_PERIODS:
        period = "none"
    raw_count = get_setting(GLOBAL_REQUEST_LIMIT_COUNT_SETTING, "0")
    count = int(raw_count) if raw_count.isdigit() else 0
    return period, count


def set_global_request_limit(period, count):
    if period not in REQUEST_LIMIT_PERIODS:
        raise ValueError(f"Unknown request-limit period: {period!r}")
    set_setting(GLOBAL_REQUEST_LIMIT_PERIOD_SETTING, period)
    set_setting(GLOBAL_REQUEST_LIMIT_COUNT_SETTING, str(max(0, int(count))))


def get_user_request_limit(requested_by_id):
    """This user's own override (period, count), or None if they're governed
    by the global limit instead."""
    conn = get_db()
    row = conn.execute("SELECT period, limit_count FROM user_request_limits WHERE requested_by_id=?",
                        (requested_by_id,)).fetchone()
    conn.close()
    return (row["period"], row["limit_count"]) if row else None


def set_user_request_limit(requested_by_id, requested_by_name, period, count):
    if period not in REQUEST_LIMIT_PERIODS:
        raise ValueError(f"Unknown request-limit period: {period!r}")
    conn = get_db()
    conn.execute("""
        INSERT INTO user_request_limits (requested_by_id, requested_by_name, period, limit_count, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(requested_by_id) DO UPDATE SET
            requested_by_name=excluded.requested_by_name,
            period=excluded.period,
            limit_count=excluded.limit_count,
            updated_at=excluded.updated_at
    """, (requested_by_id, requested_by_name, period, max(0, int(count)), now_iso()))
    conn.commit()
    conn.close()


def clear_user_request_limit(requested_by_id):
    conn = get_db()
    conn.execute("DELETE FROM user_request_limits WHERE requested_by_id=?", (requested_by_id,))
    conn.commit()
    conn.close()


def list_user_request_limits():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM user_request_limits ORDER BY requested_by_name, requested_by_id").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def distinct_requesters():
    """Every visitor who has ever made a request, deduplicated by id - this
    app has no synced Jellyfin user directory (see ROADMAP.md), so this list
    is the only way the admin can pick a visitor to set a per-user limit
    override for."""
    conn = get_db()
    rows = conn.execute("""
        SELECT requested_by_id, requested_by_name, MAX(created_at) AS last_requested_at
        FROM requests GROUP BY requested_by_id ORDER BY requested_by_name
    """).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def count_requests_since(requested_by_id, since_iso):
    """How many non-rejected requests this visitor has made at or after
    since_iso - a rejected request doesn't count against their quota, same
    reasoning as get_active_request_for_appid() above: being told no
    shouldn't also burn the visitor's limited requests for the period."""
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM requests WHERE requested_by_id=? AND status != 'rejected' "
        "AND created_at >= ?", (requested_by_id, since_iso)).fetchone()
    conn.close()
    return row["n"]


def count_unresolved_requests():
    """Requests still awaiting action - not yet 'done' or 'rejected'. Powers
    /health's optional pending_requests count for status-portal."""
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM requests WHERE status NOT IN ('done', 'rejected')").fetchone()
    conn.close()
    return row["n"]


# ---------------------------------------------------------------------------
# status-portal integration - health-check API key (see app.py's GET /health)
# and the admin-configured "home" links rendered in the page header (see
# base.html). Both are DB-backed, admin-editable from /admin/integrations -
# not env vars, so setting them up needs no restart, same reasoning as every
# other admin-tunable toggle in this app (see CLAUDE.md's "Config split").
# ---------------------------------------------------------------------------
HEALTH_API_KEY_SETTING = "health_api_key"


def get_health_api_key():
    return get_setting(HEALTH_API_KEY_SETTING)


def regenerate_health_api_key():
    key = secrets.token_hex(32)
    set_setting(HEALTH_API_KEY_SETTING, key)
    return key


def get_or_create_health_api_key():
    """Returns the current key, generating one on first use so the admin
    always has something to view/copy on their very first visit to
    /admin/integrations rather than an empty field with no way to fill it in
    short of pressing "Regenerate" once first."""
    key = get_health_api_key()
    if not key:
        key = regenerate_health_api_key()
    return key


def add_status_portal_link(label, url):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO status_portal_links (label, url, created_at) VALUES (?, ?, ?)",
        (label, url, now_iso()))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def list_status_portal_links():
    conn = get_db()
    rows = conn.execute("SELECT * FROM status_portal_links ORDER BY id").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_status_portal_link(link_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM status_portal_links WHERE id=?", (link_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_status_portal_link(link_id, label, url):
    conn = get_db()
    conn.execute("UPDATE status_portal_links SET label=?, url=? WHERE id=?", (label, url, link_id))
    conn.commit()
    conn.close()


def delete_status_portal_link(link_id):
    conn = get_db()
    conn.execute("DELETE FROM status_portal_links WHERE id=?", (link_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# status-portal integration - notification delegation (see
# status_portal_client.py). Different direction, different secret, from the
# health API key above: that key is one *this* app issues for status-portal to
# call in; this one status-portal issues for *this* app to call out with.
# Both DB-backed, admin-editable from /admin/integrations, no restart needed.
# ---------------------------------------------------------------------------
STATUS_PORTAL_NOTIFY_URL_SETTING = "status_portal_notify_url"
STATUS_PORTAL_NOTIFY_API_KEY_SETTING = "status_portal_notify_api_key"


def get_status_portal_notify_url():
    return get_setting(STATUS_PORTAL_NOTIFY_URL_SETTING, "") or ""


def set_status_portal_notify_url(url):
    set_setting(STATUS_PORTAL_NOTIFY_URL_SETTING, url)


def get_status_portal_notify_api_key():
    return get_setting(STATUS_PORTAL_NOTIFY_API_KEY_SETTING, "") or ""


def set_status_portal_notify_api_key(key):
    set_setting(STATUS_PORTAL_NOTIFY_API_KEY_SETTING, key)


# Per-event on/off toggles - both default enabled (matches this feature's
# behavior before these toggles existed), so an admin who never visits this
# setting keeps getting notified exactly as before. Same "1"/"0" string
# convention as updater.py's update_check_enabled().
NOTIFY_ON_NEW_REQUEST_SETTING = "notify_on_new_request"
NOTIFY_ON_STATUS_CHANGE_SETTING = "notify_on_status_change"


def notify_on_new_request_enabled():
    return get_setting(NOTIFY_ON_NEW_REQUEST_SETTING, "1") != "0"


def set_notify_on_new_request_enabled(enabled):
    set_setting(NOTIFY_ON_NEW_REQUEST_SETTING, "1" if enabled else "0")


def notify_on_status_change_enabled():
    return get_setting(NOTIFY_ON_STATUS_CHANGE_SETTING, "1") != "0"


def set_notify_on_status_change_enabled(enabled):
    set_setting(NOTIFY_ON_STATUS_CHANGE_SETTING, "1" if enabled else "0")
