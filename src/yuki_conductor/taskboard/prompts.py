"""Prompts for a task turn.

Three shapes: the first turn (the whole task), a follow-up (only what the
agent hasn't seen), and a fresh session standing in for one that's gone (the
whole task plus its timeline, since none of it is in the new session).
"""

from datetime import datetime

from yuki_conductor.task_store import Task, TaskComment


def _attachment_tags(paths: list[str]) -> str:
    return "".join(f"<attachment>{p}</attachment>\n" for p in paths)


def render_comment(comment: TaskComment) -> str:
    when = datetime.fromtimestamp(comment.created_at).strftime("%Y-%m-%d %H:%M")
    who = comment.author
    if comment.author == "github":
        who = f"github · {comment.external_author or 'unknown'}"
    header = f"--- comment #{comment.id} · {who} · {when} ---"
    lines = [header]
    if comment.external_url:
        lines.append(f"({comment.external_url})")
    if comment.quote_comment_id is not None:
        lines.append(f"(replying to comment #{comment.quote_comment_id})")
    lines.append(_attachment_tags(comment.attachments) + comment.body)
    return "\n".join(lines)


def _task_header(task: Task, cwd: str) -> str:
    where = f"Project: {task.project}, working directory: {cwd}"
    if task.branch:
        where += f" (a git worktree on branch `{task.branch}`)"
    return (
        f"# Task #{task.id}: {task.title}\n\n"
        f"{where}\n\n"
        f"{_attachment_tags(task.attachments)}{task.body}".rstrip()
    )


def first_turn(task: Task, comments: list[TaskComment], cwd: str) -> str:
    """The whole task, plus anything the user added before pickup."""
    parts = [_task_header(task, cwd)]
    extra = [c for c in comments if c.author != "system"]
    if extra:
        parts.append("## Comments so far\n\n" + "\n\n".join(render_comment(c) for c in extra))
    return "\n\n".join(parts)


def follow_up(comments: list[TaskComment]) -> str:
    """Only the comments that arrived since the agent's last turn.

    With none (a turn interrupted by a daemon restart, or a task re-queued
    without a comment), the agent is told to carry on instead.
    """
    shown = [c for c in comments if c.author != "system"]
    if not shown:
        return (
            "Continue working on your task. There are no new comments: your "
            "previous turn was interrupted, or the task was put back in the queue. "
            "Run `yuki-conductor task show` if you need to re-read it."
        )
    body = "\n\n".join(render_comment(c) for c in shown)
    return f"New comments on your task since your last turn:\n\n{body}"


def full_context(task: Task, comments: list[TaskComment], cwd: str) -> str:
    """For a fresh session replacing one that's gone: everything, system
    comments included, since they record what happened when."""
    timeline = "\n\n".join(render_comment(c) for c in comments)
    preface = (
        "You worked on this task before, but that session is gone. Here is the "
        "task and its full timeline; continue from where it stands, starting "
        "with the most recent comments."
    )
    return f"{preface}\n\n{_task_header(task, cwd)}\n\n## Timeline\n\n{timeline}"
