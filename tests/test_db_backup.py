"""DB snapshots: VACUUM INTO, integrity check, retention, the loop, status, CLI."""

import os
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yuki_conductor import cli_db, config, db_backup, web_server
from yuki_conductor.cli import main

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)


def make_db(path: Path, rows=("a", "b")) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()


def rows(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY rowid")]
    finally:
        conn.close()


def fake_snapshot(directory: Path, when: datetime, *, empty=False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"yuki-conductor-{when.strftime('%Y%m%dT%H%M%SZ')}.db"
    if empty:
        path.touch()
    else:
        make_db(path)
    return path


def test_backup_dir_defaults_outside_workspace():
    assert config.parse_db_backup_dir(None) == config.DATA_DIR / "backups"
    assert config.parse_db_backup_dir("  ") == config.DATA_DIR / "backups"


def test_backup_dir_is_configurable():
    assert config.parse_db_backup_dir("~/OneDrive/yuki") == Path.home() / "OneDrive" / "yuki"


def test_snapshot_copies_the_db_while_a_connection_is_open(isolated_db_backup):
    make_db(config.DB_FILE)
    held = sqlite3.connect(config.DB_FILE)  # another writer's connection
    held.execute("INSERT INTO t VALUES ('c')")  # uncommitted: not in the snapshot
    try:
        snap = db_backup.snapshot(now=NOW)
    finally:
        held.close()
    assert snap.path == isolated_db_backup / "yuki-conductor-20261117T120000Z.db"
    assert rows(snap.path) == ["a", "b"]
    assert [p.name for p in isolated_db_backup.iterdir()] == [snap.path.name]


def test_snapshot_without_a_db_fails():
    with pytest.raises(db_backup.BackupError, match="No database"):
        db_backup.snapshot(now=NOW)


def test_a_failed_snapshot_leaves_no_partial_file(monkeypatch, isolated_db_backup):
    make_db(config.DB_FILE)

    def bad_check(path):
        raise db_backup.BackupError("broken")

    monkeypatch.setattr(db_backup, "quick_check", bad_check)
    with pytest.raises(db_backup.BackupError):
        db_backup.snapshot(now=NOW)
    assert list(isolated_db_backup.iterdir()) == []


def test_quick_check_rejects_garbage(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database" * 100)
    with pytest.raises(db_backup.BackupError):
        db_backup.quick_check(junk)


def test_list_ignores_other_files(isolated_db_backup):
    newer = fake_snapshot(isolated_db_backup, NOW)
    older = fake_snapshot(isolated_db_backup, NOW - timedelta(hours=1))
    (isolated_db_backup / "notes.txt").write_text("x")
    (isolated_db_backup / f".{newer.name}.partial").write_text("x")
    assert [s.path for s in db_backup.list_snapshots()] == [newer, older]


def test_retention_keeps_newest_per_hour_day_and_week(isolated_db_backup):
    # Every 20 minutes for 60 days.
    times = [NOW - timedelta(minutes=20 * i) for i in range(3 * 24 * 60)]
    for t in times:
        fake_snapshot(isolated_db_backup, t, empty=True)

    db_backup.prune()
    kept = [s.taken_at for s in db_backup.list_snapshots()]

    hourly = [t for t in kept if NOW - t < timedelta(hours=24)]
    assert len(hourly) == 24  # newest of each of the last 24 hours
    assert kept[0] == NOW
    days = {t.date() for t in kept}
    assert len(days) >= 14
    weeks = {t.isocalendar()[:2] for t in kept}
    assert len(weeks) == 8
    assert len(kept) < 24 + 14 + 8
    # The oldest kept is the newest snapshot of the 8th week back.
    assert min(kept).isocalendar()[:2] == sorted(weeks)[0]


def test_prune_never_touches_other_files(isolated_db_backup):
    for i in range(30):
        fake_snapshot(isolated_db_backup, NOW - timedelta(hours=i), empty=True)
    other = isolated_db_backup / "mine.db"
    other.write_text("x")
    stale = isolated_db_backup / ".yuki-conductor-20261101T000000Z.db.partial"
    fresh = isolated_db_backup / ".yuki-conductor-20261117T120000Z.db.partial"
    stale.write_text("x")
    fresh.write_text("x")
    old = time.time() - 2 * db_backup.INTERVAL_S
    os.utime(stale, (old, old))
    db_backup.prune()
    assert other.exists()
    assert not stale.exists()  # left by a crashed snapshot
    assert fresh.exists()  # possibly being written right now


def test_due_when_none_old_enough_and_db_changed(isolated_db_backup):
    assert not db_backup.is_due(now=NOW)  # no DB at all
    make_db(config.DB_FILE)
    assert db_backup.is_due(now=NOW)  # never backed up

    snap = fake_snapshot(isolated_db_backup, datetime.now(UTC) - timedelta(minutes=30))
    assert not db_backup.is_due()  # too recent
    snap.unlink()

    old = datetime.now(UTC) - timedelta(hours=2)
    fake_snapshot(isolated_db_backup, old)
    assert db_backup.is_due()  # DB written since (just now)

    os.utime(config.DB_FILE, (old.timestamp() - 60, old.timestamp() - 60))
    assert not db_backup.is_due()  # unchanged since the last snapshot


def test_status_reports_last_backup_and_failures(isolated_db_backup):
    assert db_backup.status()["status"] == "stopped"

    make_db(config.DB_FILE)
    db_backup.run_backup()
    s = db_backup.status()
    assert s["status"] == "ok"
    assert "Last backup 0m ago" in s["message"]
    assert str(isolated_db_backup) in s["message"]

    config.DB_FILE.unlink()
    with pytest.raises(db_backup.BackupError):
        db_backup.run_backup()
    assert db_backup.status()["status"] == "error"
    assert "No database" in db_backup.status()["message"]


def test_api_status_includes_backup():
    client = TestClient(web_server.create_api())
    body = client.get("/api/status").json()
    assert body["db-backup"]["status"] == "stopped"


def test_loop_backs_up_when_due(monkeypatch, isolated_db_backup):
    make_db(config.DB_FILE)
    monkeypatch.setattr(db_backup, "STARTUP_DELAY_S", 0)
    stop = threading.Event()
    calls = []

    def backup():
        calls.append(1)
        stop.set()

    monkeypatch.setattr(db_backup, "run_backup", backup)
    db_backup.loop(stop)
    assert calls == [1]


def test_loop_survives_a_failure(monkeypatch):
    make_db(config.DB_FILE)
    monkeypatch.setattr(db_backup, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(db_backup, "POLL_S", 0)
    stop = threading.Event()
    calls = []

    def backup():
        calls.append(1)
        if len(calls) == 2:
            stop.set()
        raise db_backup.BackupError("disk full")

    monkeypatch.setattr(db_backup, "run_backup", backup)
    db_backup.loop(stop)
    assert len(calls) == 2


def test_restore_moves_the_old_db_and_its_journal_aside(isolated_db_backup):
    make_db(config.DB_FILE, rows=("new",))
    journal = Path(f"{config.DB_FILE}-journal")
    journal.write_bytes(b"hot journal")
    snap = fake_snapshot(isolated_db_backup, NOW)

    aside = db_backup.restore(snap, now=NOW)

    assert rows(config.DB_FILE) == ["a", "b"]
    assert not journal.exists()
    # Checked before opening `aside`: SQLite would treat the journal beside it as hot.
    assert Path(f"{aside}-journal").read_bytes() == b"hot journal"


def test_restore_rejects_a_corrupt_snapshot(tmp_path):
    make_db(config.DB_FILE, rows=("keep",))
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"junk" * 1000)
    with pytest.raises(db_backup.BackupError):
        db_backup.restore(junk)
    assert rows(config.DB_FILE) == ["keep"]


def test_cli_backup_and_list(capsys, isolated_db_backup):
    make_db(config.DB_FILE)
    main(["db", "backup"])
    assert "Snapshot written" in capsys.readouterr().out
    main(["db", "list"])
    out = capsys.readouterr().out
    assert str(isolated_db_backup) in out
    assert "yuki-conductor-" in out


def test_cli_restore_refuses_while_the_daemon_runs(monkeypatch, capsys, isolated_db_backup):
    make_db(config.DB_FILE, rows=("live",))
    snap = fake_snapshot(isolated_db_backup, NOW)
    monkeypatch.setattr(cli_db, "_daemon_running", lambda: True)
    with pytest.raises(SystemExit):
        main(["db", "restore", snap.name])
    assert "daemon is running" in capsys.readouterr().err
    assert rows(config.DB_FILE) == ["live"]

    monkeypatch.setattr(cli_db, "_daemon_running", lambda: False)
    main(["db", "restore", snap.name])
    assert "Restored" in capsys.readouterr().out
    assert rows(config.DB_FILE) == ["a", "b"]
