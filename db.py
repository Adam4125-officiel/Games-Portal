"""
db.py — The entire database layer (SQLite). No ORM, plain SQL, hand-rolled
schema management: see CLAUDE.md's "No ORM, no migration framework" rule.
"""
import os
import re
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

    # A local mirror of what Seerr holds for each linked account - see
    # seerr.py. Wiped and rewritten wholesale on every successful sync
    # (replace_seerr_contacts), never touched on a failed one, so an
    # unreachable Seerr can't erase contact details that were working.
    c.execute("""
        CREATE TABLE IF NOT EXISTS seerr_contacts (
            jellyfin_user_id TEXT PRIMARY KEY,
            seerr_user_id TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            discord_id TEXT NOT NULL DEFAULT '',
            synced_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def looks_like_email(value):
    """Whether `value` could be delivered to at all. Blank is False -
    "nothing here" and "something that isn't an address" both mean "can't
    send", and every caller treats them identically."""
    return bool(value) and bool(EMAIL_RE.match(value.strip()))


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
# Seerr contacts (see seerr.py) - READ side only touches this app's own
# database; nothing here ever calls out to Seerr.
# ---------------------------------------------------------------------------
def replace_seerr_contacts(contacts):
    """Full replace, in one transaction. Only ever called after a successful
    sync (see seerr.sync_seerr_contacts), so a failed one leaves the previous
    details completely intact rather than wiping everyone's contact info."""
    stamp = now_iso()
    conn = get_db()
    try:
        with conn:
            conn.execute("DELETE FROM seerr_contacts")
            conn.executemany(
                "INSERT INTO seerr_contacts (jellyfin_user_id, seerr_user_id, display_name, "
                "email, discord_id, synced_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(c["jellyfin_user_id"], str(c["seerr_user_id"]), c.get("display_name", ""),
                  c.get("email", ""), c.get("discord_id", ""), stamp)
                 for c in contacts if c.get("jellyfin_user_id")])
    finally:
        conn.close()


def get_seerr_contact(jellyfin_user_id):
    if not jellyfin_user_id:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM seerr_contacts WHERE jellyfin_user_id=?",
                        (jellyfin_user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def count_seerr_contacts():
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN email != '' OR discord_id != '' THEN 1 ELSE 0 END) AS with_contact "
        "FROM seerr_contacts").fetchone()
    conn.close()
    return {"total": row["total"] or 0, "with_contact": row["with_contact"] or 0}


def seerr_contacts_synced_at():
    conn = get_db()
    row = conn.execute("SELECT MAX(synced_at) AS ts FROM seerr_contacts").fetchone()
    conn.close()
    return row["ts"] if row else None
