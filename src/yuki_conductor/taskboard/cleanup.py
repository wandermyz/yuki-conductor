"""Clean up after a worktree task once it's Done."""

import logging
import threading

from yuki_conductor import taskboard
from yuki_conductor.store import ProjectStore
from yuki_conductor.task_store import DONE
from yuki_conductor.taskboard import live, pr_watcher
from yuki_conductor.taskboard.transitions import TransitionEvent
from yuki_conductor.taskboard.worktree import WorktreeError, cleanup

logger = logging.getLogger(__name__)


def run(task_id: int) -> bool:
    """Clean up one task now. Records any failure on the task (shown in the
    UI with a retry button) and returns whether it succeeded."""
    task = taskboard.store.get(task_id)
    if task is None or not task.use_worktree or not (task.worktree_path or task.branch):
        return True
    repo = ProjectStore().get_path(task.project)
    try:
        if repo is None:
            raise WorktreeError(f"project {task.project!r} isn't registered any more")
        kept = cleanup(task, repo, keep_remote=_pr_still_open(task, repo))
    except WorktreeError as exc:
        logger.warning("Task #%s: cleanup failed: %s", task_id, exc)
        taskboard.store.update_fields(task_id, cleanup_error=str(exc))
        live.task_changed(task_id)
        return False
    for note in kept:
        taskboard.store.add_comment(task_id, "system", note)
    taskboard.store.update_fields(task_id, worktree_path=None, cleanup_error=None)
    live.task_changed(task_id)
    return True


def _pr_still_open(task, repo: str) -> bool:
    """Deleting an open PR's head branch would close the PR, so a task marked
    Done by hand keeps it. When GitHub can't be asked, assume it's open."""
    if not task.pr_url:
        return False
    try:
        state = pr_watcher.pr_state(task.pr_url, repo)
    except pr_watcher.GhError:
        return True
    taskboard.store.update_fields(task.id, pr_state=state)
    return state in ("OPEN", "DRAFT")


def on_transition(event: TransitionEvent) -> None:
    """Clean up in the background when a task reaches Done; git can be slow."""
    if event.to_status == DONE:
        threading.Thread(
            target=run, args=(event.task.id,), name=f"task-cleanup-{event.task.id}", daemon=True
        ).start()
