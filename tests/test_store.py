"""Tests for SQLite-backed store module."""

import threading
from pathlib import Path

from yuki_conductor.store import ModelStore, SessionStore


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


def test_model_pins_are_per_harness(tmp_path: Path):
    """A model id only means something to one harness, so each keeps its own pin."""
    models = ModelStore(db_path=tmp_path / "test.db")
    models.set(ModelStore.key("C1", "claude-code"), "sonnet")
    models.set(ModelStore.key("C1", "codex"), "gpt-6-astra")
    assert models.get(ModelStore.key("C1", "claude-code")) == "sonnet"
    assert models.get(ModelStore.key("C1", "codex")) == "gpt-6-astra"
    models.clear(ModelStore.key("C1", "codex"))
    assert models.get(ModelStore.key("C1", "claude-code")) == "sonnet"


def test_model_clear_restores_default(tmp_path: Path):
    """An unset channel returns None, which the Claude Code harness turns into CLAUDE_DEFAULT_MODEL."""
    models = ModelStore(db_path=tmp_path / "test.db")
    assert models.get("C123") is None

    models.set("C123", "opus")
    assert models.get("C123") == "opus"

    assert models.clear("C123") is True
    assert models.get("C123") is None


def test_model_clear_is_idempotent(tmp_path: Path):
    models = ModelStore(db_path=tmp_path / "test.db")
    assert models.clear("never_set") is False


def test_model_clear_leaves_other_channels_alone(tmp_path: Path):
    models = ModelStore(db_path=tmp_path / "test.db")
    models.set("C1", "opus")
    models.set("C2", "sonnet")

    models.clear("C1")

    assert models.get("C1") is None
    assert models.get("C2") == "sonnet"


def test_model_clear_does_not_touch_sessions(tmp_path: Path):
    """Both stores share a db file; clearing one table must not affect the other."""
    db = tmp_path / "test.db"
    sessions = SessionStore(db_path=db)
    models = ModelStore(db_path=db)

    sessions.set("C1", "session_val")
    models.set("C1", "opus")

    models.clear("C1")

    assert models.get("C1") is None
    assert sessions.get("C1") == "session_val"


def test_session_harness_is_fixed_by_the_first_write(tmp_path: Path):
    """A later write can't move a session to another harness."""
    store = SessionStore(db_path=tmp_path / "test.db")
    assert store.get_harness("ts1") is None
    store.set("ts1", "sess-a", harness="pi")
    store.set("ts1", "sess-b", harness="claude-code")
    assert store.get("ts1") == "sess-b"
    assert store.get_harness("ts1") == "pi"
    store.set("ts2", "sess-c")
    assert store.get_harness("ts2") == "claude-code"
