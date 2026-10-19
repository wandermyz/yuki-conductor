"""Follow the pull requests of tasks in Ready for Review.

Every `POLL_SECONDS`, each such task's PR is checked with `gh`:

- merged or closed → Done (which cleans up its worktree);
- new comments from anyone but the agent → imported onto the timeline as
  GitHub comments, and the task goes back to To Do so the agent answers.

The agent may post as the user's own GitHub account, so its comments are
told apart by the marker its footer carries (`skills.GITHUB_AGENT_MARKER`),
not by author. Bot accounts (CI, preview deploys) are ignored too.
"""

import json
import logging
import re
import subprocess
import threading
import time
from datetime import datetime

from yuki_conductor import taskboard
from yuki_conductor.skills import GITHUB_AGENT_MARKER
from yuki_conductor.store import ProjectStore
from yuki_conductor.task_store import DONE, READY_FOR_REVIEW, TODO, Task
from yuki_conductor.taskboard.transitions import InvalidTransition, transition

logger = logging.getLogger(__name__)

POLL_SECONDS = 300

_PR_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")


class GhError(Exception):
    pass


def _gh(args: list[str], cwd: str | None) -> object:
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, cwd=cwd, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(str(exc)) from exc
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    return json.loads(proc.stdout or "null")


def _ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _is_bot(login: str, flagged: bool = False) -> bool:
    return flagged or login.endswith("[bot]")


def fetch(pr_url: str, cwd: str | None) -> tuple[str, list[dict]]:
    """The PR's state and every comment on it, as
    `{author, body, created_at, url, bot}` dicts."""
    view = _gh(["pr", "view", pr_url, "--json", "state,comments,reviews"], cwd)
    comments = []
    for c in view.get("comments") or []:
        author = c.get("author") or {}
        comments.append({
            "author": author.get("login", ""),
            "bot": _is_bot(author.get("login", ""), bool(author.get("is_bot"))),
            "body": c.get("body") or "",
            "created_at": _ts(c.get("createdAt")),
            "url": c.get("url"),
        })
    for r in view.get("reviews") or []:
        author = r.get("author") or {}
        comments.append({
            "author": author.get("login", ""),
            "bot": _is_bot(author.get("login", "")),
            "body": r.get("body") or "",
            "created_at": _ts(r.get("submittedAt")),
            "url": pr_url,
        })
    match = _PR_RE.search(pr_url)
    if match:
        owner, repo, number = match.groups()
        inline = _gh(
            ["api", f"repos/{owner}/{repo}/pulls/{number}/comments", "--paginate"], cwd
        )
        for c in inline or []:
            user = c.get("user") or {}
            line = c.get("line") or c.get("original_line")
            where = f"`{c.get('path')}:{line}`\n\n" if c.get("path") else ""
            comments.append({
                "author": user.get("login", ""),
                "bot": _is_bot(user.get("login", ""), user.get("type") == "Bot"),
                "body": where + (c.get("body") or ""),
                "created_at": _ts(c.get("created_at")),
                "url": c.get("html_url"),
            })
    comments.sort(key=lambda c: c["created_at"])
    return view.get("state", ""), comments


def _is_new(comment: dict, since: float) -> bool:
    return (
        comment["created_at"] > since
        and comment["body"].strip() != ""
        and not comment["bot"]
        and GITHUB_AGENT_MARKER not in comment["body"]
    )


def _since(task: Task) -> float:
    """Comments after this are new: the last poll, or else when the task last
    went to review, since the agent's own turn already saw what came before."""
    if task.pr_checked_at:
        return task.pr_checked_at
    for comment in reversed(taskboard.store.comments(task.id)):
        if comment.author == "system" and comment.body.endswith(("PR opened", "turn finished")):
            return comment.created_at
    return task.updated_at


def check(task: Task) -> None:
    """Poll one Ready for Review task's PR and act on it."""
    polled_at = time.time()
    state, comments = fetch(task.pr_url, ProjectStore().get_path(task.project))
    new = [c for c in comments if _is_new(c, _since(task))]
    for c in new:
        taskboard.store.add_comment(
            task.id, "github", c["body"], external_url=c["url"], external_author=c["author"]
        )
    taskboard.store.update_fields(task.id, pr_checked_at=polled_at)
    try:
        if state == "MERGED":
            transition(task.id, DONE, reason="PR merged")
        elif state == "CLOSED":
            transition(task.id, DONE, reason="PR closed without merging")
        elif new:
            transition(task.id, TODO, reason="new PR comments")
    except InvalidTransition:
        pass  # the user moved it meanwhile; any imported comments stay pending


def check_all() -> None:
    for task in taskboard.store.list_status(READY_FOR_REVIEW):
        if not task.pr_url:
            continue
        try:
            check(task)
        except GhError as exc:
            logger.warning("Task #%s: couldn't check %s: %s", task.id, task.pr_url, exc)
        except Exception:
            logger.exception("Task #%s: PR check failed", task.id)


def loop(stop: threading.Event) -> None:
    while not stop.wait(POLL_SECONDS):
        check_all()
