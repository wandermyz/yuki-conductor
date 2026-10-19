"""Pick up To Do tasks as harness capacity frees up.

One loop thread walks the queue in order and starts each task whose harness
is enabled, has a free slot (`config.taskboard_limit`), and isn't paused
board-wide. Each picked task runs on its own thread via `runner.run_turn`.
"""

import logging
import threading

from yuki_conductor import config, taskboard
from yuki_conductor.harness import HARNESSES
from yuki_conductor.task_store import IN_PROGRESS, TODO
from yuki_conductor.taskboard import runner
from yuki_conductor.taskboard.transitions import TransitionEvent, claim, transition

logger = logging.getLogger(__name__)

POLL_SECONDS = 2.0


class Harvester:
    def __init__(self, run_turn=runner.run_turn) -> None:
        self._run_turn = run_turn
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._running: dict[int, str] = {}  # task id -> harness
        self._threads: dict[int, threading.Thread] = {}

    def wake(self) -> None:
        self._wake.set()

    def on_transition(self, event: TransitionEvent) -> None:
        if event.to_status == TODO:
            self.wake()

    def running(self) -> dict[str, int]:
        """Turns in flight per harness."""
        counts: dict[str, int] = {}
        with self._lock:
            for harness in self._running.values():
                counts[harness] = counts.get(harness, 0) + 1
        return counts

    def recover(self) -> None:
        """Re-queue tasks a previous daemon left In Progress, at the top so
        they're picked up first. Their session is kept, so they resume."""
        for task in taskboard.store.list_status(IN_PROGRESS):
            transition(task.id, TODO, reason="re-queued after daemon restart", top=True)

    def tick(self) -> list[int]:
        """Start whatever can start now. Returns the ids started."""
        if taskboard.board_state.paused():
            return []
        started = []
        counts = self.running()
        with self._lock:
            finishing = set(self._threads)
        for task in taskboard.store.list_status(TODO):
            if task.id in finishing:
                continue  # back in To Do, but its last turn's thread is still exiting
            if task.harness not in HARNESSES:
                continue  # shown as "harness not enabled" in the UI
            if counts.get(task.harness, 0) >= config.taskboard_limit(task.harness):
                continue
            # Forget a stale cancel before claiming, never after: a cancel
            # that races the claim must survive to stop the new turn.
            runner.clear_cancel(task.id)
            if claim(task.id) is None:
                continue  # cancelled or edited away since the list was read
            counts[task.harness] = counts.get(task.harness, 0) + 1
            self._start(task.id, task.harness)
            started.append(task.id)
        return started

    def _start(self, task_id: int, harness: str) -> None:
        thread = threading.Thread(
            target=self._run, args=(task_id,), name=f"task-{task_id}", daemon=True
        )
        with self._lock:
            self._running[task_id] = harness
            self._threads[task_id] = thread
        thread.start()

    def _run(self, task_id: int) -> None:
        try:
            self._run_turn(task_id)
        finally:
            with self._lock:
                self._running.pop(task_id, None)
                self._threads.pop(task_id, None)
            self.wake()

    def join(self, timeout: float | None = None) -> None:
        """Wait for every running turn (tests)."""
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout)

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Taskboard harvester tick failed")
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()
