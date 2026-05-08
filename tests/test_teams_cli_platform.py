"""Tests for the Teams CLI platform."""

from unittest.mock import patch

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
    with patch("yuki_conductor.messaging.teams_cli_platform._run_teams", return_value=None):
        key = platform.start_thread("hello", title="t")
    assert key.startswith(KEY_PREFIX)


def test_start_thread_persists_session_type(platform):
    from yuki_conductor.store import SessionStore

    with patch("yuki_conductor.messaging.teams_cli_platform._run_teams", return_value=None):
        key = platform.start_thread("hello", title="my-task")
    assert SessionStore().get_session_type(key) == SESSION_TYPE


def test_set_and_get_session_id(platform):
    with patch("yuki_conductor.messaging.teams_cli_platform._run_teams", return_value=None):
        key = platform.start_thread("hi")
    platform.set_session_id(key, "claude-session-99", title_hint="renamed")
    assert platform.get_session_id(key) == "claude-session-99"


def test_send_calls_teams_cli(platform):
    with patch("yuki_conductor.messaging.teams_cli_platform._run_teams", return_value={"id": "1"}) as mock:
        platform.send("teams:12345", OutgoingMessage(text="hi", attachments=[]))
    mock.assert_called_once()
    args = mock.call_args[0]
    assert "reply" in args


def test_receiver_starts_polling(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("yuki_conductor.messaging.teams_cli_platform.TEAMS_TEAM_ID", "t")
    monkeypatch.setattr("yuki_conductor.messaging.teams_cli_platform.TEAMS_CHANNEL_ID", "c")
    monkeypatch.setattr("yuki_conductor.store.DB_FILE", tmp_path / "test.db")
    with patch("yuki_conductor.messaging.teams_cli_platform._run_teams", return_value=[]):
        receiver = TeamsCliReceiver()
        with caplog.at_level("INFO", logger="yuki_conductor.messaging.teams_cli_platform"):
            receiver.start()
    assert receiver.platform.name == "teams_cli"
    assert any("Starting Teams CLI polling" in r.message for r in caplog.records)
