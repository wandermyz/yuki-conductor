"""`yuki-conductor db backup|list|restore`: the workspace DB's snapshots.

`backup` and `list` work on the files directly, daemon or not. `restore`
refuses while the daemon answers, since replacing the DB under a running
daemon would lose or corrupt its writes.
"""

import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from yuki_conductor import db_backup


def register(sub) -> None:
    db = sub.add_parser("db", help="Back up and restore the workspace database")
    dsub = db.add_subparsers(dest="db_command")
    dsub.add_parser("backup", help="Take a snapshot now (then prune old ones)")
    dsub.add_parser("list", help="List snapshots, newest first")
    restore = dsub.add_parser(
        "restore", help="Replace the database with a snapshot (daemon must be stopped)"
    )
    restore.add_argument("snapshot", help="Snapshot file name (in the backup folder) or path")


def _daemon_running() -> bool:
    from yuki_conductor.web_server import WEB_PORT

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{WEB_PORT}/api/daemon", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _resolve(name: str) -> Path:
    path = Path(name).expanduser()
    if path.is_file():
        return path.resolve()
    return db_backup.backup_dir() / name


def _local(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def handle(args) -> None:
    cmd = args.db_command
    if cmd is None:
        print("usage: yuki-conductor db {backup,list,restore}", file=sys.stderr)
        sys.exit(1)
    try:
        if cmd == "backup":
            snap = db_backup.run_backup()
            print(f"Snapshot written: {snap.path}")
        elif cmd == "list":
            snapshots = db_backup.list_snapshots()
            print(f"Backup folder: {db_backup.backup_dir()}")
            if not snapshots:
                print("No snapshots yet.")
            for s in snapshots:
                print(f"{_local(s.taken_at)}  {db_backup.human_size(s.size):>9}  {s.path.name}")
        elif cmd == "restore":
            source = _resolve(args.snapshot)
            if _daemon_running():
                print(
                    "The daemon is running. Stop it first (on Windows, quit yuki-watcher), "
                    "then restore.",
                    file=sys.stderr,
                )
                sys.exit(1)
            aside = db_backup.restore(source)
            print(f"Restored {source.name} to {db_backup.db_file()}")
            print(f"The previous database was moved to {aside}")
    except db_backup.BackupError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
