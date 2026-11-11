"""Tests for cron task → MessagingPlatform routing."""

import threading
from unittest.mock import MagicMock, patch

import pytest

from yuki_conductor.cron_config import CronTask
from yuki_conductor.cron_scheduler import (
    _build_task_state,
    _pick_platform,
    _run_cron_task,
    _tasks_changed,
)
from yuki_conductor.harness.base import RunResult
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

    def get_harness(self, conversation_key):
        return None

    def set_session_id(self, conversation_key, session_id, title_hint=None, harness="claude-code"):
        self.session_writes.append((conversation_key, session_id, title_hint))

    def start_thread(self, text, title=None, harness="claude-code"):
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


def test_pick_platform_explicit_slack():
    """Channel name and platform name are the same string — no alias table."""
    slack = FakePlatform("slack")
    assert _pick_platform(_task(chat_app="slack"), {"slack": slack}) is slack


def test_pick_platform_default_uses_channel_order():
    slack = FakePlatform("slack")
    plugin = FakePlatform("my_plugin")
    with patch("yuki_conductor.cron_scheduler.channels", return_value=["slack"]):
        assert _pick_platform(_task(), {"slack": slack, "my_plugin": plugin}) is slack


def test_pick_platform_default_falls_back_to_plugin():
    plugin = FakePlatform("my_plugin")
    with patch("yuki_conductor.cron_scheduler.channels", return_value=["my_plugin"]):
        assert _pick_platform(_task(), {"my_plugin": plugin}) is plugin


def test_pick_platform_default_falls_back_to_web():
    web = FakePlatform("web")
    with patch("yuki_conductor.cron_scheduler.channels", return_value=[]):
        assert _pick_platform(_task(), {"web": web}) is web


def test_pick_platform_no_apps_returns_none():
    with patch("yuki_conductor.cron_scheduler.channels", return_value=[]):
        assert _pick_platform(_task(), {}) is None


def test_run_cron_task_notify_routes_to_explicit_app():
    plugin = FakePlatform("my_plugin")
    slack = FakePlatform("slack")
    fake_result = RunResult(text="hello world <notify>", session_id="sess-1")

    with patch("yuki_conductor.cron_scheduler.run_agent", return_value=fake_result):
        _run_cron_task(
            _task(chat_app="my_plugin"),
            {"slack": slack, "my_plugin": plugin},
        )

    assert plugin.threads == [("hello world", "desc t1")]
    assert plugin.session_writes == [("my_plugin-thread-0", "sess-1", "desc t1")]
    assert slack.threads == []


def test_run_cron_task_silence_does_not_route():
    slack = FakePlatform("slack")
    fake_result = RunResult(text="quiet check <silence>", session_id="sess-1")

    with (
        patch("yuki_conductor.cron_scheduler.run_agent", return_value=fake_result),
        patch("yuki_conductor.cron_scheduler.channels", return_value=["slack"]),
    ):
        _run_cron_task(_task(), {"slack": slack})

    assert slack.threads == []


@pytest.mark.parametrize("trigger", ["schedule", "manual"])
@pytest.mark.parametrize("field", ["wait_for_mcp", "skip_permissions"])
@pytest.mark.parametrize("enabled", [False, True])
def test_cron_launch_options_reach_agent(trigger, field, enabled):
    task = _task()
    setattr(task, field, enabled)
    result = RunResult(text="quiet <silence>", session_id="sess-1")
    with patch("yuki_conductor.cron_scheduler.run_agent", return_value=result) as run:
        _run_cron_task(task, {}, trigger=trigger)
    assert run.call_args.kwargs[field] is enabled


@pytest.mark.parametrize("field", ["wait_for_mcp", "skip_permissions"])
def test_cron_launch_option_changes_reload_the_schedule(field):
    before, after = _task(), _task()
    assert not _tasks_changed([before], [after])
    setattr(after, field, True)
    assert _tasks_changed([before], [after])
    assert _tasks_changed([after], [before])


def test_run_cron_task_explicit_missing_app_is_skipped():
    slack = FakePlatform("slack")
    fake_result = RunResult(text="x <notify>", session_id="sess-1")

    with patch("yuki_conductor.cron_scheduler.run_agent", return_value=fake_result):
        _run_cron_task(_task(chat_app="other_plugin"), {"slack": slack})

    assert slack.threads == []


def test_run_cron_task_no_apps_logs_only(caplog):
    fake_result = RunResult(text="reminder <notify>", session_id="sess-1")

    with (
        patch("yuki_conductor.cron_scheduler.run_agent", return_value=fake_result),
        patch("yuki_conductor.cron_scheduler.channels", return_value=[]),
    ):
        with caplog.at_level("INFO", logger="yuki_conductor.cron_scheduler"):
            _run_cron_task(_task(), {})

    assert any("no channels enabled" in rec.message.lower() for rec in caplog.records)


class ReservingPlatform(FakePlatform):
    """A platform that can hand out a conversation id before the run starts."""

    def __init__(self, name: str = "web") -> None:
        super().__init__(name)
        self.reserved: list[str] = []
        self.discarded: list[str] = []
        self.model_writes = []

    def set_model(self, key, model, harness):
        self.model_writes.append((key, model, harness))

    def reserve_thread(self, title=None, harness="claude-code"):
        key = f"{self.name}-reserved-{len(self.reserved)}"
        self.reserved.append(key)
        return key

    def discard_thread(self, conversation_key):
        self.discarded.append(conversation_key)
        return True


def test_reserved_conversation_is_passed_to_the_run_and_reused():
    platform = ReservingPlatform()
    result = RunResult(text="all done <notify>", session_id="sess-1")
    with patch(
        "yuki_conductor.cron_scheduler.run_agent", return_value=result
    ) as run:
        _run_cron_task(_task(), {"web": platform})

    assert run.call_args.kwargs["push_conversation_id"] == "web-reserved-0"
    # Reused rather than opening a second thread the user has to find.
    assert platform.threads == []
    assert [key for key, _ in platform.sent] == ["web-reserved-0"]
    assert platform.sent[0][1].text == "all done"
    assert platform.discarded == []
    assert platform.session_writes == [("web-reserved-0", "sess-1", "desc t1")]


def test_silent_run_discards_the_reserved_conversation():
    platform = ReservingPlatform()
    result = RunResult(text="nothing to report <silence>", session_id="sess-1")
    with patch("yuki_conductor.cron_scheduler.run_agent", return_value=result):
        _run_cron_task(_task(), {"web": platform})

    assert platform.discarded == ["web-reserved-0"]
    assert platform.sent == []


def test_platform_without_reserve_still_starts_a_thread():
    platform = FakePlatform("slack")
    result = RunResult(text="ping <notify>", session_id="sess-1")
    with patch(
        "yuki_conductor.cron_scheduler.run_agent", return_value=result
    ) as run:
        _run_cron_task(_task(chat_app="slack"), {"slack": platform})

    assert run.call_args.kwargs["push_conversation_id"] is None
    assert platform.threads == [("ping", "desc t1")]


# ---- Run history recording ----


def test_successful_run_is_recorded(isolated_cron_run_store):
    platform = FakePlatform("slack")
    result = RunResult(text="ping <notify>", session_id="sess-1")
    with patch("yuki_conductor.cron_scheduler.run_agent", return_value=result):
        _run_cron_task(_task(chat_app="slack"), {"slack": platform})

    run = isolated_cron_run_store.last_run("t1")
    assert run["status"] == "success"
    assert run["response"] == "ping"
    assert run["notified"] is True
    assert run["trigger"] == "schedule"
    assert run["session_id"] == "sess-1"


def test_silent_run_is_recorded_as_not_notified(isolated_cron_run_store):
    result = RunResult(text="nothing <silence>", session_id="sess-1")
    with patch("yuki_conductor.cron_scheduler.run_agent", return_value=result):
        _run_cron_task(_task(), {})

    run = isolated_cron_run_store.last_run("t1")
    assert run["status"] == "success"
    assert run["notified"] is False
    assert run["response"] == "nothing"


def test_claude_error_result_is_recorded_as_error(isolated_cron_run_store):
    result = RunResult(text="it broke", session_id=None, is_error=True)
    with patch("yuki_conductor.cron_scheduler.run_agent", return_value=result):
        _run_cron_task(_task(), {})

    run = isolated_cron_run_store.last_run("t1")
    assert run["status"] == "error"
    assert run["error"] == "it broke"


def test_raised_exception_is_recorded_as_error(isolated_cron_run_store):
    with patch(
        "yuki_conductor.cron_scheduler.run_agent",
        side_effect=RuntimeError("subprocess died"),
    ):
        _run_cron_task(_task(), {})

    run = isolated_cron_run_store.last_run("t1")
    assert run["status"] == "error"
    assert "subprocess died" in run["error"]
    assert run["finished_at"] is not None


def test_reserved_conversation_is_recorded_on_the_run(isolated_cron_run_store):
    platform = ReservingPlatform()
    result = RunResult(text="done <notify>", session_id="sess-1")
    with patch("yuki_conductor.cron_scheduler.run_agent", return_value=result):
        _run_cron_task(_task(), {"web": platform})

    assert isolated_cron_run_store.last_run("t1")["conversation_id"] == "web-reserved-0"


# ---- Manual triggering ----


def test_trigger_task_runs_a_defined_task(isolated_cron_run_store):
    from yuki_conductor import cron_scheduler

    result = RunResult(text="manual go <notify>", session_id="sess-1")
    with (
        patch.object(cron_scheduler, "_load_cron_tasks", return_value=[_task()]),
        patch.object(cron_scheduler, "run_agent", return_value=result),
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


# ---- Pausing ----


def test_paused_tasks_are_excluded_from_scheduler_state():
    active = _task("active")
    paused = _task("paused")
    paused.paused = True

    iters, next_times = _build_task_state([active, paused])
    assert [t.name for t, _ in iters] == ["active"]
    # No fire time at all, so the loop can never pick it up.
    assert "paused" not in next_times


def test_toggling_paused_counts_as_a_task_change():
    """Otherwise the reload wouldn't rebuild state and a pause wouldn't take."""
    before = _task()
    after = _task()
    after.paused = True
    assert _tasks_changed([before], [after])


def test_trigger_task_still_runs_a_paused_task(isolated_cron_run_store):
    """Pausing suspends the schedule; "Run now" is an explicit override."""
    from yuki_conductor import cron_scheduler

    paused = _task()
    paused.paused = True
    result = RunResult(text="manual go <notify>", session_id="sess-1")
    with (
        patch.object(cron_scheduler, "_load_cron_tasks", return_value=[paused]),
        patch.object(cron_scheduler, "run_agent", return_value=result),
    ):
        assert cron_scheduler.trigger_task("t1") is True
        for thread in threading.enumerate():
            if thread.name == "cron-manual-t1":
                thread.join(timeout=5)

    assert isolated_cron_run_store.last_run("t1")["status"] == "success"


# ---- Attachments in cron results ----


def test_cron_result_attachments_are_delivered(tmp_path):
    report = tmp_path / "report.png"
    report.write_bytes(b"png")
    platform = ReservingPlatform()
    result = RunResult(
        text=f"here it is <attachment>{report}</attachment> <notify>", session_id="s"
    )
    with patch("yuki_conductor.cron_scheduler.run_agent", return_value=result):
        _run_cron_task(_task(), {"web": platform})

    [(key, msg)] = platform.sent
    assert key == "web-reserved-0"
    assert msg.text == "here it is"
    assert [a.local_path for a in msg.attachments] == [report]


def test_cron_attachments_follow_a_new_thread(tmp_path):
    report = tmp_path / "report.png"
    report.write_bytes(b"png")
    platform = FakePlatform("slack")
    result = RunResult(
        text=f"done <attachment>{report}</attachment> <notify>", session_id="s"
    )
    with patch("yuki_conductor.cron_scheduler.run_agent", return_value=result):
        _run_cron_task(_task(chat_app="slack"), {"slack": platform})

    assert platform.threads == [("done", "desc t1")]
    [(key, msg)] = platform.sent
    assert key == "slack-thread-0"
    assert [a.local_path for a in msg.attachments] == [report]


def test_cron_task_harness_reaches_the_run_and_the_thread(monkeypatch):
    from yuki_conductor.harness import HARNESSES

    other = MagicMock(name="other-harness")
    other.name, other.label = "other", "Other"
    monkeypatch.setitem(HARNESSES, "other", other)

    class Recording(ReservingPlatform):
        def reserve_thread(self, title=None, harness="claude-code"):
            self.reserved_harness = harness
            return super().reserve_thread(title=title, harness=harness)

    platform = Recording()
    task = _task()
    task.harness = "other"
    result = RunResult(text="done <notify>", session_id="sess-1")
    with patch("yuki_conductor.cron_scheduler.run_agent", return_value=result) as run:
        _run_cron_task(task, {"web": platform})

    assert run.call_args.args[0] is other
    # Recorded on the thread up front, so replies resume with the same harness.
    assert platform.reserved_harness == "other"


def test_cron_task_naming_a_disabled_harness_is_recorded_and_not_run(isolated_cron_run_store):
    """A prompt written for one agent mustn't silently run on another."""
    platform = ReservingPlatform()
    task = _task()
    task.harness = "not-enabled-here"
    with patch("yuki_conductor.cron_scheduler.run_agent") as run:
        _run_cron_task(task, {"web": platform})

    run.assert_not_called()
    assert platform.reserved == []
    [row] = isolated_cron_run_store.list_runs("t1")
    assert row["status"] == "error"
    assert "isn't enabled" in row["error"]


def test_cron_task_harness_is_read_from_yaml(tmp_path):
    from yuki_conductor.cron_config import load_tasks

    path = tmp_path / "cron.yaml"
    path.write_text(
        "tasks:\n"
        "  - name: a\n    schedule: '* * * * *'\n    prompt: hi\n    harness: pi\n"
        "  - name: b\n    schedule: '* * * * *'\n    prompt: hi\n",
        encoding="utf-8",
    )
    assert [t.harness for t in load_tasks(path)] == ["pi", None]


def test_cron_alias_is_run_and_kept_for_followups():
    platform = ReservingPlatform()
    task = _task()
    task.model = "sonnet"
    result = RunResult(text="done <notify>", session_id="sess-1")
    with patch("yuki_conductor.cron_scheduler.run_agent", return_value=result) as run:
        _run_cron_task(task, {"web": platform})
    assert run.call_args.kwargs["model"] == "sonnet"
    assert platform.model_writes == [("web-reserved-0", "sonnet", "claude-code")]


def test_cron_model_changes_trigger_reload_and_parse(tmp_path):
    from dataclasses import replace

    from yuki_conductor.cron_config import load_tasks

    path = tmp_path / "cron.yaml"
    path.write_text("tasks:\n  - name: a\n    schedule: '* * * * *'\n"
                    "    prompt: hi\n    harness: codex\n    model: sol\n", encoding="utf-8")
    [task] = load_tasks(path)
    assert task.model == "sol"
    assert _tasks_changed([task], [replace(task, model="astra")])


# ---- Failure alerts ----


def _run(task, platform, text, is_error=False):
    result = RunResult(text=text, session_id="sess-1", is_error=is_error)
    with patch("yuki_conductor.cron_scheduler.run_agent", return_value=result):
        _run_cron_task(task, {platform.name: platform})


def test_failed_run_alerts_even_when_it_asked_for_silence(isolated_cron_run_store):
    platform = FakePlatform("slack")
    _run(_task(chat_app="slack"), platform, "WorkIQ 401 <silence>", is_error=True)

    [(text, title)] = platform.threads
    assert title == "desc t1"
    assert "failed once" in text and "WorkIQ 401" in text
    assert isolated_cron_run_store.last_run("t1")["notified"] is True


def test_failure_alert_into_reserved_conversation(isolated_cron_run_store):
    platform = ReservingPlatform()
    _run(_task(), platform, "", is_error=True)

    [(key, msg)] = platform.sent
    assert key == "web-reserved-0"
    assert "run failed with no output" in msg.text
    assert platform.discarded == []


def test_streak_alerts_once_at_threshold_then_recovers(isolated_cron_run_store):
    platform = FakePlatform("slack")
    task = _task(chat_app="slack")
    task.alert_after = 3

    _run(task, platform, "boom", is_error=True)
    _run(task, platform, "boom", is_error=True)
    assert platform.threads == []

    _run(task, platform, "boom", is_error=True)
    assert len(platform.threads) == 1
    assert "failed 3 times in a row" in platform.threads[0][0]

    _run(task, platform, "boom", is_error=True)
    assert len(platform.threads) == 1  # one alert per streak

    _run(task, platform, "all good <silence>")
    assert len(platform.threads) == 2
    recovered = platform.threads[1][0]
    assert recovered.startswith("✅") and "after 4 failed runs" in recovered
    assert "all good" in recovered

    _run(task, platform, "still good <silence>")
    assert len(platform.threads) == 2


def test_success_below_threshold_stays_silent(isolated_cron_run_store):
    platform = FakePlatform("slack")
    task = _task(chat_app="slack")
    task.alert_after = 2
    _run(task, platform, "boom", is_error=True)
    _run(task, platform, "fine <silence>")
    assert platform.threads == []


def test_alert_after_zero_never_alerts(isolated_cron_run_store):
    platform = FakePlatform("slack")
    task = _task(chat_app="slack")
    task.alert_after = 0
    _run(task, platform, "boom", is_error=True)
    _run(task, platform, "fine <silence>")
    assert platform.threads == []


def test_raised_exception_alerts(isolated_cron_run_store):
    platform = FakePlatform("slack")
    with patch(
        "yuki_conductor.cron_scheduler.run_agent",
        side_effect=RuntimeError("subprocess died"),
    ):
        _run_cron_task(_task(chat_app="slack"), {"slack": platform})

    [(text, _)] = platform.threads
    assert "RuntimeError: subprocess died" in text
    assert isolated_cron_run_store.last_run("t1")["notified"] is True


def test_disabled_harness_alerts(isolated_cron_run_store):
    platform = FakePlatform("slack")
    task = _task(chat_app="slack")
    task.harness = "not-enabled-here"
    with patch("yuki_conductor.cron_scheduler.run_agent") as run:
        _run_cron_task(task, {"slack": platform})

    run.assert_not_called()
    [(text, _)] = platform.threads
    assert "isn't enabled" in text


def test_long_errors_are_truncated_in_the_alert(isolated_cron_run_store):
    platform = FakePlatform("slack")
    _run(_task(chat_app="slack"), platform, "x" * 5000, is_error=True)
    [(text, _)] = platform.threads
    assert len(text) < 1700 and text.endswith("…")
    assert len(isolated_cron_run_store.last_run("t1")["error"]) == 5000


def test_failure_streak_skips_running_rows(isolated_cron_run_store):
    store = isolated_cron_run_store
    store.finish_run(store.start_run("t"), status="success")
    store.finish_run(store.start_run("t"), status="error")
    store.start_run("t")  # in flight
    store.finish_run(store.start_run("t"), status="error")
    assert store.failure_streak("t") == 2


def test_alert_after_is_read_from_yaml(tmp_path):
    from yuki_conductor.cron_config import load_tasks

    path = tmp_path / "cron.yaml"
    path.write_text(
        "tasks:\n"
        "  - name: a\n    schedule: '* * * * *'\n    prompt: hi\n    alert_after: 4\n"
        "  - name: b\n    schedule: '* * * * *'\n    prompt: hi\n",
        encoding="utf-8",
    )
    assert [t.alert_after for t in load_tasks(path)] == [4, 1]
