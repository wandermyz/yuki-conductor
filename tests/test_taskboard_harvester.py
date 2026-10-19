"""Pickup: per-harness concurrency, pause, disabled harnesses, recovery."""

import threading

import pytest

from yuki_conductor import config, taskboard
from yuki_conductor.task_store import IN_PROGRESS, NEEDS_INPUT, TODO
from yuki_conductor.taskboard import harvester as harvester_mod
from yuki_conductor.taskboard import transitions
from yuki_conductor.taskboard.harvester import Harvester
from yuki_conductor.taskboard.transitions import claim, transition


@pytest.fixture(autouse=True)
def harnesses(monkeypatch):
    monkeypatch.setattr(harvester_mod, "HARNESSES", {"claude-code": object(), "pi": object()})
    monkeypatch.setattr(config, "TASKBOARD_CONCURRENCY", {"claude-code": 2, "pi": 1})


class Gate:
    """A fake turn that blocks until released, then finishes the task."""

    def __init__(self):
        self.release = threading.Event()
        self.started: list[int] = []

    def __call__(self, task_id):
        self.started.append(task_id)
        self.release.wait(5)
        transition(task_id, NEEDS_INPUT, reason="fake turn ended")


def _new(harness="claude-code", title="t"):
    return taskboard.store.create(title=title, project="p", harness=harness, use_worktree=False)


def test_respects_per_harness_limits():
    gate = Gate()
    h = Harvester(run_turn=gate)
    claude = [_new() for _ in range(4)]
    pis = [_new("pi") for _ in range(2)]

    started = h.tick()
    assert started == [claude[0].id, claude[1].id, pis[0].id]
    assert h.running() == {"claude-code": 2, "pi": 1}
    assert h.tick() == []  # still full

    gate.release.set()
    h.join(5)
    assert h.running() == {}
    assert set(h.tick()) == {claude[2].id, claude[3].id, pis[1].id}
    h.join(5)


def test_busy_harness_does_not_block_others():
    gate = Gate()
    h = Harvester(run_turn=gate)
    pi_a, pi_b, claude = _new("pi"), _new("pi"), _new()
    assert h.tick() == [pi_a.id, claude.id]
    assert taskboard.store.get(pi_b.id).status == TODO
    gate.release.set()
    h.join(5)


def test_queue_order_is_respected():
    gate = Gate()
    gate.release.set()
    h = Harvester(run_turn=gate)
    a, b = _new(title="a"), _new(title="b")
    taskboard.store.move_to_top(b.id)
    assert h.tick() == [b.id, a.id]
    h.join(5)


def test_paused_board_starts_nothing():
    h = Harvester(run_turn=Gate())
    _new()
    taskboard.set_paused(True)
    assert h.tick() == []
    taskboard.set_paused(False)
    started = h.tick()
    assert len(started) == 1
    h._run_turn.release.set()
    h.join(5)


def test_disabled_harness_is_skipped(monkeypatch):
    monkeypatch.setattr(harvester_mod, "HARNESSES", {"claude-code": object()})
    gate = Gate()
    gate.release.set()
    h = Harvester(run_turn=gate)
    stuck, ok = _new("pi"), _new()
    assert h.tick() == [ok.id]
    assert taskboard.store.get(stuck.id).status == TODO
    h.join(5)


def test_cancelled_task_is_not_picked_up(monkeypatch):
    gate = Gate()
    h = Harvester(run_turn=gate)
    task = _new()
    real_claim = harvester_mod.claim

    def cancel_then_claim(task_id):
        transition(task_id, NEEDS_INPUT, reason="cancelled")
        return real_claim(task_id)

    monkeypatch.setattr(harvester_mod, "claim", cancel_then_claim)
    assert h.tick() == []
    assert taskboard.store.get(task.id).status == NEEDS_INPUT


def test_recover_requeues_at_the_top():
    _new(title="a"), _new(title="b")
    c = _new(title="c")
    claim(c.id)
    taskboard.store.update_fields(c.id, session_id="keep-me")
    Harvester(run_turn=Gate()).recover()
    queue = taskboard.store.list_status(TODO)
    assert [t.title for t in queue] == ["c", "a", "b"]
    assert queue[0].session_id == "keep-me"
    assert "daemon restart" in taskboard.store.comments(c.id)[-1].body
    assert taskboard.store.list_status(IN_PROGRESS) == []


def test_requeue_wakes_the_loop():
    h = Harvester(run_turn=Gate())
    transitions.add_listener(h.on_transition)
    task = _new()
    transition(task.id, NEEDS_INPUT, reason="cancelled")
    h._wake.clear()
    transition(task.id, TODO, reason="new comment")
    assert h._wake.is_set()


def test_loop_runs_until_stopped():
    gate = Gate()
    gate.release.set()
    h = Harvester(run_turn=gate)
    task = _new()
    stop = threading.Event()
    thread = threading.Thread(target=h.loop, args=(stop,), daemon=True)
    thread.start()
    h.wake()
    for _ in range(50):
        if gate.started:
            break
        threading.Event().wait(0.05)
    stop.set()
    h.wake()
    thread.join(5)
    h.join(5)
    assert gate.started == [task.id]
    assert not thread.is_alive()


def test_start_taskboard_recovers_and_broadcasts(monkeypatch):
    from yuki_conductor.taskboard import live

    sent = []

    class Manager:
        def broadcast_all(self, payload):
            sent.append(payload)

    monkeypatch.setattr(taskboard, "active_harvester", None)
    monkeypatch.setattr(harvester_mod.Harvester, "loop", lambda self, stop: None)
    task = _new()
    claim(task.id)
    try:
        taskboard.start_taskboard({}, Manager())
    finally:
        live.attach(None)
    assert taskboard.store.get(task.id).status == TODO
    assert {"type": "task", "id": task.id} in sent
    assert taskboard.active_harvester is not None


def test_task_still_finishing_is_not_picked_up_again():
    """A turn that ends by re-queueing its task must exit before it restarts."""
    entered, release = threading.Event(), threading.Event()

    def requeue_then_linger(task_id):
        transition(task_id, TODO, reason="new comment during the turn")
        entered.set()
        release.wait(5)

    h = Harvester(run_turn=requeue_then_linger)
    task = _new()
    assert h.tick() == [task.id]
    entered.wait(5)
    assert h.tick() == []  # back in To Do, but its thread hasn't exited
    h._run_turn = Gate()
    h._run_turn.release.set()
    release.set()
    h.join(5)
    assert h.tick() == [task.id]
    h.join(5)
