"""Tell the user when a task needs them.

Only transitions flagged `notify` (Needs Input, Ready for Review) ping the
user: as a browser push, and in Slack when that channel is enabled. Both are
best effort.
"""

import logging
import threading

from yuki_conductor import taskboard, web_push
from yuki_conductor.task_store import READY_FOR_REVIEW, STATUS_LABELS, Task
from yuki_conductor.taskboard.transitions import TransitionEvent

logger = logging.getLogger(__name__)

# Chat platforms by name, as `start_taskboard` received them.
_platforms: dict = {}


def set_platforms(platforms_by_name: dict) -> None:
    _platforms.clear()
    _platforms.update(platforms_by_name)


def _latest_agent_comment(task: Task) -> str:
    for comment in reversed(taskboard.store.comments(task.id)):
        if comment.author == "agent" and comment.body.strip():
            return comment.body
    return ""


def message(task: Task, reason: str) -> tuple[str, str]:
    """`(title, body)` for a task's notification."""
    title = f"Task #{task.id} · {STATUS_LABELS[task.status]}: {task.title}"
    body = _latest_agent_comment(task) or reason
    if task.status == READY_FOR_REVIEW and task.pr_url:
        body = f"{task.pr_url}\n{body}"
    return title, body


def on_transition(event: TransitionEvent) -> None:
    if not event.notify:
        return
    title, body = message(event.task, event.reason)
    # Slack is a network call, and transitions can run on a request thread.
    threading.Thread(
        target=_send, args=(event.task.id, title, body), name="task-notify", daemon=True
    ).start()


def _send(task_id: int, title: str, body: str) -> None:
    web_push.notify(web_push.task_target(task_id), title, body)
    slack = _platforms.get("slack")
    if slack is not None:
        try:
            slack.send_notification(f"*{title}*\n{body}")
        except Exception:
            logger.warning("Slack task notification failed", exc_info=True)
