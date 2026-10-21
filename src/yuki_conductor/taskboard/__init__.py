"""The taskboard: a queue of agent tasks the daemon picks up on its own.

The module-level stores are what every part of the board reads and writes;
tests rebind them (see `tests/conftest.py`). `start_taskboard` wires the rest
together inside the daemon.
"""

import threading

from yuki_conductor.task_store import TaskboardStateStore, TaskStore

store = TaskStore()
board_state = TaskboardStateStore()

# The daemon's Harvester, once `start_taskboard` has run. (Not `harvester`:
# that name is the submodule.)
active_harvester = None


def start_taskboard(platforms_by_name: dict | None = None, ws_manager=None) -> threading.Event:
    """Recover tasks left In Progress, then start picking up To Do tasks.

    `platforms_by_name` are the enabled chat platforms (for notifications);
    `ws_manager` is the web `ConnectionManager` the board reports live state
    to. Returns an event that stops the loop when set.
    """
    global active_harvester
    from yuki_conductor.taskboard import cleanup, live, notify, transitions
    from yuki_conductor.taskboard.harvester import Harvester

    live.attach(ws_manager)
    notify.set_platforms(platforms_by_name or {})
    active_harvester = Harvester()
    transitions.add_listener(lambda event: live.task_changed(event.task.id))
    transitions.add_listener(active_harvester.on_transition)
    transitions.add_listener(notify.on_transition)
    transitions.add_listener(cleanup.on_transition)
    active_harvester.recover()

    stop = threading.Event()
    threading.Thread(
        target=active_harvester.loop, args=(stop,), name="taskboard-harvester", daemon=True
    ).start()
    return stop


def set_paused(paused: bool) -> None:
    """Stop or resume new pickups. Running turns are never interrupted."""
    board_state.set_paused(paused)
    if active_harvester is not None and not paused:
        active_harvester.wake()
