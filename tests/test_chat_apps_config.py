"""Tests for CHAT_APPS env parsing."""

import pytest

from yuki_conductor.config import ChatApp, chat_apps


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("CHAT_APPS", raising=False)


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
