"""Tests for CHAT_APPS env parsing and SLACK_MODE legacy translation."""

import pytest

from yuki_conductor.config import ChatApp, chat_apps


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("CHAT_APPS", raising=False)
    monkeypatch.delenv("SLACK_MODE", raising=False)


def test_default_is_slack_socket():
    assert chat_apps() == {ChatApp.SLACK_SOCKET}


def test_empty_disables_all(monkeypatch):
    monkeypatch.setenv("CHAT_APPS", "")
    assert chat_apps() == set()


def test_none_keyword(monkeypatch):
    monkeypatch.setenv("CHAT_APPS", "none")
    assert chat_apps() == set()


def test_single_app(monkeypatch):
    monkeypatch.setenv("CHAT_APPS", "teams_cli")
    assert chat_apps() == {ChatApp.TEAMS_CLI}


def test_multiple_apps(monkeypatch):
    monkeypatch.setenv("CHAT_APPS", "slack_socket,teams_cli")
    assert chat_apps() == {ChatApp.SLACK_SOCKET, ChatApp.TEAMS_CLI}


def test_whitespace_and_case(monkeypatch):
    monkeypatch.setenv("CHAT_APPS", "  Slack_Socket , TEAMS_CLI  ")
    assert chat_apps() == {ChatApp.SLACK_SOCKET, ChatApp.TEAMS_CLI}


def test_invalid_value(monkeypatch):
    monkeypatch.setenv("CHAT_APPS", "discord")
    with pytest.raises(RuntimeError, match="Invalid CHAT_APPS"):
        chat_apps()


def test_legacy_slack_mode_socket(monkeypatch):
    monkeypatch.setenv("SLACK_MODE", "SOCKET")
    assert chat_apps() == {ChatApp.SLACK_SOCKET}


def test_legacy_slack_mode_none(monkeypatch):
    monkeypatch.setenv("SLACK_MODE", "NONE")
    assert chat_apps() == set()


def test_legacy_slack_mode_token_rejected(monkeypatch):
    monkeypatch.setenv("SLACK_MODE", "TOKEN")
    with pytest.raises(RuntimeError, match="no longer supported"):
        chat_apps()


def test_chat_apps_overrides_slack_mode(monkeypatch):
    monkeypatch.setenv("SLACK_MODE", "SOCKET")
    monkeypatch.setenv("CHAT_APPS", "teams_cli")
    assert chat_apps() == {ChatApp.TEAMS_CLI}
