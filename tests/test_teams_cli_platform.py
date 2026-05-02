"""Tests for the Teams CLI placeholder platform."""

import pytest

from yuki_conductor.messaging.platform import OutgoingMessage
from yuki_conductor.messaging.teams_cli_platform import (
    KEY_PREFIX,
    SESSION_TYPE,
    TeamsCliPlatform,
    TeamsCliReceiver,
)


@pytest.fixture
def platform(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr("yuki_conductor.store.DB_FILE", db)
    return TeamsCliPlatform()


def test_start_thread_returns_prefixed_key(platform):
    key = platform.start_thread("hello", title="t")
    assert key.startswith(KEY_PREFIX)


def test_start_thread_persists_session_type(platform):
    from yuki_conductor.store import SessionStore

    key = platform.start_thread("hello", title="my-task")
    assert SessionStore().get_session_type(key) == SESSION_TYPE


def test_set_and_get_session_id(platform):
    key = platform.start_thread("hi")
    platform.set_session_id(key, "claude-session-99", title_hint="renamed")
    assert platform.get_session_id(key) == "claude-session-99"


def test_send_logs_only(platform, caplog):
    with caplog.at_level("INFO", logger="yuki_conductor.messaging.teams_cli_platform"):
        platform.send("teams:abc", OutgoingMessage(text="hi", attachments=[]))
    assert any("teams-cli placeholder" in r.message for r in caplog.records)


def test_receiver_starts_without_blocking(caplog):
    receiver = TeamsCliReceiver()
    with caplog.at_level("WARNING", logger="yuki_conductor.messaging.teams_cli_platform"):
        receiver.start()
    assert receiver.platform.name == "teams_cli"
    assert any("placeholder" in r.message for r in caplog.records)
