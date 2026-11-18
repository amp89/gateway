"""SQLite access: one connection per call site, WAL, numbered SQL migrations."""
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS = Path(__file__).parent / "migrations"


class Connection(sqlite3.Connection):
    """One shared connection; an RLock serializes statements and whole transactions across threads."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.lock = threading.RLock()

    def execute(self, *a, **kw):
        with self.lock:
            return super().execute(*a, **kw)


def connect(path: str) -> Connection:
    conn = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False, factory=Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def tx(conn: Connection):
    with conn.lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> list[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
    )
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for f in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        version = int(f.name[:4])
        if version in done:
            continue
        with tx(conn):
            for stmt in f.read_text().split(";\n"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, time.time()))
        applied.append(version)
    return applied


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
