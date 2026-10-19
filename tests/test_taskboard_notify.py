"""Which task transitions ping the user, and how."""

import pytest

from yuki_conductor import taskboard, web_push
from yuki_conductor.task_store import NEEDS_INPUT, READY_FOR_REVIEW, TODO
from yuki_conductor.taskboard import notify, transitions
from yuki_conductor.taskboard.transitions import claim, transition


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(web_push, "notify", lambda target, title, text: sent.append((target, title, text)))
    transitions.add_listener(notify.on_transition)
    return sent


class FakeSlack:
    def __init__(self):
        self.sent = []

    def send_notification(self, text):
        self.sent.append(text)


def _running():
    task = taskboard.store.create(title="Add cache", project="p", harness="claude-code", use_worktree=False)
    claim(task.id)
    return task


def test_needs_input_pushes_with_the_question(pushes):
    notify.set_platforms({})
    task = _running()
    taskboard.store.add_comment(task.id, "agent", "Postgres or SQLite?")
    transition(task.id, NEEDS_INPUT, reason="the agent asked for input", notify=True)
    ((target, title, text),) = pushes
    assert target == {"kind": "task", "id": task.id}
    assert title == f"Task #{task.id} · Needs Input: Add cache"
    assert text == "Postgres or SQLite?"


def test_review_includes_the_pr_and_reaches_slack(pushes):
    slack = FakeSlack()
    notify.set_platforms({"slack": slack})
    try:
        task = _running()
        taskboard.store.update_fields(task.id, pr_url="https://github.com/o/r/pull/1")
        transition(task.id, READY_FOR_REVIEW, reason="PR opened", notify=True)
    finally:
        notify.set_platforms({})
    assert pushes[0][2].startswith("https://github.com/o/r/pull/1")
    assert slack.sent and "Ready for Review" in slack.sent[0]


def test_silent_transitions(pushes):
    notify.set_platforms({})
    task = _running()
    transition(task.id, TODO, reason="new comment during the turn")
    claim(task.id)
    transition(task.id, NEEDS_INPUT, reason="cancelled")
    assert pushes == []


def test_slack_failure_is_swallowed(pushes):
    class Broken:
        def send_notification(self, text):
            raise RuntimeError("slack down")

    notify.set_platforms({"slack": Broken()})
    try:
        task = _running()
        transition(task.id, NEEDS_INPUT, reason="x", notify=True)
    finally:
        notify.set_platforms({})
    assert len(pushes) == 1
