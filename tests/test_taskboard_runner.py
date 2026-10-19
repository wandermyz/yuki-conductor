"""One task turn: prompt choice, outcomes, failures, cancel."""

import threading

import pytest

from yuki_conductor import taskboard
from yuki_conductor.harness.base import RunResult, StreamEvent
from yuki_conductor.task_store import NEEDS_INPUT, READY_FOR_REVIEW, TODO
from yuki_conductor.taskboard import runner
from yuki_conductor.taskboard.transitions import claim
from yuki_conductor.taskboard.worktree import WorktreeError


class FakeHarness:
    name = "claude-code"
    label = "Fake"
    bin = "fake"
    missing_session_marker = None

    def __init__(self):
        self.sessions: set[str] = set()

    def session_exists(self, session_id, cwd):
        return session_id in self.sessions


class FakeProjects:
    paths = {"proj": "/repo"}

    def get_path(self, name):
        return self.paths.get(name)


@pytest.fixture
def env(monkeypatch):
    """Patch everything a turn touches outside the store; scripts the agent.

    `env.script` is a list of callables `(prompt, kwargs) -> RunResult`, one
    per run_agent call.
    """
    class Env:
        harness = FakeHarness()
        calls: list[dict] = []
        script: list = []

    def fake_run_agent(harness, prompt, **kwargs):
        Env.calls.append({"prompt": prompt, **kwargs})
        kwargs["on_event"](StreamEvent(kind="text", label="working"))
        return Env.script.pop(0)(prompt, kwargs)

    Env.calls = []
    Env.script = []
    monkeypatch.setattr(runner, "run_agent", fake_run_agent)
    monkeypatch.setattr(runner, "get_harness", lambda name: Env.harness)
    monkeypatch.setattr(runner, "ProjectStore", FakeProjects)
    return Env


def reply(text="done", session_id="s1", **kw):
    return lambda prompt, kwargs: RunResult(text=text, session_id=session_id, **kw)


def _claimed(**kw):
    kw.setdefault("use_worktree", False)
    task = taskboard.store.create(title="Do it", project="proj", harness="claude-code", body="the body", **kw)
    claim(task.id)
    return task


def _status(task):
    return taskboard.store.get(task.id).status


def _agent_comments(task):
    return [c.body for c in taskboard.store.comments(task.id) if c.author == "agent"]


def test_first_turn_runs_in_project_and_lands_in_review(env):
    task = _claimed()
    env.script = [reply("All done.")]
    runner.run_turn(task.id)

    call = env.calls[0]
    assert call["cwd"] == "/repo"
    assert call["session_id"] is None
    assert call["task_id"] == task.id
    assert call["conversation_key"] == f"task:{task.id}"
    assert "# Task #" in call["prompt"] and "the body" in call["prompt"]
    stored = taskboard.store.get(task.id)
    assert stored.status == READY_FOR_REVIEW
    assert stored.session_id == "s1"
    assert _agent_comments(task) == ["All done."]


def test_pr_url_in_reply_is_recorded(env, monkeypatch):
    monkeypatch.setattr(runner, "_origin_repo", lambda cwd: "o/r")
    task = _claimed()
    env.script = [reply("Fixes the bug upstream reported in https://github.com/up/lib/pull/7. "
                        "Opened https://github.com/o/r/pull/42 for review")]
    runner.run_turn(task.id)
    assert taskboard.store.get(task.id).pr_url == "https://github.com/o/r/pull/42"
    assert taskboard.store.comments(task.id)[-1].body.endswith("PR opened")


def test_unrelated_pr_url_is_not_recorded(env, monkeypatch):
    monkeypatch.setattr(runner, "_origin_repo", lambda cwd: "o/r")
    task = _claimed()
    env.script = [reply("See https://github.com/up/lib/pull/7")]
    runner.run_turn(task.id)
    assert taskboard.store.get(task.id).pr_url is None


def test_origin_repo_parsing(tmp_path):
    import subprocess

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "git@github.com:Owner/my.repo.git"], check=True)
    assert runner._origin_repo(str(tmp_path)) == "Owner/my.repo"
    assert runner._origin_repo(str(tmp_path / "nope")) is None


def test_ask_lands_in_needs_input(env):
    task = _claimed()

    def asks(prompt, kwargs):
        taskboard.store.update_fields(task.id, asked_input=True)
        return RunResult(text="Which DB?", session_id="s1")

    env.script = [asks]
    runner.run_turn(task.id)
    assert _status(task) == NEEDS_INPUT


def test_error_lands_in_needs_input_with_details(env):
    task = _claimed()
    env.script = [reply("boom: rate limited", is_error=True)]
    runner.run_turn(task.id)
    assert _status(task) == NEEDS_INPUT
    assert any("rate limited" in c.body for c in taskboard.store.comments(task.id) if c.author == "system")


def test_failed_run_keeps_its_comments_pending(env):
    task = taskboard.store.create(title="t", project="proj", harness="claude-code", use_worktree=False)
    comment = taskboard.store.add_comment(task.id, "user", "use postgres")
    claim(task.id)
    env.script = [reply("launch failed", is_error=True)]
    runner.run_turn(task.id)
    # Not re-queued as a "new" comment, but sent again on the next turn.
    assert _status(task) == NEEDS_INPUT
    assert [c.id for c in taskboard.store.undelivered(task.id)] == [comment.id]


def test_resume_without_new_comments_says_continue(env):
    task = _claimed()
    taskboard.store.update_fields(task.id, session_id="s1")
    env.harness.sessions.add("s1")
    env.script = [reply()]
    runner.run_turn(task.id)
    assert env.calls[0]["prompt"].startswith("Continue working on your task")


def test_comment_after_the_outcome_check_is_not_stranded(env, monkeypatch):
    task = _claimed()
    env.script = [reply()]
    real_transition = runner.transition
    posted = []

    def transition_then_comment(task_id, to, **kw):
        # The user's comment lands just before the move to Ready for Review:
        # it saw In Progress, so it didn't re-queue the task itself.
        if not posted:
            posted.append(taskboard.store.add_comment(task_id, "user", "one more thing"))
        return real_transition(task_id, to, **kw)

    monkeypatch.setattr(runner, "transition", transition_then_comment)
    runner.run_turn(task.id)
    assert _status(task) == TODO


def test_comment_during_turn_requeues(env):
    task = _claimed()

    def user_comments_meanwhile(prompt, kwargs):
        taskboard.store.add_comment(task.id, "user", "also do X")
        return RunResult(text="done", session_id="s1")

    env.script = [user_comments_meanwhile]
    runner.run_turn(task.id)
    assert _status(task) == TODO


def test_comments_before_pickup_go_into_first_prompt(env):
    task = taskboard.store.create(title="t", project="proj", harness="claude-code", use_worktree=False)
    taskboard.store.add_comment(task.id, "user", "use postgres")
    claim(task.id)
    env.script = [reply()]
    runner.run_turn(task.id)
    assert "use postgres" in env.calls[0]["prompt"]
    assert taskboard.store.undelivered(task.id) == []
    assert _status(task) == READY_FOR_REVIEW


def test_follow_up_resumes_with_only_new_comments(env):
    task = _claimed()
    env.script = [reply(session_id="s1")]
    runner.run_turn(task.id)
    env.harness.sessions.add("s1")
    taskboard.store.add_comment(task.id, "user", "please also add tests")
    from yuki_conductor.taskboard.transitions import transition

    transition(task.id, TODO, reason="new comment")
    claim(task.id)
    env.script = [reply("added", session_id="s1")]
    runner.run_turn(task.id)

    call = env.calls[1]
    assert call["session_id"] == "s1"
    assert "please also add tests" in call["prompt"]
    assert "the body" not in call["prompt"]


def test_gone_session_starts_fresh_with_full_context(env):
    task = _claimed()
    taskboard.store.update_fields(task.id, session_id="old")
    taskboard.store.add_comment(task.id, "user", "earlier feedback")
    env.script = [reply(session_id="new")]
    runner.run_turn(task.id)

    call = env.calls[0]
    assert call["session_id"] is None
    assert "session is gone" in call["prompt"]
    assert "earlier feedback" in call["prompt"] and "the body" in call["prompt"]
    assert taskboard.store.get(task.id).session_id == "new"


def test_session_missing_mid_resume_retries_fresh(env):
    task = _claimed()
    taskboard.store.update_fields(task.id, session_id="s1")
    env.harness.sessions.add("s1")
    env.script = [
        reply("No conversation found", session_id="s1", is_error=True, session_missing=True),
        reply("recovered", session_id="s2"),
    ]
    runner.run_turn(task.id)
    assert [c["session_id"] for c in env.calls] == ["s1", None]
    assert _status(task) == READY_FOR_REVIEW
    assert taskboard.store.get(task.id).session_id == "s2"


def test_missing_project_fails_the_turn(env):
    task = _claimed()
    taskboard.store.update_fields(task.id, project="gone")
    runner.run_turn(task.id)
    assert env.calls == []
    assert _status(task) == NEEDS_INPUT


def test_worktree_failure_fails_the_turn(env, monkeypatch):
    task = _claimed(use_worktree=True)

    def broken(task, repo):
        raise WorktreeError("fatal: not a git repository")

    monkeypatch.setattr(runner, "ensure_worktree", broken)
    runner.run_turn(task.id)
    assert env.calls == []
    assert _status(task) == NEEDS_INPUT
    assert "not a git repository" in taskboard.store.comments(task.id)[-2].body


def test_worktree_task_runs_in_the_worktree(env, monkeypatch):
    task = _claimed(use_worktree=True)
    monkeypatch.setattr(runner, "ensure_worktree", lambda t, repo: ("/repo/.claude/worktrees/task-1", "task/1-do-it"))
    env.script = [reply()]
    runner.run_turn(task.id)
    assert env.calls[0]["cwd"] == "/repo/.claude/worktrees/task-1"
    stored = taskboard.store.get(task.id)
    assert (stored.worktree_path, stored.branch) == ("/repo/.claude/worktrees/task-1", "task/1-do-it")


def test_crash_never_strands_the_task(env):
    task = _claimed()

    def crash(prompt, kwargs):
        raise RuntimeError("parser exploded")

    env.script = [crash]
    runner.run_turn(task.id)
    assert _status(task) == NEEDS_INPUT


def test_cancel_lands_in_needs_input_without_agent_comment(env, monkeypatch):
    task = _claimed()
    monkeypatch.setattr(runner, "cancel_process", lambda key: True)

    def cancelled(prompt, kwargs):
        runner.request_cancel(task.id)
        return RunResult(text="(stopped by user)", session_id="s1")

    env.script = [cancelled]
    runner.run_turn(task.id)
    assert _status(task) == NEEDS_INPUT
    assert _agent_comments(task) == []
    assert taskboard.store.comments(task.id)[-1].body.endswith("cancelled")


def test_cancel_before_the_run_starts_skips_it(env, monkeypatch):
    task = _claimed()
    monkeypatch.setattr(runner, "cancel_process", lambda key: False)
    runner.request_cancel(task.id)  # e.g. while the worktree was being made
    runner.run_turn(task.id)
    assert env.calls == []
    assert _status(task) == NEEDS_INPUT


def test_stale_cancel_is_cleared_at_pickup(env, monkeypatch):
    from yuki_conductor.taskboard.harvester import Harvester

    monkeypatch.setattr(runner, "cancel_process", lambda key: False)
    task = taskboard.store.create(title="t", project="proj", harness="claude-code", use_worktree=False)
    runner.request_cancel(task.id)  # arrived after the previous turn ended
    env.script = [reply()]
    h = Harvester()
    monkeypatch.setattr("yuki_conductor.taskboard.harvester.HARNESSES", {"claude-code": object()})
    assert h.tick() == [task.id]
    h.join(5)
    assert len(env.calls) == 1
    assert _status(task) == READY_FOR_REVIEW


def test_live_steps_and_processing_are_reported(env):
    from yuki_conductor.taskboard import live

    class Manager:
        def __init__(self):
            self.events = []
            self.lock = threading.Lock()

        def set_processing(self, key, msg_id, on):
            self.events.append(("processing", key, on))

        def add_step(self, key, step):
            self.events.append(("step", key, step["label"]))

        def broadcast_all(self, payload):
            self.events.append(("broadcast", payload["id"]))

    manager = Manager()
    live.attach(manager)
    try:
        task = _claimed()
        env.script = [reply()]
        runner.run_turn(task.id)
    finally:
        live.attach(None)
    key = f"task:{task.id}"
    assert ("processing", key, True) in manager.events
    assert ("step", key, "working") in manager.events
    assert ("processing", key, False) in manager.events
    assert ("broadcast", task.id) in manager.events


@pytest.mark.parametrize(
    "kw, cancelled, comments, expected",
    [
        ({}, False, False, READY_FOR_REVIEW),
        ({"is_error": True}, False, False, NEEDS_INPUT),
        ({"is_error": True}, True, False, NEEDS_INPUT),
        ({}, True, True, TODO),  # new comments beat a cancel
        ({"is_error": True}, False, True, TODO),
    ],
)
def test_resolve_outcome_precedence(kw, cancelled, comments, expected):
    task = taskboard.store.create(title="t", project="p", harness="claude-code", use_worktree=False)
    result = RunResult(text="x", session_id=None, **kw)
    outcome = runner.resolve_outcome(task, result, cancelled=cancelled, has_new_comments=comments)
    assert outcome.status == expected


def test_resolve_outcome_notifies_only_when_the_user_is_needed():
    task = taskboard.store.create(title="t", project="p", harness="claude-code", use_worktree=False)
    ok = RunResult(text="x", session_id=None)
    assert runner.resolve_outcome(task, ok, cancelled=False, has_new_comments=False).notify
    assert not runner.resolve_outcome(task, ok, cancelled=True, has_new_comments=False).notify
    assert not runner.resolve_outcome(task, ok, cancelled=False, has_new_comments=True).notify


