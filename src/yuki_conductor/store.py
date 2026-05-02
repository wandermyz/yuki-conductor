"""Thread-safe SQLite stores for sessions and model preferences."""

import sqlite3
import threading
from pathlib import Path

from yuki_conductor.config import DB_FILE


class _SqliteKVStore:
    """Generic key-value store backed by a SQLite table."""

    def __init__(self, table: str, db_path: Path | None = None):
        self._table = table
        self._db_path = db_path or DB_FILE
        self._lock = threading.Lock()
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self._db_path))

    def _init_table(self) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    f"CREATE TABLE IF NOT EXISTS {self._table} "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                con.commit()
            finally:
                con.close()

    def get(self, key: str) -> str | None:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    f"SELECT value FROM {self._table} WHERE key = ?", (key,)
                ).fetchone()
                return row[0] if row else None
            finally:
                con.close()

    def set(self, key: str, value: str) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    f"INSERT OR REPLACE INTO {self._table} (key, value) VALUES (?, ?)",
                    (key, value),
                )
                con.commit()
            finally:
                con.close()


class SessionStore:
    """Maps Slack thread_ts -> (session_id, channel_id, title).

    Extended from the original KV store to include channel_id for
    constructing Slack thread URLs and title for display.
    Auto-migrates the old schema.
    """

    MAX_TITLE_LEN = 100

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or DB_FILE
        self._lock = threading.Lock()
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self._db_path))

    def _init_table(self) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS sessions "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                # Auto-migrate: add columns if missing
                cols = {
                    row[1]
                    for row in con.execute("PRAGMA table_info(sessions)").fetchall()
                }
                if "channel_id" not in cols:
                    con.execute(
                        "ALTER TABLE sessions ADD COLUMN channel_id TEXT"
                    )
                if "title" not in cols:
                    con.execute(
                        "ALTER TABLE sessions ADD COLUMN title TEXT"
                    )
                if "session_type" not in cols:
                    con.execute(
                        "ALTER TABLE sessions ADD COLUMN session_type TEXT DEFAULT 'slack'"
                    )
                if "project" not in cols:
                    con.execute(
                        "ALTER TABLE sessions ADD COLUMN project TEXT"
                    )
                con.commit()
            finally:
                con.close()

    def get(self, key: str) -> str | None:
        """Return session_id for a thread_ts, or None."""
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    "SELECT value FROM sessions WHERE key = ?", (key,)
                ).fetchone()
                return row[0] if row else None
            finally:
                con.close()

    def get_channel(self, key: str) -> str | None:
        """Return the channel_id stored for a session, or None if unknown."""
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    "SELECT channel_id FROM sessions WHERE key = ?", (key,)
                ).fetchone()
                return row[0] if row and row[0] else None
            finally:
                con.close()

    _SENTINEL = object()

    def set(self, key: str, value: str, channel_id: str | None = None,
            title: str | None = _SENTINEL, session_type: str = "slack",
            project: str | None = _SENTINEL) -> None:
        """Store session_id (and optionally channel_id/title) for a thread_ts.

        When updating an existing row, title and project are preserved if not
        explicitly passed (avoids wiping them on thread-reply updates).
        """
        with self._lock:
            con = self._connect()
            try:
                existing = con.execute(
                    "SELECT title, session_type, project FROM sessions WHERE key = ?",
                    (key,),
                ).fetchone()
                if title is self._SENTINEL:
                    title = existing[0] if existing else None
                if title and len(title) > self.MAX_TITLE_LEN:
                    title = title[:self.MAX_TITLE_LEN - 1] + "\u2026"
                if project is self._SENTINEL:
                    project = existing[2] if existing else None
                con.execute(
                    "INSERT OR REPLACE INTO sessions "
                    "(key, value, channel_id, title, session_type, project) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (key, value, channel_id, title, session_type, project),
                )
                con.commit()
            finally:
                con.close()

    def set_title(self, key: str, title: str) -> bool:
        """Update just the title for an existing session. Returns True if found."""
        if len(title) > self.MAX_TITLE_LEN:
            title = title[:self.MAX_TITLE_LEN - 1] + "\u2026"
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    "UPDATE sessions SET title = ? WHERE key = ?",
                    (title, key),
                )
                con.commit()
                return cur.rowcount > 0
            finally:
                con.close()

    def list_all(self) -> list[dict]:
        """Return all sessions as dicts."""
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT key, value, channel_id, title, session_type, project "
                    "FROM sessions ORDER BY key DESC"
                ).fetchall()
                return [
                    {
                        "thread_ts": row[0],
                        "session_id": row[1],
                        "channel_id": row[2],
                        "title": row[3],
                        "session_type": row[4] or "slack",
                        "project": row[5],
                    }
                    for row in rows
                ]
            finally:
                con.close()

    def create_zellij_session(self, zellij_session_name: str, title: str,
                              project: str) -> str:
        """Create a Zellij session entry. Returns the key."""
        key = f"zellij:{zellij_session_name}"
        self.set(key, zellij_session_name, session_type="zellij",
                 project=project, title=title)
        return key

    def delete_session(self, key: str) -> bool:
        """Delete a session by key. Returns True if found."""
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute("DELETE FROM sessions WHERE key = ?", (key,))
                con.commit()
                return cur.rowcount > 0
            finally:
                con.close()

    def stats(self) -> dict:
        """Return usage statistics from the sessions table."""
        import time

        now = time.time()
        day7 = now - 7 * 86400
        day30 = now - 30 * 86400

        with self._lock:
            con = self._connect()
            try:
                total = con.execute(
                    "SELECT COUNT(*) FROM sessions"
                ).fetchone()[0]
                # thread_ts is a Slack timestamp (epoch.seq), so we can compare directly
                last_7 = con.execute(
                    "SELECT COUNT(*) FROM sessions WHERE CAST(key AS REAL) >= ?",
                    (day7,),
                ).fetchone()[0]
                last_30 = con.execute(
                    "SELECT COUNT(*) FROM sessions WHERE CAST(key AS REAL) >= ?",
                    (day30,),
                ).fetchone()[0]
                # Per-channel breakdown (last 30 days)
                per_channel = con.execute(
                    "SELECT channel_id, COUNT(*) FROM sessions "
                    "WHERE CAST(key AS REAL) >= ? AND channel_id IS NOT NULL "
                    "GROUP BY channel_id ORDER BY COUNT(*) DESC",
                    (day30,),
                ).fetchall()
                return {
                    "total": total,
                    "last_7_days": last_7,
                    "last_30_days": last_30,
                    "per_channel_30d": per_channel,
                }
            finally:
                con.close()


class ModelStore(_SqliteKVStore):
    """Maps Slack channel_id -> model alias."""

    def __init__(self, db_path: Path | None = None):
        super().__init__("models", db_path)

    def list_all(self) -> list[tuple[str, str]]:
        """Return all (channel_id, model) pairs."""
        with self._lock:
            con = self._connect()
            try:
                return con.execute(
                    f"SELECT key, value FROM {self._table}"
                ).fetchall()
            finally:
                con.close()


VALID_MODELS = {"sonnet", "opus", "haiku"}
