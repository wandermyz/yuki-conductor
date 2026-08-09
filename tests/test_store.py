"""Tests for SQLite-backed store module."""

import threading
from pathlib import Path

from yuki_conductor.store import DEFAULT_MODEL_ARG, MODEL_ALIASES, ModelStore, SessionStore


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


def test_model_aliases():
    assert set(MODEL_ALIASES) == {"sonnet", "sonnet1m", "opus", "opus1m"}


def test_unreachable_models_stay_out():
    """haiku and fable 400 against a non-default endpoint; don't offer them."""
    assert "haiku" not in MODEL_ALIASES
    assert "fable" not in MODEL_ALIASES


def test_1m_aliases_resolve_to_bracketed_cli_values():
    """The whole point of the 1m aliases: bracket-free in, bracketed out."""
    assert MODEL_ALIASES["opus1m"] == "opus[1m]"
    assert MODEL_ALIASES["sonnet1m"] == "sonnet[1m]"


def test_plain_aliases_are_passthrough():
    for alias in ("sonnet", "opus"):
        assert MODEL_ALIASES[alias] == alias


def test_aliases_are_lowercase_and_bracket_free():
    """Slack lowercases the arg before lookup, so uppercase keys are unreachable."""
    for alias in MODEL_ALIASES:
        assert alias == alias.lower()
        assert "[" not in alias


def test_default_is_not_a_pinnable_model():
    """`default` means "no override", so it must not collide with a real alias."""
    assert DEFAULT_MODEL_ARG not in MODEL_ALIASES


def test_model_clear_restores_cli_default(tmp_path: Path):
    """An unset channel returns None, which run_claude turns into no --model flag."""
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
