"""The taskboard state machine: allowed moves, system comments, listeners."""

import pytest

from yuki_conductor import taskboard
from yuki_conductor.task_store import (
    DONE,
    IN_PROGRESS,
    NEEDS_INPUT,
    READY_FOR_REVIEW,
    TODO,
)
from yuki_conductor.taskboard import transitions
from yuki_conductor.taskboard.transitions import InvalidTransition, claim, transition


@pytest.fixture
def events():
    seen = []
    transitions.add_listener(seen.append)
    yield seen
    transitions.remove_listener(seen.append)


def _new(title="t"):
    return taskboard.store.create(title=title, project="p", harness="claude-code", use_worktree=False)


def test_claim_records_and_notifies_listeners(events):
    task = _new()
    claimed = claim(task.id)
    assert claimed.status == IN_PROGRESS
    assert claim(task.id) is None
    assert len(events) == 1
    assert (events[0].from_status, events[0].to_status) == (TODO, IN_PROGRESS)
    assert taskboard.store.comments(task.id)[-1].body == "To Do → In Progress: picked up"


def test_transition_writes_system_comment(events):
    task = _new()
    claim(task.id)
    moved = transition(task.id, READY_FOR_REVIEW, reason="turn ended", notify=True)
    assert moved.status == READY_FOR_REVIEW
    last = taskboard.store.comments(task.id)[-1]
    assert last.author == "system"
    assert last.body == "In Progress → Ready for Review: turn ended"
    assert events[-1].notify is True
    assert events[-1].task.status == READY_FOR_REVIEW


def test_done_sets_done_at():
    task = _new()
    transition(task.id, NEEDS_INPUT, reason="cancelled")
    assert transition(task.id, DONE, reason="marked done").done_at is not None


def test_requeue_goes_to_end_or_top():
    a, _, c = _new("a"), _new("b"), _new("c")
    claim(a.id)
    transition(a.id, NEEDS_INPUT, reason="asked")
    transition(a.id, TODO, reason="new comment")
    assert [t.title for t in taskboard.store.list_status(TODO)] == ["b", "c", "a"]
    claim(c.id)
    transition(c.id, TODO, reason="re-queued after daemon restart", top=True)
    assert [t.title for t in taskboard.store.list_status(TODO)] == ["c", "b", "a"]


@pytest.mark.parametrize(
    "path, bad",
    [
        ([], DONE),                       # To Do can only be cancelled
        ([], READY_FOR_REVIEW),
        ([NEEDS_INPUT, DONE], TODO),      # Done is terminal
        ([NEEDS_INPUT], READY_FOR_REVIEW),
    ],
)
def test_disallowed_transitions(path, bad):
    task = _new()
    for step in path:
        transition(task.id, step, reason="setup")
    with pytest.raises(InvalidTransition):
        transition(task.id, bad, reason="nope")


def test_todo_to_in_progress_only_via_claim():
    task = _new()
    with pytest.raises(InvalidTransition):
        transition(task.id, IN_PROGRESS, reason="nope")


def test_transition_loses_race_to_claim(monkeypatch):
    task = _new()
    real_get = taskboard.store.get

    def stale_get(task_id):
        # The cancel read the task as To Do; the harvester claims it before
        # the cancel writes.
        snapshot = real_get(task_id)
        taskboard.store.claim(task_id)
        return snapshot

    with monkeypatch.context() as m:
        m.setattr(taskboard.store, "get", stale_get)
        with pytest.raises(InvalidTransition, match="left To Do"):
            transition(task.id, NEEDS_INPUT, reason="cancelled")
    assert taskboard.store.get(task.id).status == IN_PROGRESS


def test_unknown_task():
    with pytest.raises(KeyError):
        transition(404, DONE, reason="x")


def test_failing_listener_does_not_break_transition(events):
    def boom(_event):
        raise RuntimeError("listener bug")

    transitions.add_listener(boom)
    try:
        task = _new()
        assert transition(task.id, NEEDS_INPUT, reason="cancelled").status == NEEDS_INPUT
        assert events[-1].to_status == NEEDS_INPUT
    finally:
        transitions.remove_listener(boom)
