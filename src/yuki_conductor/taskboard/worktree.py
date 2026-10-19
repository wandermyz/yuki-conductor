"""Git worktrees for tasks that ask for one.

A worktree task works on its own branch, `task/<id>-<slug>`, checked out at
`<repo>/.claude/worktrees/task-<id>` from the remote's default branch.
"""

import logging
import re
import subprocess
import threading
from pathlib import Path

from yuki_conductor.task_store import Task

logger = logging.getLogger(__name__)

WORKTREES_DIR = Path(".claude") / "worktrees"

_repo_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class WorktreeError(Exception):
    pass


def _git(repo: str, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
    except subprocess.CalledProcessError as exc:
        raise WorktreeError(
            f"`git {' '.join(args)}` failed: {(exc.stderr or exc.stdout).strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"`git {' '.join(args)}` timed out") from exc
    return proc.stdout.strip()


def slugify(title: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:limit].rstrip("-") or "task"


def branch_name(task: Task) -> str:
    return f"task/{task.id}-{slugify(task.title)}"


def worktree_path(repo: str, task_id: int) -> Path:
    return Path(repo) / WORKTREES_DIR / f"task-{task_id}"


def default_branch(repo: str) -> str:
    """The remote's default branch.

    `origin/HEAD` names it in most clones; without it, the first of `main` /
    `master` the remote has, else `main`.
    """
    try:
        ref = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        return ref.removeprefix("origin/")
    except WorktreeError:
        pass
    for name in ("main", "master"):
        if _ref_exists(repo, f"refs/remotes/origin/{name}"):
            return name
    return "main"


def _ensure_ignored(repo: str) -> None:
    """Keep the worktrees out of the main checkout's `git status`, without
    editing the repo's own `.gitignore`."""
    check = subprocess.run(
        ["git", "-C", repo, "check-ignore", "-q", str(WORKTREES_DIR / "x")],
        capture_output=True,
    )
    if check.returncode == 0:
        return
    exclude = Path(_git(repo, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = Path(repo) / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    sep = "" if not existing or existing.endswith("\n") else "\n"
    exclude.write_text(f"{existing}{sep}{WORKTREES_DIR.as_posix()}/\n")


def _ref_exists(repo: str, ref: str) -> bool:
    return subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
    ).returncode == 0


def _repo_lock(repo: str) -> threading.Lock:
    """One git writer per repo: parallel `fetch`es fight over ref locks."""
    key = str(Path(repo).resolve())
    with _locks_guard:
        return _repo_locks.setdefault(key, threading.Lock())


def ensure_worktree(task: Task, repo: str) -> tuple[str, str]:
    """Return `(path, branch)` of the task's worktree, creating it if needed.

    Raises WorktreeError with git's message when it can't be created.
    """
    if task.worktree_path and Path(task.worktree_path).is_dir() and task.branch:
        return task.worktree_path, task.branch
    with _repo_lock(repo):
        return _create(task, repo)


def _create(task: Task, repo: str) -> tuple[str, str]:
    path = worktree_path(repo, task.id)
    branch = task.branch or branch_name(task)
    _git(repo, "fetch", "origin")
    base = default_branch(repo)
    _ensure_ignored(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _ref_exists(repo, f"refs/heads/{branch}"):
        # A restart between `worktree add` and saving it, or a worktree
        # removed by hand: the branch holds the work, so check it out again.
        _git(repo, "worktree", "prune")
        _git(repo, "worktree", "add", str(path), branch)
    else:
        _git(repo, "worktree", "add", str(path), "-b", branch, f"origin/{base}")
    logger.info("Task #%s: worktree %s on %s from origin/%s", task.id, path, branch, base)
    return str(path), branch


def cleanup(task: Task, repo: str, *, keep_remote: bool = False) -> list[str]:
    """Remove a finished task's worktree, local branch and remote branch.

    Each step tolerates the thing already being gone, and only `task/`
    branches are ever deleted. Work that exists nowhere else is kept: a local
    branch with commits no remote has, and (with `keep_remote`, for a PR that
    is still open) the remote branch. Returns notes on what was kept. Raises
    WorktreeError on any other failure.
    """
    kept: list[str] = []
    with _repo_lock(repo):
        if task.worktree_path and Path(task.worktree_path).exists():
            _git(repo, "worktree", "remove", "--force", task.worktree_path)
        _git(repo, "worktree", "prune")
        branch = task.branch
        if not branch or not branch.startswith("task/"):
            return kept
        if _ref_exists(repo, f"refs/heads/{branch}"):
            # A task with a PR pushed its work; after a merge GitHub may have
            # deleted the remote branch, so "on no remote" proves nothing then.
            if not task.pr_url and _git(
                repo, "rev-list", "--max-count=1", branch, "--not", "--remotes"
            ):
                kept.append(f"Kept local branch `{branch}`: it has commits that were never pushed.")
            else:
                _git(repo, "branch", "-D", branch)
        if keep_remote:
            kept.append(f"Kept remote branch `{branch}`: its pull request is still open.")
        else:
            try:
                _git(repo, "push", "origin", "--delete", branch)
            except WorktreeError as exc:
                # GitHub deletes merged branches itself when the repo says so.
                if "remote ref does not exist" not in str(exc):
                    raise
    logger.info("Task #%s: cleaned up %s", task.id, branch)
    return kept
