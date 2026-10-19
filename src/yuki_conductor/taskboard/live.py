"""The board's link to whatever shows it live (the web UI).

The runner and transitions report here; `attach()` connects a
`ConnectionManager` at startup. Unattached (tests, CLI), every call is a
no-op, so nothing in the board depends on the web server.
"""

from yuki_conductor.harness.base import StreamEvent

_manager = None


def attach(manager) -> None:
    global _manager
    _manager = manager


def steps_key(task_id: int) -> str:
    """The `ConnectionManager` key a task's live steps are buffered under."""
    return f"task:{task_id}"


def task_changed(task_id: int) -> None:
    """Tell browsers to refetch a task (status, fields or comments changed)."""
    if _manager is not None:
        _manager.broadcast_all({"type": "task", "id": task_id})


def turn_started(task_id: int) -> None:
    if _manager is not None:
        _manager.set_processing(steps_key(task_id), "", True)


def turn_ended(task_id: int) -> None:
    if _manager is not None:
        _manager.set_processing(steps_key(task_id), "", False)


def step(task_id: int, event: StreamEvent) -> None:
    if _manager is not None:
        _manager.add_step(steps_key(task_id), event.to_dict())


def steps(task_id: int) -> list[dict]:
    if _manager is None:
        return []
    return _manager.get_steps().get(steps_key(task_id), [])
