"""SQLite storage for the taskboard: tasks and their comment timelines.

Status changes go through `taskboard.transitions`, never `set_status`
directly, so every change leaves a system comment and reaches its listeners.
"""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from yuki_conductor.config import DB_FILE
from yuki_conductor.store import _SqliteKVStore

TODO = "todo"
IN_PROGRESS = "in_progress"
NEEDS_INPUT = "needs_input"
READY_FOR_REVIEW = "ready_for_review"
DONE = "done"
STATUSES = (TODO, IN_PROGRESS, NEEDS_INPUT, READY_FOR_REVIEW, DONE)

STATUS_LABELS = {
    TODO: "To Do",
    IN_PROGRESS: "In Progress",
    NEEDS_INPUT: "Needs Input",
    READY_FOR_REVIEW: "Ready for Review",
    DONE: "Done",
}

COMMENT_AUTHORS = ("user", "agent", "system", "github")

# Done tasks older than this are hidden from the default list.
ARCHIVE_AFTER_SECONDS = 30 * 24 * 3600

_TASK_COLUMNS = (
    "id", "title", "body", "attachments", "project", "harness", "use_worktree",
    "status", "sort_order", "session_id", "worktree_path", "branch", "pr_url",
    "pr_checked_at", "asked_input", "parent_task_id", "cleanup_error",
    "created_at", "updated_at", "started_at", "done_at",
)
_COMMENT_COLUMNS = (
    "id", "task_id", "author", "body", "quote_comment_id", "attachments",
    "delivered", "external_url", "external_author", "created_at",
)

# Columns `update_fields` may write. Status, ordering and timestamps have
# dedicated methods so their invariants live in one place.
_UPDATABLE = frozenset({
    "title", "body", "attachments", "project", "harness", "use_worktree",
    "session_id", "worktree_path", "branch", "pr_url", "pr_checked_at",
    "asked_input", "cleanup_error",
})


@dataclass
class Task:
    id: int
    title: str
    body: str
    attachments: list[str]
    project: str
    harness: str
    use_worktree: bool
    status: str
    sort_order: float
    session_id: str | None
    worktree_path: str | None
    branch: str | None
    pr_url: str | None
    pr_checked_at: float | None
    asked_input: bool
    parent_task_id: int | None
    cleanup_error: str | None
    created_at: float
    updated_at: float
    started_at: float | None
    done_at: float | None

    @property
    def editable(self) -> bool:
        """Only a task nobody has picked up yet may be edited."""
        return self.status == TODO and self.started_at is None


@dataclass
class TaskComment:
    id: int
    task_id: int
    author: str
    body: str
    quote_comment_id: int | None
    attachments: list[str] = field(default_factory=list)
    delivered: bool = False
    external_url: str | None = None
    external_author: str | None = None
    created_at: float = 0.0


def _task(row: tuple) -> Task:
    d = dict(zip(_TASK_COLUMNS, row, strict=True))
    d["attachments"] = json.loads(d["attachments"])
    d["use_worktree"] = bool(d["use_worktree"])
    d["asked_input"] = bool(d["asked_input"])
    return Task(**d)


def _comment(row: tuple) -> TaskComment:
    d = dict(zip(_COMMENT_COLUMNS, row, strict=True))
    d["attachments"] = json.loads(d["attachments"])
    d["delivered"] = bool(d["delivered"])
    return TaskComment(**d)


class TaskStore:
    """Tasks and comments in the workspace DB.

    `sort_order` is REAL so a task can be put before the first or after the
    last without renumbering anything else.
    """

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
                    "CREATE TABLE IF NOT EXISTS tasks ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "title TEXT NOT NULL, "
                    "body TEXT NOT NULL DEFAULT '', "
                    "attachments TEXT NOT NULL DEFAULT '[]', "
                    "project TEXT NOT NULL, "
                    "harness TEXT NOT NULL, "
                    "use_worktree INTEGER NOT NULL, "
                    "status TEXT NOT NULL, "
                    "sort_order REAL NOT NULL, "
                    "session_id TEXT, "
                    "worktree_path TEXT, "
                    "branch TEXT, "
                    "pr_url TEXT, "
                    "pr_checked_at REAL, "
                    "asked_input INTEGER NOT NULL DEFAULT 0, "
                    "parent_task_id INTEGER REFERENCES tasks(id), "
                    "cleanup_error TEXT, "
                    "created_at REAL NOT NULL, "
                    "updated_at REAL NOT NULL, "
                    "started_at REAL, "
                    "done_at REAL)"
                )
                con.execute(
                    "CREATE TABLE IF NOT EXISTS task_comments ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "task_id INTEGER NOT NULL REFERENCES tasks(id), "
                    "author TEXT NOT NULL, "
                    "body TEXT NOT NULL, "
                    "quote_comment_id INTEGER, "
                    "attachments TEXT NOT NULL DEFAULT '[]', "
                    "delivered INTEGER NOT NULL DEFAULT 0, "
                    "external_url TEXT, "
                    "external_author TEXT, "
                    "created_at REAL NOT NULL)"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_comments_task "
                    "ON task_comments (task_id, id)"
                )
                con.commit()
            finally:
                con.close()

    # -- tasks -------------------------------------------------------------

    def create(
        self,
        title: str,
        project: str,
        harness: str,
        use_worktree: bool,
        body: str = "",
        attachments: list[str] | None = None,
        parent_task_id: int | None = None,
    ) -> Task:
        """Add a task at the end of the To Do queue."""
        now = time.time()
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    "INSERT INTO tasks (title, body, attachments, project, harness, "
                    "use_worktree, status, sort_order, parent_task_id, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, "
                    "(SELECT COALESCE(MAX(sort_order), 0) + 1 FROM tasks), ?, ?, ?)",
                    (
                        title, body, json.dumps(attachments or []), project, harness,
                        int(use_worktree), TODO, parent_task_id, now, now,
                    ),
                )
                con.commit()
                task_id = cur.lastrowid
            finally:
                con.close()
        return self.get(task_id)

    def get(self, task_id: int) -> Task | None:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
            finally:
                con.close()
        return _task(row) if row else None

    def list_tasks(self, include_archived: bool = False) -> list[Task]:
        """Every task in queue order. Done tasks past the archive cutoff are
        left out unless `include_archived`."""
        sql = f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks"
        params: tuple = ()
        if not include_archived:
            sql += " WHERE NOT (status = ? AND done_at < ?)"
            params = (DONE, time.time() - ARCHIVE_AFTER_SECONDS)
        sql += " ORDER BY sort_order, id"
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(sql, params).fetchall()
            finally:
                con.close()
        return [_task(r) for r in rows]

    def list_status(self, status: str) -> list[Task]:
        """Tasks in one status, in queue order."""
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks WHERE status = ? "
                    "ORDER BY sort_order, id",
                    (status,),
                ).fetchall()
            finally:
                con.close()
        return [_task(r) for r in rows]

    def children(self, task_id: int) -> list[Task]:
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks "
                    "WHERE parent_task_id = ? ORDER BY id",
                    (task_id,),
                ).fetchall()
            finally:
                con.close()
        return [_task(r) for r in rows]

    def update_fields(self, task_id: int, **fields) -> Task | None:
        """Write any of the `_UPDATABLE` columns and bump `updated_at`."""
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"Not updatable: {', '.join(sorted(unknown))}")
        if "attachments" in fields:
            fields["attachments"] = json.dumps(fields["attachments"])
        for flag in ("use_worktree", "asked_input"):
            if flag in fields:
                fields[flag] = int(fields[flag])
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    f"UPDATE tasks SET {assignments} WHERE id = ?",
                    (*fields.values(), task_id),
                )
                con.commit()
            finally:
                con.close()
        return self.get(task_id)

    def edit_if_untouched(self, task_id: int, **fields) -> bool:
        """Like `update_fields`, but only while the task is To Do and has never
        been picked up, checked in the same statement so a pickup can't slip
        in between. Returns whether the edit happened."""
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"Not updatable: {', '.join(sorted(unknown))}")
        if "attachments" in fields:
            fields["attachments"] = json.dumps(fields["attachments"])
        if "use_worktree" in fields:
            fields["use_worktree"] = int(fields["use_worktree"])
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    f"UPDATE tasks SET {assignments} WHERE id = ? AND status = ? "
                    "AND started_at IS NULL",
                    (*fields.values(), task_id, TODO),
                )
                con.commit()
                return cur.rowcount == 1
            finally:
                con.close()

    def claim(self, task_id: int) -> bool:
        """Move a To Do task to In Progress, atomically.

        Returns False if the task is no longer To Do (cancelled or claimed in
        the meantime), so pickup can't race a cancel.
        """
        now = time.time()
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    "UPDATE tasks SET status = ?, started_at = COALESCE(started_at, ?), "
                    "updated_at = ? WHERE id = ? AND status = ?",
                    (IN_PROGRESS, now, now, task_id, TODO),
                )
                con.commit()
                return cur.rowcount == 1
            finally:
                con.close()

    def set_status(
        self, task_id: int, status: str, *, expected: str, position: str | None = None
    ) -> bool:
        """Low-level compare-and-set. Only `taskboard.transitions` should call it.

        Writes `status` only if the task is still in `expected`, and returns
        whether it did, so a cancel can't overwrite a pickup (or a turn's end)
        that landed in between. `position` is "top" or "end" of the queue, for
        tasks entering To Do. `done_at` is set on entering Done and cleared
        otherwise.
        """
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        now = time.time()
        sql = "UPDATE tasks SET status = ?, updated_at = ?, done_at = ?"
        if position == "top":
            sql += ", sort_order = (SELECT MIN(sort_order) - 1 FROM tasks)"
        elif position == "end":
            sql += ", sort_order = (SELECT MAX(sort_order) + 1 FROM tasks)"
        elif position is not None:
            raise ValueError(f"Unknown position {position!r}")
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    sql + " WHERE id = ? AND status = ?",
                    (status, now, now if status == DONE else None, task_id, expected),
                )
                con.commit()
                return cur.rowcount == 1
            finally:
                con.close()

    def move_to_top(self, task_id: int) -> None:
        self._reposition(task_id, "MIN(sort_order) - 1")

    def move_to_end(self, task_id: int) -> None:
        self._reposition(task_id, "MAX(sort_order) + 1")

    def _reposition(self, task_id: int, expr: str) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    f"UPDATE tasks SET sort_order = (SELECT {expr} FROM tasks) WHERE id = ?",
                    (task_id,),
                )
                con.commit()
            finally:
                con.close()

    def reorder(self, ids: list[int]) -> None:
        """Put the given To Do tasks in this order.

        They're reassigned the sort slots they already hold between them, so
        tasks not named keep their place relative to the rest of the queue.
        Raises ValueError if any id isn't a To Do task.
        """
        with self._lock:
            con = self._connect()
            try:
                placeholders = ", ".join("?" for _ in ids)
                rows = con.execute(
                    f"SELECT id, sort_order FROM tasks WHERE status = ? "
                    f"AND id IN ({placeholders})",
                    (TODO, *ids),
                ).fetchall()
                if len(rows) != len(set(ids)) or len(ids) != len(set(ids)):
                    raise ValueError("reorder accepts distinct To Do task ids only")
                slots = sorted(order for _, order in rows)
                con.executemany(
                    "UPDATE tasks SET sort_order = ? WHERE id = ?",
                    list(zip(slots, ids, strict=True)),
                )
                con.commit()
            finally:
                con.close()

    def running_counts(self) -> dict[str, int]:
        """In Progress tasks per harness."""
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT harness, COUNT(*) FROM tasks WHERE status = ? GROUP BY harness",
                    (IN_PROGRESS,),
                ).fetchall()
            finally:
                con.close()
        return dict(rows)

    # -- comments ----------------------------------------------------------

    def add_comment(
        self,
        task_id: int,
        author: str,
        body: str,
        *,
        quote_comment_id: int | None = None,
        attachments: list[str] | None = None,
        external_url: str | None = None,
        external_author: str | None = None,
    ) -> TaskComment:
        """Append to a task's timeline and bump the task's `updated_at`.

        Only user and GitHub comments start undelivered: they're what the
        agent hasn't seen yet. Its own and system comments never need sending.
        Raises KeyError for an unknown task.
        """
        if author not in COMMENT_AUTHORS:
            raise ValueError(f"Unknown comment author {author!r}")
        delivered = author not in ("user", "github")
        now = time.time()
        with self._lock:
            con = self._connect()
            try:
                if not con.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
                    raise KeyError(task_id)
                cur = con.execute(
                    "INSERT INTO task_comments (task_id, author, body, quote_comment_id, "
                    "attachments, delivered, external_url, external_author, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id, author, body, quote_comment_id,
                        json.dumps(attachments or []), int(delivered), external_url,
                        external_author, now,
                    ),
                )
                con.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (now, task_id))
                con.commit()
                comment_id = cur.lastrowid
            finally:
                con.close()
        return TaskComment(
            id=comment_id, task_id=task_id, author=author, body=body,
            quote_comment_id=quote_comment_id, attachments=list(attachments or []),
            delivered=delivered, external_url=external_url,
            external_author=external_author, created_at=now,
        )

    def comments(self, task_id: int) -> list[TaskComment]:
        return self._comments("WHERE task_id = ?", (task_id,))

    def comment_counts(self) -> dict[int, int]:
        """Non-system comments per task, for the list view."""
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT task_id, COUNT(*) FROM task_comments WHERE author != 'system' "
                    "GROUP BY task_id"
                ).fetchall()
            finally:
                con.close()
        return dict(rows)

    def undelivered(self, task_id: int) -> list[TaskComment]:
        """Comments the agent hasn't been sent yet, oldest first."""
        return self._comments("WHERE task_id = ? AND delivered = 0", (task_id,))

    def _comments(self, where: str, params: tuple) -> list[TaskComment]:
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    f"SELECT {', '.join(_COMMENT_COLUMNS)} FROM task_comments "
                    f"{where} ORDER BY id",
                    params,
                ).fetchall()
            finally:
                con.close()
        return [_comment(r) for r in rows]

    def mark_delivered(self, comment_ids: list[int], delivered: bool = True) -> None:
        if not comment_ids:
            return
        placeholders = ", ".join("?" for _ in comment_ids)
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    f"UPDATE task_comments SET delivered = ? WHERE id IN ({placeholders})",
                    (int(delivered), *comment_ids),
                )
                con.commit()
            finally:
                con.close()


class TaskboardStateStore(_SqliteKVStore):
    """Board-wide settings that must survive a restart (the pause switch)."""

    def __init__(self, db_path: Path | None = None):
        super().__init__("taskboard_state", db_path)

    def paused(self) -> bool:
        return self.get("paused") == "1"

    def set_paused(self, paused: bool) -> None:
        self.set("paused", "1" if paused else "0")
