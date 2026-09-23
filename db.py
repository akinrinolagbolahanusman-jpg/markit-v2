"""
Mark-it V2 — database layer.
SQLite file-based DB. On Render's free tier the filesystem is ephemeral
(wiped on redeploy), so for real persistence later, consider Render's
paid persistent disk or an external DB. Fine for launch.
"""
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = "markit.db"


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                tier TEXT NOT NULL DEFAULT 'free',       -- 'free' | 'tier1' | 'tier2'
                tier_expiry INTEGER,                     -- unix timestamp, NULL for free
                utc_offset REAL,                         -- e.g. 1.0 for Lagos (UTC+1)
                streak INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                hour INTEGER NOT NULL,                   -- local hour, 0-23
                minute INTEGER NOT NULL,                 -- local minute, 0-59
                recurrence TEXT NOT NULL DEFAULT 'once',  -- 'once' | 'daily' | 'weekly'
                weekday INTEGER,                          -- 0-6 for weekly, else NULL
                photo_file_id TEXT,                       -- for pictorial reminders (tier1+)
                active INTEGER NOT NULL DEFAULT 1,
                fired_today INTEGER NOT NULL DEFAULT 0,   -- prevents double-fire same day
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                stars INTEGER NOT NULL,
                tier TEXT NOT NULL,
                paid_at INTEGER NOT NULL
            )
        """)
        conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------- users ----------

def get_or_create_user(user_id: int) -> sqlite3.Row:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return row
        conn.execute(
            "INSERT INTO users (user_id, tier, created_at) VALUES (?, 'free', ?)",
            (user_id, int(time.time())),
        )
        conn.commit()
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def set_utc_offset(user_id: int, offset: float):
    with get_conn() as conn:
        conn.execute("UPDATE users SET utc_offset=? WHERE user_id=?", (offset, user_id))
        conn.commit()


def get_effective_tier(user: sqlite3.Row) -> str:
    """Returns the user's actual current tier, accounting for expiry."""
    if user["tier"] in ("tier1", "tier2") and user["tier_expiry"]:
        if user["tier_expiry"] < time.time():
            return "free"
    return user["tier"]


def set_tier(user_id: int, tier: str, days: int = 30):
    expiry = int(time.time()) + days * 86400
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET tier=?, tier_expiry=? WHERE user_id=?",
            (tier, expiry, user_id),
        )
        conn.commit()


def increment_streak(user_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE users SET streak = streak + 1 WHERE user_id=?", (user_id,))
        conn.commit()


def reset_streak(user_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE users SET streak = 0 WHERE user_id=?", (user_id,))
        conn.commit()


# ---------- reminders ----------

def count_active_reminders(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM reminders WHERE user_id=? AND active=1", (user_id,)
        ).fetchone()
        return row["c"]


def add_reminder(user_id, message, hour, minute, recurrence="once", weekday=None, photo_file_id=None):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO reminders
               (user_id, message, hour, minute, recurrence, weekday, photo_file_id, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (user_id, message, hour, minute, recurrence, weekday, photo_file_id, int(time.time())),
        )
        conn.commit()
        return cur.lastrowid


def list_reminders(user_id):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM reminders WHERE user_id=? AND active=1 ORDER BY hour, minute",
            (user_id,),
        ).fetchall()


def get_reminder(reminder_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()


def delete_reminder(reminder_id, user_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE reminders SET active=0 WHERE id=? AND user_id=?", (reminder_id, user_id)
        )
        conn.commit()


def all_active_reminders():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM reminders WHERE active=1").fetchall()


def mark_fired(reminder_id):
    with get_conn() as conn:
        conn.execute("UPDATE reminders SET fired_today=1 WHERE id=?", (reminder_id,))
        conn.commit()


def reset_fired_flags():
    """Call once a day (midnight sweep) so daily/weekly reminders can fire again."""
    with get_conn() as conn:
        conn.execute("UPDATE reminders SET fired_today=0")
        conn.commit()


def deactivate_once_reminder(reminder_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE reminders SET active=0 WHERE id=? AND recurrence='once'", (reminder_id,)
        )
        conn.commit()


# ---------- payments / admin stats ----------

def record_payment(user_id, stars, tier):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO payments (user_id, stars, tier, paid_at) VALUES (?,?,?,?)",
            (user_id, stars, tier, int(time.time())),
        )
        conn.commit()


def stats_since(unix_ts: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(stars),0) total, COUNT(*) count FROM payments WHERE paid_at>=?",
            (unix_ts,),
        ).fetchone()
        return row["total"], row["count"]


def total_users():
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) c FROM users").fetchone()
        return row["c"]


def users_by_tier():
    with get_conn() as conn:
        rows = conn.execute("SELECT tier, COUNT(*) c FROM users GROUP BY tier").fetchall()
        return {r["tier"]: r["c"] for r in rows}
