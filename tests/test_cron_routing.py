"""Tests for cron task → MessagingPlatform routing."""

from unittest.mock import patch

from yuki_conductor.claude_runner import ClaudeResult
from yuki_conductor.cron_scheduler import CronTask, _pick_platform, _run_cron_task
from yuki_conductor.messaging.platform import OutgoingMessage


class FakePlatform:
    def __init__(self, name: str) -> None:
        self.name = name
        self.threads: list[tuple[str, str | None]] = []
        self.session_writes: list[tuple[str, str, str | None]] = []
        self.sent: list[tuple[str, OutgoingMessage]] = []

    def send(self, conversation_key, msg):
        self.sent.append((conversation_key, msg))

    def set_processing(self, conversation_key, message_id, on):
        pass

    def get_session_id(self, conversation_key):
        return None

    def set_session_id(self, conversation_key, session_id, title_hint=None):
        self.session_writes.append((conversation_key, session_id, title_hint))

    def start_thread(self, text, title=None):
        key = f"{self.name}-thread-{len(self.threads)}"
        self.threads.append((text, title))
        return key


def _task(name="t1", chat_app=None):
    return CronTask(
        name=name,
        schedule="* * * * *",
        description=f"desc {name}",
        prompt="do the thing",
        chat_app=chat_app,
    )


def test_pick_platform_explicit_match():
    teams = FakePlatform("teams_cli")
    assert _pick_platform(_task(chat_app="teams_cli"), {"teams_cli": teams}) is teams


def test_pick_platform_explicit_missing_skips():
    slack = FakePlatform("slack")
    # Explicit chat_app="teams_cli" but only slack is enabled → skip (None)
    assert _pick_platform(_task(chat_app="teams_cli"), {"slack": slack}) is None


def test_pick_platform_slack_socket_alias():
    """`chat_app: slack_socket` (the CHAT_APPS spelling) maps to platform `slack`."""
    slack = FakePlatform("slack")
    assert _pick_platform(_task(chat_app="slack_socket"), {"slack": slack}) is slack


def test_pick_platform_default_prefers_slack():
    slack = FakePlatform("slack")
    teams = FakePlatform("teams_cli")
    assert _pick_platform(_task(), {"slack": slack, "teams_cli": teams}) is slack


def test_pick_platform_default_falls_back_to_teams():
    teams = FakePlatform("teams_cli")
    assert _pick_platform(_task(), {"teams_cli": teams}) is teams


def test_pick_platform_no_apps_returns_none():
    assert _pick_platform(_task(), {}) is None


def test_run_cron_task_notify_routes_to_explicit_app():
    teams = FakePlatform("teams_cli")
    slack = FakePlatform("slack")
    fake_result = ClaudeResult(text="hello world <notify>", session_id="sess-1")

    with patch("yuki_conductor.cron_scheduler.run_claude", return_value=fake_result):
        _run_cron_task(
            _task(chat_app="teams_cli"),
            {"slack": slack, "teams_cli": teams},
        )

    assert teams.threads == [("hello world", "desc t1")]
    assert teams.session_writes == [("teams_cli-thread-0", "sess-1", "desc t1")]
    assert slack.threads == []


def test_run_cron_task_silence_does_not_route():
    slack = FakePlatform("slack")
    fake_result = ClaudeResult(text="quiet check <silence>", session_id="sess-1")

    with patch("yuki_conductor.cron_scheduler.run_claude", return_value=fake_result):
        _run_cron_task(_task(), {"slack": slack})

    assert slack.threads == []


def test_run_cron_task_explicit_missing_app_is_skipped():
    slack = FakePlatform("slack")
    fake_result = ClaudeResult(text="x <notify>", session_id="sess-1")

    with patch("yuki_conductor.cron_scheduler.run_claude", return_value=fake_result):
        _run_cron_task(_task(chat_app="teams_cli"), {"slack": slack})

    assert slack.threads == []


def test_run_cron_task_no_apps_logs_only(caplog):
    fake_result = ClaudeResult(text="reminder <notify>", session_id="sess-1")

    with patch("yuki_conductor.cron_scheduler.run_claude", return_value=fake_result):
        with caplog.at_level("INFO", logger="yuki_conductor.cron_scheduler"):
            _run_cron_task(_task(), {})

    assert any("no chat apps enabled" in rec.message.lower() for rec in caplog.records)
