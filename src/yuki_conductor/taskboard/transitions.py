"""The one place a task's status changes.

`transition()` (and `claim()` for pickup) writes the new status, leaves a
system comment on the timeline, and then tells every registered listener.
Side effects that belong to later stages — the web broadcast, notifications,
waking the harvester, worktree cleanup — subscribe through `add_listener`
rather than being called from here, so this module stays free of web and git
imports.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from yuki_conductor import taskboard
from yuki_conductor.task_store import (
    DONE,
    IN_PROGRESS,
    NEEDS_INPUT,
    READY_FOR_REVIEW,
    STATUS_LABELS,
    TODO,
    Task,
)

logger = logging.getLogger(__name__)

# The state machine from the PRD. To Do → In Progress isn't here: only
# `claim()` does that, atomically.
ALLOWED: dict[str, frozenset[str]] = {
    TODO: frozenset({NEEDS_INPUT}),
    IN_PROGRESS: frozenset({TODO, NEEDS_INPUT, READY_FOR_REVIEW}),
    NEEDS_INPUT: frozenset({TODO, DONE}),
    READY_FOR_REVIEW: frozenset({TODO, DONE}),
    DONE: frozenset({TODO}),  # reopened
}


class InvalidTransition(Exception):
    pass


@dataclass
class TransitionEvent:
    task: Task  # as it is after the change
    from_status: str
    to_status: str
    reason: str
    notify: bool  # whether the user should be pinged about it


Listener = Callable[[TransitionEvent], None]

_listeners: list[Listener] = []


def add_listener(listener: Listener) -> None:
    _listeners.append(listener)


def remove_listener(listener: Listener) -> None:
    if listener in _listeners:
        _listeners.remove(listener)


def transition(
    task_id: int,
    to: str,
    *,
    reason: str,
    notify: bool = False,
    top: bool = False,
) -> Task:
    """Move a task to `to`, record why, and tell the listeners.

    A task entering To Do goes to the end of the queue, or the top when `top`
    is set (restart recovery). Raises `InvalidTransition` for a move the state
    machine doesn't allow — including when the task moved on between the
    read and the write — and `KeyError` for an unknown task.
    """
    task = taskboard.store.get(task_id)
    if task is None:
        raise KeyError(task_id)
    if to not in ALLOWED.get(task.status, frozenset()):
        raise InvalidTransition(
            f"Task #{task_id}: {STATUS_LABELS[task.status]} → "
            f"{STATUS_LABELS.get(to, to)} isn't allowed"
        )
    position = ("top" if top else "end") if to == TODO else None
    if not taskboard.store.set_status(task_id, to, expected=task.status, position=position):
        raise InvalidTransition(
            f"Task #{task_id} left {STATUS_LABELS[task.status]} before it could move to "
            f"{STATUS_LABELS[to]}"
        )
    return _record(task_id, task.status, to, reason, notify)


def claim(task_id: int, *, reason: str = "picked up") -> Task | None:
    """Pick up a To Do task. Returns None if it was no longer To Do."""
    if not taskboard.store.claim(task_id):
        return None
    return _record(task_id, TODO, IN_PROGRESS, reason, notify=False)


def _record(task_id: int, from_status: str, to: str, reason: str, notify: bool) -> Task:
    taskboard.store.add_comment(
        task_id,
        "system",
        f"{STATUS_LABELS[from_status]} → {STATUS_LABELS[to]}: {reason}",
    )
    task = taskboard.store.get(task_id)
    event = TransitionEvent(
        task=task, from_status=from_status, to_status=to, reason=reason, notify=notify
    )
    for listener in list(_listeners):
        try:
            listener(event)
        except Exception:
            logger.warning("Taskboard transition listener failed", exc_info=True)
    return task
