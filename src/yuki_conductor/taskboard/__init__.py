"""The taskboard: a queue of agent tasks the daemon picks up on its own.

The module-level stores are what every part of the board reads and writes;
tests rebind them (see `tests/conftest.py`).
"""

from yuki_conductor.task_store import TaskboardStateStore, TaskStore

store = TaskStore()
board_state = TaskboardStateStore()
