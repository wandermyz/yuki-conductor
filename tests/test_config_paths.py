"""Workspace / DB locations, and the one-time move of the DB out of the workspace."""

import importlib
import sqlite3

import pytest

from yuki_conductor import config


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_daemon_listening", lambda: False)
    data, workspace = tmp_path / "data", tmp_path / "ws"
    workspace.mkdir()
    return data / "yuki-conductor.db", workspace


def make_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES ('kept')")
    con.commit()
    con.close()


def test_moves_db_and_sidecars_out_of_workspace(dirs):
    db_file, workspace = dirs
    make_db(workspace / "yuki-conductor.db")
    (workspace / "yuki-conductor.db-journal").write_bytes(b"j")

    assert config.migrate_db_file(db_file, workspace) == db_file

    assert not (workspace / "yuki-conductor.db").exists()
    assert not (workspace / "yuki-conductor.db-journal").exists()
    assert db_file.with_name("yuki-conductor.db-journal").read_bytes() == b"j"
    con = sqlite3.connect(db_file)
    assert con.execute("SELECT v FROM t").fetchall() == [("kept",)]
    con.close()


def test_existing_data_dir_db_wins(dirs):
    db_file, workspace = dirs
    db_file.parent.mkdir()
    db_file.write_bytes(b"new")
    (workspace / "yuki-conductor.db").write_bytes(b"old")

    assert config.migrate_db_file(db_file, workspace) == db_file
    assert (workspace / "yuki-conductor.db").read_bytes() == b"old"  # left alone
    assert db_file.read_bytes() == b"new"


def test_nothing_to_move(dirs):
    db_file, workspace = dirs
    assert config.migrate_db_file(db_file, workspace) == db_file
    assert not db_file.exists()


def test_waits_while_a_daemon_runs(dirs, monkeypatch):
    db_file, workspace = dirs
    (workspace / "yuki-conductor.db").write_bytes(b"old")
    monkeypatch.setattr(config, "_daemon_listening", lambda: True)

    assert config.migrate_db_file(db_file, workspace) == workspace / "yuki-conductor.db"
    assert not db_file.exists()


def test_workspace_dir_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("YUKI_CONDUCTOR_WORKSPACE_DIR", str(tmp_path / "synced"))
    try:
        importlib.reload(config)
        assert config.WORKSPACE_DIR == tmp_path / "synced"
        assert config.CRON_FILE == tmp_path / "synced" / "cron.yaml"
        assert config.ATTACHMENTS_DIR == tmp_path / "synced" / "attachments"
        assert config.DB_FILE == config.DATA_DIR / "yuki-conductor.db"
    finally:
        monkeypatch.delenv("YUKI_CONDUCTOR_WORKSPACE_DIR")
        importlib.reload(config)
