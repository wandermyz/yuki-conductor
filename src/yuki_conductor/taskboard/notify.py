"""Tell the user when a task needs them.

Only transitions flagged `notify` (Needs Input, Ready for Review) and comments
posted with `notify` (an agent's `yuki-conductor send` mid-turn) ping the
user, as a browser push only: Slack is kept for replies to Slack threads, cron
results and restarts. The push is best effort and says only what happened to
which task, never the content: the user opens the task to read it.
"""

import threading

from yuki_conductor import web_push
from yuki_conductor.task_store import STATUS_LABELS, Task, TaskComment
from yuki_conductor.taskboard.transitions import TransitionEvent


def message(task: Task) -> tuple[str, str]:
    """`(title, body)` for a task's move into the status it now has."""
    return f"Task #{task.id}: {task.title}", f"Moved to {STATUS_LABELS[task.status]}"


def on_transition(event: TransitionEvent) -> None:
    if not event.notify:
        return
    title, body = message(event.task)
    _send_in_background(event.task.id, title, body)


def on_comment(task: Task, comment: TaskComment) -> None:
    _send_in_background(task.id, f"Task #{task.id}: {task.title}", f"{comment.author.capitalize()} posted a comment")


def _send_in_background(task_id: int, title: str, body: str) -> None:
    # Web push is a network call per subscription, and this can run on a
    # request thread.
    threading.Thread(
        target=_send, args=(task_id, title, body), name="task-notify", daemon=True
    ).start()


def _send(task_id: int, title: str, body: str) -> None:
    web_push.notify(web_push.task_target(task_id), title, body)
