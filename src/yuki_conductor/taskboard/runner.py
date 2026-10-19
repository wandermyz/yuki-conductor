"""Run one turn of a task, then decide where the task goes next.

`run_turn` is called on its own thread by the harvester, after the task has
been claimed (it's In Progress). Whatever happens, it leaves the task out of
In Progress: an unexpected error lands it in Needs Input rather than
stranding it.
"""

import logging
import re
import subprocess
import threading
from dataclasses import dataclass

from yuki_conductor import taskboard
from yuki_conductor.harness import get_harness
from yuki_conductor.harness.base import RunResult
from yuki_conductor.harness.process import cancel_process, run_agent
from yuki_conductor.messaging import split_response
from yuki_conductor.store import ProjectStore
from yuki_conductor.task_store import NEEDS_INPUT, READY_FOR_REVIEW, TODO, Task
from yuki_conductor.taskboard import live, prompts
from yuki_conductor.taskboard.transitions import InvalidTransition, transition
from yuki_conductor.taskboard.worktree import WorktreeError, ensure_worktree

logger = logging.getLogger(__name__)

PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")

# Tasks the user cancelled while In Progress. The run's own result can't tell
# a cancel from a crash, so the runner checks here instead.
_cancel_requested: set[int] = set()
_cancel_lock = threading.Lock()


def request_cancel(task_id: int) -> bool:
    """Stop a running task's turn. It then lands in Needs Input.

    Callers only ask for In Progress tasks. The flag also covers the gaps
    where no agent process exists yet (worktree setup, between retries).
    Returns whether a process was killed.
    """
    with _cancel_lock:
        _cancel_requested.add(task_id)
    return cancel_process(live.steps_key(task_id))


def clear_cancel(task_id: int) -> None:
    """Forget a stale request; called when a task is picked up."""
    _take_cancel(task_id)


def _cancel_pending(task_id: int) -> bool:
    with _cancel_lock:
        return task_id in _cancel_requested


def _take_cancel(task_id: int) -> bool:
    with _cancel_lock:
        if task_id in _cancel_requested:
            _cancel_requested.discard(task_id)
            return True
        return False


@dataclass
class Outcome:
    status: str
    reason: str
    notify: bool


def resolve_outcome(
    task: Task, result: RunResult, *, cancelled: bool, has_new_comments: bool
) -> Outcome:
    """Where a task goes when its turn ends. First match wins (see the PRD)."""
    if has_new_comments:
        return Outcome(TODO, "new comment during the turn", notify=False)
    if cancelled:
        return Outcome(NEEDS_INPUT, "cancelled", notify=False)
    if result.is_error:
        return Outcome(NEEDS_INPUT, "the run failed", notify=True)
    if task.asked_input:
        return Outcome(NEEDS_INPUT, "the agent asked for input", notify=True)
    if task.pr_url:
        return Outcome(READY_FOR_REVIEW, "PR opened", notify=True)
    return Outcome(READY_FOR_REVIEW, "turn finished", notify=True)


def _fail(task_id: int, message: str) -> None:
    """Explain a failure on the timeline and hand the task back to the user."""
    taskboard.store.add_comment(task_id, "system", message)
    transition(task_id, NEEDS_INPUT, reason="the run failed", notify=True)


def _working_dir(task: Task) -> str | None:
    repo = ProjectStore().get_path(task.project)
    if repo is None:
        _fail(task.id, f"Project `{task.project}` isn't registered any more.")
        return None
    if not task.use_worktree:
        return repo
    try:
        path, branch = ensure_worktree(task, repo)
    except WorktreeError as exc:
        _fail(task.id, f"Couldn't create the worktree:\n\n```\n{exc}\n```")
        return None
    taskboard.store.update_fields(task.id, worktree_path=path, branch=branch)
    return path


@dataclass
class _Turn:
    result: RunResult | None  # None: cancelled before the agent started
    delivered: list[int]  # comment ids the prompt carried


def _run(task: Task, cwd: str) -> _Turn:
    harness = get_harness(task.harness)
    comments = taskboard.store.comments(task.id)
    pending = [c for c in comments if not c.delivered]
    if task.session_id and harness.session_exists(task.session_id, cwd):
        prompt, session_id = prompts.follow_up(pending), task.session_id
    elif task.session_id:
        logger.info("Task #%s: session %s is gone, starting fresh", task.id, task.session_id)
        prompt, session_id = prompts.full_context(task, comments, cwd), None
    else:
        prompt, session_id = prompts.first_turn(task, comments, cwd), None
    delivered = [c.id for c in pending]
    # Shown as seen while the agent works on them; rolled back if the run
    # fails, so the next turn sends them again.
    taskboard.store.mark_delivered(delivered)

    def run(prompt: str, session_id: str | None) -> RunResult | None:
        # The cancel registry only knows a process once it's started; check
        # the board's own flag before each start so a cancel never waits for
        # a whole run.
        if _cancel_pending(task.id):
            return None
        return run_agent(
            harness,
            prompt,
            session_id=session_id,
            conversation_key=live.steps_key(task.id),
            cwd=cwd,
            task_id=task.id,
            on_event=lambda event: live.step(task.id, event),
        )

    result = run(prompt, session_id)
    if result is not None and result.session_missing:
        logger.info("Task #%s: session %s vanished mid-resume, retrying fresh", task.id, session_id)
        result = run(prompts.full_context(task, comments, cwd), None)
    return _Turn(result=result, delivered=delivered)


def _origin_repo(cwd: str) -> str | None:
    """`owner/repo` of the checkout's GitHub origin, if it has one."""
    try:
        url = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else None


def _own_pr_url(text: str, cwd: str) -> str | None:
    """The first PR URL in the reply that belongs to the project's own repo.

    A reply may cite unrelated PRs (an upstream fix, a related change); only
    one on the origin repo can be the task's. `task pr` is the reliable way.
    """
    repo = _origin_repo(cwd)
    if repo is None:
        return None
    for match in PR_URL_RE.finditer(text):
        if match.group(0).lower().startswith(f"https://github.com/{repo.lower()}/pull/"):
            return match.group(0)
    return None


def run_turn(task_id: int) -> None:
    """Run one turn of a claimed (In Progress) task and move it on."""
    try:
        _run_turn(task_id)
    except InvalidTransition:
        # Someone else moved the task out of In Progress meanwhile (a restart
        # recovery, a manual change); their move stands.
        logger.warning("Task #%s left In Progress before its turn finished", task_id, exc_info=True)
    except Exception as exc:
        logger.exception("Task #%s: turn crashed", task_id)
        try:
            _fail(task_id, f"The turn crashed: `{exc}`")
        except InvalidTransition:
            pass  # someone else already moved it out of In Progress
    finally:
        _take_cancel(task_id)
        live.turn_ended(task_id)
        live.task_changed(task_id)


def _new_comments(task_id: int, delivered: list[int]) -> bool:
    """User or GitHub comments the agent hasn't seen: anything undelivered
    that this turn's prompt didn't carry."""
    sent = set(delivered)
    return any(c.id not in sent for c in taskboard.store.undelivered(task_id))


def _run_turn(task_id: int) -> None:
    task = taskboard.store.update_fields(task_id, asked_input=False)
    cwd = _working_dir(task)
    if cwd is None:
        return

    task = taskboard.store.get(task_id)
    live.turn_started(task_id)
    turn = _run(task, cwd)
    result = turn.result

    # Only a turn that was actually stopped counts: a cancel that arrived
    # after the agent finished on its own is too late to matter.
    _take_cancel(task_id)
    cancelled = result is None or result.cancelled
    if result is None:
        result = RunResult(text="", session_id=None)
    # A failed run may never have reached the agent.
    if result.is_error or turn.result is None:
        taskboard.store.mark_delivered(turn.delivered, delivered=False)
    text, attachments = split_response(result.text)
    if result.session_id:
        taskboard.store.update_fields(task_id, session_id=result.session_id)
    if (text or attachments) and not cancelled:
        taskboard.store.add_comment(
            task_id, "agent", text, attachments=[str(a.local_path) for a in attachments]
        )
    task = taskboard.store.get(task_id)
    if not task.pr_url and not cancelled and (url := _own_pr_url(text, cwd)):
        task = taskboard.store.update_fields(task_id, pr_url=url, pr_state="OPEN")

    outcome = resolve_outcome(
        task,
        result,
        cancelled=cancelled,
        has_new_comments=_new_comments(task_id, turn.delivered),
    )
    if result.is_error and outcome.status == NEEDS_INPUT:
        taskboard.store.add_comment(
            task_id, "system", f"The run failed:\n\n```\n{result.text.strip()[:4000]}\n```"
        )
    transition(task_id, outcome.status, reason=outcome.reason, notify=outcome.notify)
    # A comment posted after the check above but before the move saw the task
    # still In Progress and left it for us; don't strand it.
    if outcome.status != TODO and _new_comments(task_id, turn.delivered):
        transition(task_id, TODO, reason="new comment")
