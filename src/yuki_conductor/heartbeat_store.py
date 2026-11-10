"""SQLite storage for heartbeats: recurring wake-ups of a conversation or task.

One row per target in `heartbeats`, and its recent ticks in
`heartbeat_ticks`. Quiet ticks live only here, never in a conversation's
messages or a task's timeline. `heartbeat.py` owns scheduling; this module
only reads and writes rows. `store` is the instance everything uses; tests
rebind it (see `tests/conftest.py`).
"""

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from yuki_conductor.config import DB_FILE

CONVERSATION = "conversation"
TASK = "task"
TARGET_KINDS = (CONVERSATION, TASK)

# 270s keeps Anthropic's 5-minute prompt cache warm between ticks.
DEFAULT_INTERVAL_S = 270
MIN_INTERVAL_S = 60
# A hard cap: renewing means running `heartbeat set` again.
MAX_LIFETIME_S = 7 * 24 * 3600

# What a tick came to.
QUIET = "quiet"
NOTIFIED = "notified"
ERROR = "error"
OUTCOMES = (QUIET, NOTIFIED, ERROR)

MAX_TICKS_PER_TARGET = 50

_COLUMNS = (
    "target_kind", "target_id", "prompt", "interval_s", "next_due_at", "expires_at",
    "paused_at", "created_by", "created_at", "last_tick_at", "last_outcome",
    "consecutive_errors",
)
_TICK_COLUMNS = (
    "id", "target_kind", "target_id", "started_at", "finished_at", "outcome",
    "response", "error",
)


@dataclass
class Heartbeat:
    target_kind: str
    target_id: str
    prompt: str
    interval_s: int
    next_due_at: float
    expires_at: float
    paused_at: float | None
    created_by: str  # "agent" | "user"
    created_at: float
    last_tick_at: float | None
    last_outcome: str | None
    consecutive_errors: int

    @property
    def paused(self) -> bool:
        return self.paused_at is not None


@dataclass
class HeartbeatTick:
    id: int
    target_kind: str
    target_id: str
    started_at: float
    finished_at: float | None  # None: still running
    outcome: str | None
    response: str | None
    error: str | None


class HeartbeatStore:
    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or DB_FILE
        self._lock = threading.Lock()
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self._db_path))

    def _execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(sql, params)
                con.commit()
                return cur.rowcount
            finally:
                con.close()

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            con = self._connect()
            try:
                return con.execute(sql, params).fetchall()
            finally:
                con.close()

    def _init_tables(self) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS heartbeats ("
                    "target_kind TEXT NOT NULL, "
                    "target_id TEXT NOT NULL, "
                    "prompt TEXT NOT NULL, "
                    "interval_s INTEGER NOT NULL, "
                    "next_due_at REAL NOT NULL, "
                    "expires_at REAL NOT NULL, "
                    "paused_at REAL, "
                    "created_by TEXT NOT NULL, "
                    "created_at REAL NOT NULL, "
                    "last_tick_at REAL, "
                    "last_outcome TEXT, "
                    "consecutive_errors INTEGER NOT NULL DEFAULT 0, "
                    "PRIMARY KEY (target_kind, target_id))"
                )
                con.execute(
                    "CREATE TABLE IF NOT EXISTS heartbeat_ticks ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "target_kind TEXT NOT NULL, "
                    "target_id TEXT NOT NULL, "
                    "started_at REAL NOT NULL, "
                    "finished_at REAL, "
                    "outcome TEXT, "
                    "response TEXT, "
                    "error TEXT)"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_heartbeat_ticks_target "
                    "ON heartbeat_ticks (target_kind, target_id, id)"
                )
                con.commit()
            finally:
                con.close()

    # -- heartbeats ----------------------------------------------------------

    def set(
        self,
        kind: str,
        target_id: str,
        *,
        prompt: str,
        interval_s: int,
        lifetime_s: int,
        created_by: str,
    ) -> Heartbeat:
        """Create or replace a target's heartbeat. The clock restarts: the
        first tick is one interval away and it expires `lifetime_s` from now."""
        now = time.time()
        self._execute(
            "INSERT OR REPLACE INTO heartbeats (target_kind, target_id, prompt, interval_s, "
            "next_due_at, expires_at, paused_at, created_by, created_at, last_tick_at, "
            "last_outcome, consecutive_errors) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, NULL, 0)",
            (kind, str(target_id), prompt, interval_s, now + interval_s, now + lifetime_s,
             created_by, now),
        )
        return self.get(kind, target_id)

    def get(self, kind: str, target_id: str) -> Heartbeat | None:
        rows = self._query(
            f"SELECT {', '.join(_COLUMNS)} FROM heartbeats "
            "WHERE target_kind = ? AND target_id = ?",
            (kind, str(target_id)),
        )
        return Heartbeat(*rows[0]) if rows else None

    def list_all(self, kind: str | None = None) -> list[Heartbeat]:
        sql = f"SELECT {', '.join(_COLUMNS)} FROM heartbeats"
        params: tuple = ()
        if kind is not None:
            sql += " WHERE target_kind = ?"
            params = (kind,)
        return [Heartbeat(*r) for r in self._query(sql + " ORDER BY next_due_at", params)]

    def due(self, kind: str, now: float | None = None) -> list[Heartbeat]:
        """Unpaused, unexpired heartbeats whose next tick is due, oldest first."""
        now = time.time() if now is None else now
        rows = self._query(
            f"SELECT {', '.join(_COLUMNS)} FROM heartbeats WHERE target_kind = ? "
            "AND paused_at IS NULL AND next_due_at <= ? AND expires_at > ? "
            "ORDER BY next_due_at",
            (kind, now, now),
        )
        return [Heartbeat(*r) for r in rows]

    def expired(self, now: float | None = None) -> list[Heartbeat]:
        now = time.time() if now is None else now
        rows = self._query(
            f"SELECT {', '.join(_COLUMNS)} FROM heartbeats WHERE expires_at <= ?", (now,)
        )
        return [Heartbeat(*r) for r in rows]

    def delete(self, kind: str, target_id: str) -> bool:
        return self._execute(
            "DELETE FROM heartbeats WHERE target_kind = ? AND target_id = ?",
            (kind, str(target_id)),
        ) > 0

    def update(self, kind: str, target_id: str, **fields) -> Heartbeat | None:
        """Change `prompt` and/or `interval_s`; a new interval reschedules the
        next tick to one interval from now."""
        allowed = {"prompt", "interval_s"}
        if set(fields) - allowed:
            raise ValueError(f"Not updatable: {', '.join(sorted(set(fields) - allowed))}")
        if "interval_s" in fields:
            fields["next_due_at"] = time.time() + fields["interval_s"]
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            self._execute(
                f"UPDATE heartbeats SET {assignments} WHERE target_kind = ? AND target_id = ?",
                (*fields.values(), kind, str(target_id)),
            )
        return self.get(kind, target_id)

    def set_paused(self, kind: str, target_id: str, paused: bool) -> Heartbeat | None:
        """Pause, or resume with the next tick one interval away and the
        error count reset."""
        now = time.time()
        if paused:
            self._execute(
                "UPDATE heartbeats SET paused_at = COALESCE(paused_at, ?) "
                "WHERE target_kind = ? AND target_id = ?",
                (now, kind, str(target_id)),
            )
        else:
            self._execute(
                "UPDATE heartbeats SET paused_at = NULL, consecutive_errors = 0, "
                "next_due_at = ? + interval_s WHERE target_kind = ? AND target_id = ? "
                "AND paused_at IS NOT NULL",
                (now, kind, str(target_id)),
            )
        return self.get(kind, target_id)

    def postpone(self, kind: str, target_id: str, now: float | None = None) -> None:
        """Count a turn that just ended as a check: the next tick is one
        interval after it, never earlier than already planned."""
        now = time.time() if now is None else now
        self._execute(
            "UPDATE heartbeats SET next_due_at = MAX(next_due_at, ? + interval_s) "
            "WHERE target_kind = ? AND target_id = ?",
            (now, kind, str(target_id)),
        )

    def spread_overdue(self, jitter, now: float | None = None) -> int:
        """Reschedule overdue heartbeats to `now + jitter()` each (startup),
        so a restart doesn't wake every target at once. Returns how many."""
        now = time.time() if now is None else now
        overdue = self._query(
            "SELECT target_kind, target_id FROM heartbeats WHERE next_due_at < ?", (now,)
        )
        for kind, target_id in overdue:
            self._execute(
                "UPDATE heartbeats SET next_due_at = ? WHERE target_kind = ? AND target_id = ?",
                (now + jitter(), kind, target_id),
            )
        return len(overdue)

    def record_result(
        self, hb: Heartbeat, outcome: str, *, pause_after_errors: int, now: float | None = None
    ) -> Heartbeat | None:
        """Write a finished tick's outcome onto the heartbeat it ran for.

        Only if it's still the same heartbeat: one stopped or replaced
        (`heartbeat set` again) during the tick is left alone. The next tick
        is one interval after this one ended. Errors in a row past
        `pause_after_errors` pause it. Returns the updated row, or None if it
        was left alone.
        """
        now = time.time() if now is None else now
        errors = "consecutive_errors + 1" if outcome == ERROR else "0"
        n = self._execute(
            f"UPDATE heartbeats SET last_tick_at = ?, last_outcome = ?, "
            f"consecutive_errors = {errors}, next_due_at = ? + interval_s "
            "WHERE target_kind = ? AND target_id = ? AND created_at = ?",
            (now, outcome, now, hb.target_kind, hb.target_id, hb.created_at),
        )
        if not n:
            return None
        self._execute(
            "UPDATE heartbeats SET paused_at = ? WHERE target_kind = ? AND target_id = ? "
            "AND consecutive_errors >= ? AND paused_at IS NULL",
            (now, hb.target_kind, hb.target_id, pause_after_errors),
        )
        return self.get(hb.target_kind, hb.target_id)

    # -- ticks ---------------------------------------------------------------

    def start_tick(self, kind: str, target_id: str) -> int:
        """Record a tick that has just begun, pruning old ones. Returns its id."""
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    "INSERT INTO heartbeat_ticks (target_kind, target_id, started_at) "
                    "VALUES (?, ?, ?)",
                    (kind, str(target_id), time.time()),
                )
                con.execute(
                    "DELETE FROM heartbeat_ticks WHERE target_kind = ? AND target_id = ? "
                    "AND id NOT IN (SELECT id FROM heartbeat_ticks WHERE target_kind = ? "
                    "AND target_id = ? ORDER BY id DESC LIMIT ?)",
                    (kind, str(target_id), kind, str(target_id), MAX_TICKS_PER_TARGET),
                )
                con.commit()
                return cur.lastrowid
            finally:
                con.close()

    def finish_tick(
        self, tick_id: int, outcome: str, response: str | None = None, error: str | None = None
    ) -> None:
        self._execute(
            "UPDATE heartbeat_ticks SET finished_at = ?, outcome = ?, response = ?, error = ? "
            "WHERE id = ?",
            (time.time(), outcome, response, error, tick_id),
        )

    def ticks(self, kind: str, target_id: str, limit: int = MAX_TICKS_PER_TARGET) -> list[HeartbeatTick]:
        """Most recent ticks, newest first."""
        rows = self._query(
            f"SELECT {', '.join(_TICK_COLUMNS)} FROM heartbeat_ticks "
            "WHERE target_kind = ? AND target_id = ? ORDER BY id DESC LIMIT ?",
            (kind, str(target_id), limit),
        )
        return [HeartbeatTick(*r) for r in rows]

    def delete_ticks(self, kind: str, target_id: str) -> None:
        self._execute(
            "DELETE FROM heartbeat_ticks WHERE target_kind = ? AND target_id = ?",
            (kind, str(target_id)),
        )


store = HeartbeatStore()
