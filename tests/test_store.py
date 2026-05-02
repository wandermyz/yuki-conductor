"""Tests for SQLite-backed store module."""

import threading
from pathlib import Path

from yuki_conductor.store import VALID_MODELS, ModelStore, SessionStore


def test_session_roundtrip(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "test.db")
    store.set("ts_1", "session_abc")
    assert store.get("ts_1") == "session_abc"


def test_session_missing_key(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "test.db")
    assert store.get("nonexistent") is None


def test_session_overwrite(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "test.db")
    store.set("ts_1", "old")
    store.set("ts_1", "new")
    assert store.get("ts_1") == "new"


def test_session_concurrent_access(tmp_path: Path):
    store = SessionStore(db_path=tmp_path / "test.db")
    errors: list[Exception] = []

    def writer(i: int):
        try:
            store.set(f"ts_{i}", f"session_{i}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    for i in range(20):
        assert store.get(f"ts_{i}") == f"session_{i}"


def test_model_roundtrip(tmp_path: Path):
    store = ModelStore(db_path=tmp_path / "test.db")
    store.set("C123", "opus")
    assert store.get("C123") == "opus"


def test_model_missing_key(tmp_path: Path):
    store = ModelStore(db_path=tmp_path / "test.db")
    assert store.get("C999") is None


def test_separate_tables(tmp_path: Path):
    """SessionStore and ModelStore use the same db file but different tables."""
    db = tmp_path / "test.db"
    sessions = SessionStore(db_path=db)
    models = ModelStore(db_path=db)

    sessions.set("key1", "session_val")
    models.set("key1", "model_val")

    assert sessions.get("key1") == "session_val"
    assert models.get("key1") == "model_val"


def test_db_creation(tmp_path: Path):
    db = tmp_path / "subdir" / "test.db"
    store = SessionStore(db_path=db)
    store.set("ts_1", "s1")
    assert db.exists()


def test_valid_models():
    assert VALID_MODELS == {"sonnet", "opus", "haiku"}
