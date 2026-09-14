"""Tests for the CHANNELS env override."""

import pytest

from yuki_conductor.config import channels, channels_override


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CHANNELS", raising=False)


def test_unset_defers_to_registry():
    """No env var means the registry decides, signalled by a None override."""
    assert channels_override() is None


def test_empty_disables_all(monkeypatch):
    monkeypatch.setenv("CHANNELS", "")
    assert channels_override() == []
    assert channels() == []


def test_none_disables_all(monkeypatch):
    monkeypatch.setenv("CHANNELS", "none")
    assert channels() == []


def test_single_plugin(monkeypatch):
    monkeypatch.setenv("CHANNELS", "my_plugin")
    assert channels() == ["my_plugin"]


def test_multiple_preserves_order(monkeypatch):
    monkeypatch.setenv("CHANNELS", "slack_socket,my_plugin")
    assert channels() == ["slack_socket", "my_plugin"]


def test_whitespace_and_case_normalized(monkeypatch):
    monkeypatch.setenv("CHANNELS", "  Slack_Socket , MY_PLUGIN  ")
    assert channels() == ["slack_socket", "my_plugin"]


def test_override_wins_over_registry(monkeypatch, isolated_plugin_registry):
    """The env var is the escape hatch, so it beats an enabled registry."""
    isolated_plugin_registry.write_text(
        "plugins:\n  - name: slack\n    builtin: true\n    enabled: true\n",
        encoding="utf-8",
    )
    assert channels() == ["slack_socket"]
    monkeypatch.setenv("CHANNELS", "none")
    assert channels() == []
