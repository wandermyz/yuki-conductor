"""Tests for cron task → MessagingPlatform routing."""

import threading
from unittest.mock import patch

from yuki_conductor.claude_runner import ClaudeResult
from yuki_conductor.cron_config import CronTask
from yuki_conductor.cron_scheduler import _pick_platform, _run_cron_task
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
    plugin = FakePlatform("my_plugin")
    assert _pick_platform(_task(chat_app="my_plugin"), {"my_plugin": plugin}) is plugin


def test_pick_platform_explicit_missing_skips():
    slack = FakePlatform("slack")
    assert _pick_platform(_task(chat_app="other_plugin"), {"slack": slack}) is None


def test_pick_platform_slack_socket_alias():
    """`chat_app: slack_socket` (the CHAT_APPS spelling) maps to platform `slack`."""
    slack = FakePlatform("slack")
    assert _pick_platform(_task(chat_app="slack_socket"), {"slack": slack}) is slack


def test_pick_platform_default_uses_chat_apps_order():
    slack = FakePlatform("slack")
    plugin = FakePlatform("my_plugin")
    with patch("yuki_conductor.cron_scheduler.chat_apps", return_value=["slack_socket"]):
        assert _pick_platform(_task(), {"slack": slack, "my_plugin": plugin}) is slack


def test_pick_platform_default_falls_back_to_plugin():
    plugin = FakePlatform("my_plugin")
    with patch("yuki_conductor.cron_scheduler.chat_apps", return_value=["my_plugin"]):
        assert _pick_platform(_task(), {"my_plugin": plugin}) is plugin


def test_pick_platform_default_falls_back_to_web():
    web = FakePlatform("web")
    with patch("yuki_conductor.cron_scheduler.chat_apps", return_value=[]):
        assert _pick_platform(_task(), {"web": web}) is web


def test_pick_platform_no_apps_returns_none():
    with patch("yuki_conductor.cron_scheduler.chat_apps", return_value=[]):
        assert _pick_platform(_task(), {}) is None


def test_run_cron_task_notify_routes_to_explicit_app():
    plugin = FakePlatform("my_plugin")
    slack = FakePlatform("slack")
    fake_result = ClaudeResult(text="hello world <notify>", session_id="sess-1")

    with patch("yuki_conductor.cron_scheduler.run_claude", return_value=fake_result):
        _run_cron_task(
            _task(chat_app="my_plugin"),
            {"slack": slack, "my_plugin": plugin},
        )

    assert plugin.threads == [("hello world", "desc t1")]
    assert plugin.session_writes == [("my_plugin-thread-0", "sess-1", "desc t1")]
    assert slack.threads == []


def test_run_cron_task_silence_does_not_route():
    slack = FakePlatform("slack")
    fake_result = ClaudeResult(text="quiet check <silence>", session_id="sess-1")

    with (
        patch("yuki_conductor.cron_scheduler.run_claude", return_value=fake_result),
        patch("yuki_conductor.cron_scheduler.chat_apps", return_value=["slack_socket"]),
    ):
        _run_cron_task(_task(), {"slack": slack})

    assert slack.threads == []


def test_run_cron_task_explicit_missing_app_is_skipped():
    slack = FakePlatform("slack")
    fake_result = ClaudeResult(text="x <notify>", session_id="sess-1")

    with patch("yuki_conductor.cron_scheduler.run_claude", return_value=fake_result):
        _run_cron_task(_task(chat_app="other_plugin"), {"slack": slack})

    assert slack.threads == []


def test_run_cron_task_no_apps_logs_only(caplog):
    fake_result = ClaudeResult(text="reminder <notify>", session_id="sess-1")

    with (
        patch("yuki_conductor.cron_scheduler.run_claude", return_value=fake_result),
        patch("yuki_conductor.cron_scheduler.chat_apps", return_value=[]),
    ):
        with caplog.at_level("INFO", logger="yuki_conductor.cron_scheduler"):
            _run_cron_task(_task(), {})

    assert any("no chat apps enabled" in rec.message.lower() for rec in caplog.records)


class ReservingPlatform(FakePlatform):
    """A platform that can hand out a conversation id before the run starts."""

    def __init__(self, name: str = "web") -> None:
        super().__init__(name)
        self.reserved: list[str] = []
        self.discarded: list[str] = []

    def reserve_thread(self, title=None):
        key = f"{self.name}-reserved-{len(self.reserved)}"
        self.reserved.append(key)
        return key

    def discard_thread(self, conversation_key):
        self.discarded.append(conversation_key)
        return True


def test_reserved_conversation_is_passed_to_the_run_and_reused():
    platform = ReservingPlatform()
    result = ClaudeResult(text="all done <notify>", session_id="sess-1")
    with patch(
        "yuki_conductor.cron_scheduler.run_claude", return_value=result
    ) as run:
        _run_cron_task(_task(), {"web": platform})

    assert run.call_args.kwargs["web_conversation_id"] == "web-reserved-0"
    # Reused rather than opening a second thread the user has to find.
    assert platform.threads == []
    assert [key for key, _ in platform.sent] == ["web-reserved-0"]
    assert platform.sent[0][1].text == "all done"
    assert platform.discarded == []
    assert platform.session_writes == [("web-reserved-0", "sess-1", "desc t1")]


def test_silent_run_discards_the_reserved_conversation():
    platform = ReservingPlatform()
    result = ClaudeResult(text="nothing to report <silence>", session_id="sess-1")
    with patch("yuki_conductor.cron_scheduler.run_claude", return_value=result):
        _run_cron_task(_task(), {"web": platform})

    assert platform.discarded == ["web-reserved-0"]
    assert platform.sent == []


def test_platform_without_reserve_still_starts_a_thread():
    platform = FakePlatform("slack")
    result = ClaudeResult(text="ping <notify>", session_id="sess-1")
    with patch(
        "yuki_conductor.cron_scheduler.run_claude", return_value=result
    ) as run:
        _run_cron_task(_task(chat_app="slack"), {"slack": platform})

    assert run.call_args.kwargs["web_conversation_id"] is None
    assert platform.threads == [("ping", "desc t1")]


# ---- Run history recording ----


def test_successful_run_is_recorded(isolated_cron_run_store):
    platform = FakePlatform("slack")
    result = ClaudeResult(text="ping <notify>", session_id="sess-1")
    with patch("yuki_conductor.cron_scheduler.run_claude", return_value=result):
        _run_cron_task(_task(chat_app="slack"), {"slack": platform})

    run = isolated_cron_run_store.last_run("t1")
    assert run["status"] == "success"
    assert run["response"] == "ping"
    assert run["notified"] is True
    assert run["trigger"] == "schedule"
    assert run["session_id"] == "sess-1"


def test_silent_run_is_recorded_as_not_notified(isolated_cron_run_store):
    result = ClaudeResult(text="nothing <silence>", session_id="sess-1")
    with patch("yuki_conductor.cron_scheduler.run_claude", return_value=result):
        _run_cron_task(_task(), {})

    run = isolated_cron_run_store.last_run("t1")
    assert run["status"] == "success"
    assert run["notified"] is False
    assert run["response"] == "nothing"


def test_claude_error_result_is_recorded_as_error(isolated_cron_run_store):
    result = ClaudeResult(text="it broke", session_id=None, is_error=True)
    with patch("yuki_conductor.cron_scheduler.run_claude", return_value=result):
        _run_cron_task(_task(), {})

    run = isolated_cron_run_store.last_run("t1")
    assert run["status"] == "error"
    assert run["error"] == "it broke"


def test_raised_exception_is_recorded_as_error(isolated_cron_run_store):
    with patch(
        "yuki_conductor.cron_scheduler.run_claude",
        side_effect=RuntimeError("subprocess died"),
    ):
        _run_cron_task(_task(), {})

    run = isolated_cron_run_store.last_run("t1")
    assert run["status"] == "error"
    assert "subprocess died" in run["error"]
    assert run["finished_at"] is not None


def test_reserved_conversation_is_recorded_on_the_run(isolated_cron_run_store):
    platform = ReservingPlatform()
    result = ClaudeResult(text="done <notify>", session_id="sess-1")
    with patch("yuki_conductor.cron_scheduler.run_claude", return_value=result):
        _run_cron_task(_task(), {"web": platform})

    assert isolated_cron_run_store.last_run("t1")["conversation_id"] == "web-reserved-0"


# ---- Manual triggering ----


def test_trigger_task_runs_a_defined_task(isolated_cron_run_store):
    from yuki_conductor import cron_scheduler

    result = ClaudeResult(text="manual go <notify>", session_id="sess-1")
    with (
        patch.object(cron_scheduler, "_load_cron_tasks", return_value=[_task()]),
        patch.object(cron_scheduler, "run_claude", return_value=result),
    ):
        assert cron_scheduler.trigger_task("t1") is True
        for thread in threading.enumerate():
            if thread.name == "cron-manual-t1":
                thread.join(timeout=5)

    run = isolated_cron_run_store.last_run("t1")
    assert run["trigger"] == "manual"
    assert run["status"] == "success"


def test_trigger_task_unknown_name_returns_false():
    from yuki_conductor import cron_scheduler

    with patch.object(cron_scheduler, "_load_cron_tasks", return_value=[_task()]):
        assert cron_scheduler.trigger_task("does-not-exist") is False
