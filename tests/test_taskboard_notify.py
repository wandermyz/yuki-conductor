"""Which task transitions ping the user, and how."""

import pytest

from yuki_conductor import taskboard, web_push
from yuki_conductor.task_store import NEEDS_INPUT, READY_FOR_REVIEW, TODO
from yuki_conductor.taskboard import notify, transitions
from yuki_conductor.taskboard.transitions import claim, transition


class InlineThread:
    """Run the notification's background send right away."""

    def __init__(self, target, args, **_):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def pushes(monkeypatch):
    monkeypatch.setattr(notify.threading, "Thread", InlineThread)
    sent = []
    monkeypatch.setattr(web_push, "notify", lambda target, title, text: sent.append((target, title, text)))
    transitions.add_listener(notify.on_transition)
    return sent


def _running():
    task = taskboard.store.create(title="Add cache", project="p", harness="claude-code", use_worktree=False)
    claim(task.id)
    return task


def test_needs_input_pushes_without_the_question(pushes):
    task = _running()
    taskboard.store.add_comment(task.id, "agent", "Postgres or SQLite?")
    transition(task.id, NEEDS_INPUT, reason="the agent asked for input", notify=True)
    ((target, title, text),) = pushes
    assert target == {"kind": "task", "id": task.id}
    assert title == f"Task #{task.id}: Add cache"
    assert text == "Moved to Needs Input"


def test_review_pushes_without_content(pushes):
    task = _running()
    taskboard.store.update_fields(task.id, pr_url="https://github.com/o/r/pull/1")
    transition(task.id, READY_FOR_REVIEW, reason="PR opened", notify=True)
    assert pushes == [({"kind": "task", "id": task.id}, f"Task #{task.id}: Add cache", "Moved to Ready for Review")]


def test_silent_transitions(pushes):
    task = _running()
    transition(task.id, TODO, reason="new comment during the turn")
    claim(task.id)
    transition(task.id, NEEDS_INPUT, reason="cancelled")
    assert pushes == []


def test_comment_with_notify_pushes_that_it_exists(pushes):
    from yuki_conductor.taskboard import actions

    task = _running()
    actions.add_comment(task.id, "Halfway: tests pass", author="agent", notify_user=True)
    actions.add_comment(task.id, "quiet progress note", author="agent")
    assert pushes == [({"kind": "task", "id": task.id}, f"Task #{task.id}: Add cache", "Agent posted a comment")]
