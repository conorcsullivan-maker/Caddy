"""SQLite setup, schema/migrations, and connection management."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from security import hash_pin_secure, SESSION_MAX_AGE_DAYS

BASE_DIR = Path(__file__).parent
# Allow DB to live on a persistent disk in production (Render mounts at /data)
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "caddy.db"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db(immediate: bool = False):
    """Yield a row-factory connection that commits on clean exit.

    immediate=True opens the transaction as a writer up front
    (BEGIN IMMEDIATE) — use it for read-modify-write sequences on JSON
    columns (shot_stats etc.) so two concurrent writers can't interleave
    between the read and the write and silently lose one update."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pin_hash TEXT,
            full_name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            reason TEXT,
            referral TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            is_admin INTEGER DEFAULT 0,
            onboarded INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            approved_at TEXT,
            bag TEXT,
            driver_miss TEXT,
            iron_miss TEXT,
            home_course TEXT,
            rounds TEXT,
            handicap_index REAL,
            tendencies_summary TEXT,
            conversation_history TEXT,
            active_round_state TEXT
        )
    """)
    # Conversations archive — every chat preserved, never discarded
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'casual',
            course_name TEXT,
            total_score INTEGER,
            messages TEXT NOT NULL,
            round_metadata TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    # Migration safety: ALTER TABLE for any column added after the original schema
    for col_def in (
        "ALTER TABLE users ADD COLUMN referral TEXT",
        "ALTER TABLE users ADD COLUMN conversation_history TEXT",
        "ALTER TABLE users ADD COLUMN active_round_state TEXT",
        "ALTER TABLE users ADD COLUMN on_course_shots TEXT",
        "ALTER TABLE users ADD COLUMN shot_stats TEXT",
        "ALTER TABLE users ADD COLUMN trackman_session_ids TEXT",
    ):
        try:
            c.execute(col_def)
        except sqlite3.OperationalError:
            pass  # column already exists
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    # Course geometry cache. Courses don't move, so successful fetches live
    # forever; failed fetches (no_data) can be retried after a week. Keyed
    # by source+id so we don't collide with future synthetic-course IDs.
    c.execute("""
        CREATE TABLE IF NOT EXISTS course_geometry (
            source TEXT NOT NULL,
            course_id TEXT NOT NULL,
            club_name TEXT,
            has_data INTEGER NOT NULL DEFAULT 0,
            geometry_json TEXT,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (source, course_id)
        )
    """)
    conn.commit()
    conn.close()


def seed_initial_admin():
    """If BOOTSTRAP_ADMIN_* env vars are set and no admins exist yet,
    create that user as an approved admin. Lets you bootstrap a fresh
    production database without using a shell."""
    username = os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "").strip().lower()
    pin = os.environ.get("BOOTSTRAP_ADMIN_PIN", "").strip()
    full_name = os.environ.get("BOOTSTRAP_ADMIN_NAME", "Admin").strip()
    if not username or not pin:
        return
    with db() as conn:
        admin_count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 1"
        ).fetchone()[0]
        if admin_count > 0:
            return
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            # Promote existing user to admin
            conn.execute(
                "UPDATE users SET is_admin = 1, status = 'approved' WHERE username = ?",
                (username,),
            )
            print(f"Promoted existing user '{username}' to admin")
        else:
            conn.execute(
                """INSERT INTO users
                   (username, pin_hash, full_name, status, is_admin, onboarded, created_at, approved_at)
                   VALUES (?, ?, ?, 'approved', 1, 0, ?, ?)""",
                (username, hash_pin_secure(pin), full_name, now_iso(), now_iso()),
            )
            print(f"Bootstrapped admin user '{username}'")


def purge_expired_sessions():
    """Delete session tokens older than the cookie lifetime. Before this,
    tokens outlived their cookies forever — a leaked DB row was a permanent
    credential."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SESSION_MAX_AGE_DAYS)).isoformat()
    with db() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))
        if cur.rowcount:
            print(f"Purged {cur.rowcount} expired session(s)")
