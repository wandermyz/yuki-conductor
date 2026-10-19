"""What a user or agent can do to the board.

The HTTP routes (`taskboard.routes`) and through them the `yuki-conductor
task` CLI are thin wrappers over these functions. Errors are raised as
`BoardError` with the HTTP status they map to.
"""

import re

from yuki_conductor import taskboard
from yuki_conductor.harness import HARNESSES, default_harness
from yuki_conductor.store import ProjectStore
from yuki_conductor.task_store import (
    DONE,
    IN_PROGRESS,
    NEEDS_INPUT,
    READY_FOR_REVIEW,
    TODO,
    Task,
    TaskComment,
)
from yuki_conductor.taskboard import live, runner
from yuki_conductor.taskboard.transitions import InvalidTransition, transition

PR_URL_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+/?$")

# Who may post through the API. GitHub and system comments are written by
# the board itself.
API_AUTHORS = ("user", "agent")


class BoardError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def get(task_id: int) -> Task:
    task = taskboard.store.get(task_id)
    if task is None:
        raise BoardError(404, f"Task #{task_id} not found")
    return task


def _check_project(name: str) -> None:
    if ProjectStore().get_path(name) is None:
        raise BoardError(400, f"Project {name!r} isn't registered (see the Projects tab)")


def _check_harness(name: str) -> None:
    if name not in HARNESSES:
        raise BoardError(400, f"Harness {name!r} isn't enabled on this machine")


def _check_title(title: str) -> str:
    title = title.strip()
    if not title:
        raise BoardError(400, "A task needs a title")
    return title


def create(
    *,
    title: str,
    project: str,
    harness: str | None = None,
    use_worktree: bool = True,
    body: str = "",
    attachments: list[str] | None = None,
    parent_task_id: int | None = None,
) -> Task:
    harness = harness or default_harness()
    title = _check_title(title)
    _check_project(project)
    _check_harness(harness)
    if parent_task_id is not None:
        get(parent_task_id)
    task = taskboard.store.create(
        title=title,
        project=project,
        harness=harness,
        use_worktree=use_worktree,
        body=body,
        attachments=attachments,
        parent_task_id=parent_task_id,
    )
    _changed(task.id)
    return task


def edit(task_id: int, **fields) -> Task:
    """Change a task nobody has picked up yet."""
    task = get(task_id)
    if not task.editable:
        raise BoardError(409, "Only a To Do task that was never picked up can be edited")
    if "title" in fields:
        fields["title"] = _check_title(fields["title"])
    if "project" in fields:
        _check_project(fields["project"])
    if "harness" in fields:
        _check_harness(fields["harness"])
    if fields and not taskboard.store.edit_if_untouched(task_id, **fields):
        raise BoardError(409, "The task was picked up before the edit could be saved")
    _changed(task_id)
    return get(task_id)


def add_comment(
    task_id: int,
    body: str,
    *,
    author: str = "user",
    quote_comment_id: int | None = None,
    attachments: list[str] | None = None,
) -> TaskComment:
    """Post a comment. A user comment sends a waiting task back to the queue.

    Needs Input and Ready for Review go straight back to To Do. In Progress
    keeps it for when the turn ends; To Do delivers it at pickup. Done tasks
    stay done.
    """
    if author not in API_AUTHORS:
        raise BoardError(400, f"Unknown author {author!r}")
    if not body.strip() and not attachments:
        raise BoardError(400, "Empty comment")
    get(task_id)
    if quote_comment_id is not None and not any(
        c.id == quote_comment_id for c in taskboard.store.comments(task_id)
    ):
        raise BoardError(400, f"Comment #{quote_comment_id} isn't on task #{task_id}")
    comment = taskboard.store.add_comment(
        task_id, author, body, quote_comment_id=quote_comment_id, attachments=attachments
    )
    # Read the status only after the insert: a turn ending concurrently
    # re-checks for comments after its own move, so between the two of us
    # one always sees the other and the comment is never stranded.
    task = get(task_id)
    if author == "user" and task.status in (NEEDS_INPUT, READY_FOR_REVIEW):
        try:
            transition(task_id, TODO, reason="new comment")
        except InvalidTransition:
            pass  # it moved on meanwhile; the comment is still undelivered
    _changed(task_id)
    return comment


def ask(task_id: int, question: str) -> TaskComment:
    """The agent needs the user: post the question; the turn's end moves the
    task to Needs Input."""
    if not question.strip():
        raise BoardError(400, "Empty question")
    if get(task_id).status != IN_PROGRESS:
        raise BoardError(409, "Only a running task can ask; comment instead")
    comment = taskboard.store.add_comment(task_id, "agent", question)
    taskboard.store.update_fields(task_id, asked_input=True)
    _changed(task_id)
    return comment


def set_pr(task_id: int, url: str) -> Task:
    url = url.strip()
    if not PR_URL_RE.match(url):
        raise BoardError(400, f"Not a GitHub pull request URL: {url!r}")
    get(task_id)
    task = taskboard.store.update_fields(task_id, pr_url=url.rstrip("/"))
    _changed(task_id)
    return task


def cancel(task_id: int) -> Task:
    """Take a task off the queue, or stop its running turn. Either way it
    ends up in Needs Input, waiting for the user's next comment."""
    task = get(task_id)
    if task.status == TODO:
        try:
            return transition(task_id, NEEDS_INPUT, reason="cancelled")
        except InvalidTransition:
            task = get(task_id)  # picked up meanwhile: stop the turn instead
    if task.status == IN_PROGRESS:
        # If the turn hasn't reached its agent run yet, the runner sees the
        # request before starting it.
        runner.request_cancel(task_id)
        return task
    raise BoardError(409, "Only a To Do or In Progress task can be cancelled")


def mark_done(task_id: int) -> Task:
    task = get(task_id)
    if task.status not in (NEEDS_INPUT, READY_FOR_REVIEW):
        raise BoardError(409, "Only a Needs Input or Ready for Review task can be marked done")
    try:
        return transition(task_id, DONE, reason="marked done")
    except InvalidTransition as exc:
        raise BoardError(409, str(exc)) from exc


def retry_cleanup(task_id: int) -> Task:
    from yuki_conductor.taskboard import cleanup

    if get(task_id).status != DONE:
        raise BoardError(409, "Only a Done task is cleaned up")
    if not cleanup.run(task_id):
        raise BoardError(500, get(task_id).cleanup_error or "Cleanup failed")
    return get(task_id)


def reorder(ids: list[int]) -> None:
    try:
        taskboard.store.reorder(ids)
    except ValueError as exc:
        raise BoardError(400, str(exc)) from exc
    for task_id in ids:
        live.task_changed(task_id)


def move_to_top(task_id: int) -> Task:
    if get(task_id).status != TODO:
        raise BoardError(409, "Only To Do tasks can be reordered")
    taskboard.store.move_to_top(task_id)
    _changed(task_id)
    return get(task_id)


def board_status() -> dict:
    harvester = taskboard.active_harvester
    running = harvester.running() if harvester else taskboard.store.running_counts()
    from yuki_conductor import config

    return {
        "paused": taskboard.board_state.paused(),
        "running": {h: running.get(h, 0) for h in HARNESSES},
        "limits": {h: config.taskboard_limit(h) for h in HARNESSES},
    }


def set_paused(paused: bool) -> dict:
    taskboard.set_paused(paused)
    return board_status()


def attachment_paths(task_id: int) -> set[str]:
    """Every file the task or its comments reference: what may be served."""
    task = get(task_id)
    paths = set(task.attachments)
    for comment in taskboard.store.comments(task_id):
        paths.update(comment.attachments)
    return paths


def _changed(task_id: int) -> None:
    live.task_changed(task_id)
