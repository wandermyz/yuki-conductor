"""Tests for CHAT_APPS env parsing."""

import pytest

from yuki_conductor.config import chat_apps


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("CHAT_APPS", raising=False)


def test_default_is_slack_socket():
    assert chat_apps() == ["slack_socket"]


def test_empty_disables_all(monkeypatch):
    monkeypatch.setenv("CHAT_APPS", "")
    assert chat_apps() == []


def test_none_keyword(monkeypatch):
    monkeypatch.setenv("CHAT_APPS", "none")
    assert chat_apps() == []


def test_single_app(monkeypatch):
    monkeypatch.setenv("CHAT_APPS", "teams_mcp")
    assert chat_apps() == ["teams_mcp"]


def test_multiple_apps_preserves_order(monkeypatch):
    monkeypatch.setenv("CHAT_APPS", "slack_socket,teams_mcp")
    assert chat_apps() == ["slack_socket", "teams_mcp"]


def test_whitespace_and_case(monkeypatch):
    monkeypatch.setenv("CHAT_APPS", "  Slack_Socket , TEAMS_MCP  ")
    assert chat_apps() == ["slack_socket", "teams_mcp"]


def test_arbitrary_plugin_names_accepted(monkeypatch):
    """No parse-time validation — unknown names are resolved at startup."""
    monkeypatch.setenv("CHAT_APPS", "discord")
    assert chat_apps() == ["discord"]
