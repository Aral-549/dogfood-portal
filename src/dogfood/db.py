"""SQLite connection and schema setup."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# Columns added after the first release. Existing volumes get them at boot (migration path).
# Tables listed here gain columns over time: always INSERT into them with named columns.
ADDED_COLUMNS = [
    ("events", "voting_open", "TEXT"),
    ("events", "voting_close", "TEXT"),
    ("events", "votes_per_voter", "INTEGER NOT NULL DEFAULT 1 CHECK (votes_per_voter >= 1)"),
    ("events", "voting_closed_sent", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "email_norm", "TEXT"),
    ("records", "revoked_at", "TEXT"),
    ("records", "revoked_reason", "TEXT"),
    ("sessions", "last_used_at", "TEXT"),  # API tokens only, updated at most every 10 minutes  # core.public.normalize_email, filled lazily (see app._registration_flags)
]


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    migrate(conn)


def migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in ADDED_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email_norm ON users(email_norm)")


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT, rolled back on any error. Keep audit rows inside it."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
