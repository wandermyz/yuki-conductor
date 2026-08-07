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
    monkeypatch.setenv("CHAT_APPS", "my_plugin")
    assert chat_apps() == ["my_plugin"]


def test_multiple_apps_preserves_order(monkeypatch):
    monkeypatch.setenv("CHAT_APPS", "slack_socket,my_plugin")
    assert chat_apps() == ["slack_socket", "my_plugin"]


def test_whitespace_and_case(monkeypatch):
    monkeypatch.setenv("CHAT_APPS", "  Slack_Socket , MY_PLUGIN  ")
    assert chat_apps() == ["slack_socket", "my_plugin"]


def test_arbitrary_plugin_names_accepted(monkeypatch):
    """No parse-time validation — unknown names are resolved at startup."""
    monkeypatch.setenv("CHAT_APPS", "discord")
    assert chat_apps() == ["discord"]
