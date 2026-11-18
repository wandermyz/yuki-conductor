"""Periodic snapshots of the workspace SQLite database.

The live DB must never be copied as a file: a copy taken mid-write is torn,
and a sync tool writing into it corrupts it. Instead the daemon takes a
consistent snapshot with `VACUUM INTO` (safe while other connections write),
checks it with `PRAGMA quick_check`, and renames it into place, so the backup
folder only ever holds complete, immutable files. That makes the folder safe
to point at OneDrive or any other sync/backup tool (`DB_BACKUP_DIR`).

Snapshots are named `yuki-conductor-<UTC timestamp>.db`. Retention keeps the
newest snapshot in each of the last `KEEP_HOURLY` hours, `KEEP_DAILY` days and
`KEEP_WEEKLY` ISO weeks that have one; anything else matching the name pattern
is deleted. Other files in the folder are never touched.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from yuki_conductor import config

logger = logging.getLogger(__name__)

INTERVAL_S = 3600  # take a snapshot once the newest is this old
POLL_S = 300  # how often the loop checks whether one is due
STARTUP_DELAY_S = 60  # let the daemon settle before the first check
KEEP_HOURLY = 24
KEEP_DAILY = 14
KEEP_WEEKLY = 8

_PREFIX = "yuki-conductor-"
_STAMP = "%Y%m%dT%H%M%SZ"
_NAME_RE = re.compile(r"^yuki-conductor-(\d{8}T\d{6}Z)\.db$")


@dataclass(frozen=True)
class Snapshot:
    path: Path
    taken_at: datetime  # UTC, from the file name

    @property
    def size(self) -> int:
        return self.path.stat().st_size


class BackupError(RuntimeError):
    pass


def backup_dir() -> Path:
    return config.DB_BACKUP_DIR


def db_file() -> Path:
    return config.DB_FILE


def list_snapshots(directory: Path | None = None) -> list[Snapshot]:
    """Every snapshot in the backup folder, newest first."""
    directory = directory or backup_dir()
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        m = _NAME_RE.match(path.name)
        if m and path.is_file():
            taken = datetime.strptime(m.group(1), _STAMP).replace(tzinfo=UTC)
            found.append(Snapshot(path, taken))
    found.sort(key=lambda s: s.taken_at, reverse=True)
    return found


def quick_check(path: Path) -> None:
    """Raise BackupError unless `path` is a readable, intact SQLite DB."""
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"{path.name} is not a readable SQLite database: {exc}") from exc
    if rows != [("ok",)]:
        problems = "; ".join(r[0] for r in rows[:5])
        raise BackupError(f"{path.name} failed its integrity check: {problems}")


def snapshot(now: datetime | None = None) -> Snapshot:
    """Write a consistent, checked snapshot of the DB into the backup folder."""
    source = db_file()
    if not source.is_file():
        raise BackupError(f"No database at {source}")
    now = now or datetime.now(UTC)
    directory = backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{_PREFIX}{now.strftime(_STAMP)}.db"
    # Not matching _NAME_RE, so an interrupted snapshot is never listed or
    # restored; prune() sweeps one a crash left behind.
    partial = directory / f".{final.name}.partial"
    partial.unlink(missing_ok=True)
    try:
        conn = sqlite3.connect(str(source), timeout=30)
        try:
            conn.execute("VACUUM INTO ?", (str(partial),))
        finally:
            conn.close()
        quick_check(partial)
        os.replace(partial, final)
    except sqlite3.Error as exc:
        partial.unlink(missing_ok=True)
        raise BackupError(f"Snapshot failed: {exc}") from exc
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return Snapshot(final, now)


def to_keep(snapshots: list[Snapshot]) -> set[Path]:
    """The snapshots retention keeps: newest per hour, day and ISO week."""
    keep: set[Path] = set()
    buckets = (
        (KEEP_HOURLY, lambda t: t.strftime("%Y%m%d%H")),
        (KEEP_DAILY, lambda t: t.strftime("%Y%m%d")),
        (KEEP_WEEKLY, lambda t: t.isocalendar()[:2]),
    )
    for limit, key in buckets:
        seen = []
        for snap in sorted(snapshots, key=lambda s: s.taken_at, reverse=True):
            k = key(snap.taken_at)
            if k in seen:
                continue
            if len(seen) == limit:
                break
            seen.append(k)
            keep.add(snap.path)
    return keep


def prune() -> list[Path]:
    """Delete snapshots retention no longer keeps. Returns what was deleted."""
    snapshots = list_snapshots()
    keep = to_keep(snapshots)
    deleted = []
    cutoff = time.time() - INTERVAL_S
    for stale in backup_dir().glob(f".{_PREFIX}*.partial"):
        if stale.stat().st_mtime < cutoff:  # not one being written right now
            stale.unlink(missing_ok=True)
            deleted.append(stale)
    for snap in snapshots:
        if snap.path not in keep:
            try:
                snap.path.unlink()
                deleted.append(snap.path)
            except OSError:
                logger.warning("Could not delete old DB snapshot %s", snap.path, exc_info=True)
    return deleted


def is_due(now: datetime | None = None) -> bool:
    """A snapshot is due when the newest is `INTERVAL_S` old (or there is none)
    and the DB has been written since it was taken."""
    snapshots = list_snapshots()
    if not snapshots:
        return db_file().is_file()
    now = now or datetime.now(UTC)
    newest = snapshots[0].taken_at
    if (now - newest).total_seconds() < INTERVAL_S:
        return False
    return db_file().stat().st_mtime > newest.timestamp()


# -- status, as /api/status shows it ---------------------------------------------

_lock = threading.Lock()
_last_error: str | None = None


def run_backup() -> Snapshot:
    """Snapshot, then prune. Records the outcome for `status()`."""
    global _last_error
    with _lock:
        try:
            snap = snapshot()
            deleted = prune()
        except Exception as exc:
            _last_error = str(exc)
            raise
        _last_error = None
    logger.info(
        "DB snapshot %s (%s); pruned %d old", snap.path, human_size(snap.size), len(deleted)
    )
    return snap


def status(now: datetime | None = None) -> dict:
    directory = backup_dir()
    snapshots = list_snapshots()
    if _last_error:
        return {"status": "error", "message": f"Last backup failed: {_last_error}\nFolder: {directory}"}
    if not snapshots:
        return {"status": "stopped", "message": f"No backup yet\nFolder: {directory}"}
    newest = snapshots[0]
    now = now or datetime.now(UTC)
    age = _human_age((now - newest.taken_at).total_seconds())
    return {
        "status": "ok",
        "message": (
            f"Last backup {age} ago: {newest.path.name} ({human_size(newest.size)})\n"
            f"{len(snapshots)} snapshots in {directory}"
        ),
    }


def _human_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# -- restore ---------------------------------------------------------------------


def restore(source: Path, now: datetime | None = None) -> Path:
    """Replace the live DB with `source`. The daemon must not be running.

    The current DB, and any journal/WAL beside it, are moved aside first:
    a leftover hot journal would otherwise be rolled back into the restored
    file and corrupt it. Returns where the old DB went.
    """
    if not source.is_file():
        raise BackupError(f"No such file: {source}")
    quick_check(source)
    target = db_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime(_STAMP)
    aside = target.with_name(f"{target.name}.before-restore-{stamp}")
    for suffix in ("", "-journal", "-wal", "-shm"):
        current = Path(f"{target}{suffix}")
        if current.exists():
            os.replace(current, Path(f"{aside}{suffix}"))
    shutil.copy2(source, target)
    return aside


# -- the loop --------------------------------------------------------------------


def loop(stop_event: threading.Event) -> None:
    if stop_event.wait(STARTUP_DELAY_S):
        return
    while not stop_event.is_set():
        try:
            if is_due():
                run_backup()
        except Exception:
            logger.exception("DB backup failed")
        stop_event.wait(POLL_S)


def start() -> threading.Event:
    """Start the backup loop in a daemon thread."""
    logger.info("DB backups: every %ss into %s", INTERVAL_S, backup_dir())
    stop_event = threading.Event()
    threading.Thread(target=loop, args=(stop_event,), name="db-backup", daemon=True).start()
    return stop_event

