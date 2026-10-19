"""Self-update: fetch origin, report what's new, rebase onto it.

The daemon always runs from a git checkout (`config.project_dir()`), so an
update is just `git fetch` + `git rebase` onto the branch's upstream, a
frontend rebuild if `web/` changed, and a restart. Dependencies need no step
of their own: the daemon starts under `uv run`, which syncs them.

Anything git can't do cleanly — local changes, no upstream, a conflicting
rebase — raises `UpdateError` with a message meant for the user. A failed
rebase is aborted, so the checkout is never left half-rebased.
"""

import subprocess
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from yuki_conductor.config import build_web_frontend, project_dir

# Every browser tab polls; one git writer at a time keeps fetches from racing
# each other and a rebase.
_lock = threading.Lock()

_LOG_FORMAT = "%H%x1f%h%x1f%an%x1f%aI%x1f%s"
MAX_COMMITS = 50


class UpdateError(RuntimeError):
    pass


@dataclass
class Commit:
    sha: str
    short: str
    author: str
    date: str
    subject: str


@dataclass
class UpdateStatus:
    branch: str
    upstream: str
    current: str
    latest: str
    behind: int
    ahead: int
    dirty: list[str] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _git(*args: str, cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"git {args[0]} failed: {exc}") from None
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        raise UpdateError(f"git {args[0]} failed: {output}")
    return result.stdout.strip()


def _dirty(repo: Path) -> list[str]:
    """Tracked files with uncommitted changes. Untracked files don't block a rebase."""
    return _git("diff", "--name-only", "HEAD", cwd=repo).splitlines()


def _status(repo: Path) -> UpdateStatus:
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    if branch == "HEAD":
        raise UpdateError("The checkout is on a detached HEAD; check out a branch first")
    try:
        upstream = _git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=repo)
    except UpdateError:
        raise UpdateError(f"Branch {branch!r} has no upstream to update from") from None
    ahead, behind = (
        int(n)
        for n in _git(
            "rev-list", "--left-right", "--count", "HEAD...@{upstream}", cwd=repo
        ).split()
    )
    log = _git(
        "log", f"--max-count={MAX_COMMITS}", f"--format={_LOG_FORMAT}",
        "HEAD..@{upstream}", cwd=repo,
    )
    commits = [Commit(*line.split("\x1f", 4)) for line in log.splitlines() if line]
    return UpdateStatus(
        branch=branch,
        upstream=upstream,
        current=_git("rev-parse", "HEAD", cwd=repo),
        latest=_git("rev-parse", "@{upstream}", cwd=repo),
        behind=behind,
        ahead=ahead,
        dirty=_dirty(repo),
        commits=commits,
    )


def check(repo: Path | None = None) -> UpdateStatus:
    """Fetch origin and report how far the checkout is behind its upstream."""
    repo = repo or project_dir()
    with _lock:
        _git("fetch", "--quiet", "origin", cwd=repo)
        return _status(repo)


def apply(repo: Path | None = None, *, build_frontend=None) -> UpdateStatus:
    """Fetch and rebase onto the upstream. Returns the status *before* rebasing.

    Rebuilds the frontend when the rebase touched `web/`. Restarting is the
    caller's job, and only worth doing if the returned status was behind.
    """
    repo = repo or project_dir()
    build_frontend = build_frontend or build_web_frontend
    with _lock:
        _git("fetch", "--quiet", "origin", cwd=repo)
        status = _status(repo)
        if status.dirty:
            raise UpdateError(
                "Local changes would be overwritten; commit or discard them first: "
                + ", ".join(status.dirty)
            )
        if status.behind == 0:
            return status
        try:
            _git("rebase", "@{upstream}", cwd=repo)
        except UpdateError as exc:
            subprocess.run(
                ["git", "rebase", "--abort"], cwd=repo, capture_output=True, check=False
            )
            raise UpdateError(f"Rebase failed and was aborted. {exc}") from None
        changed = _git("diff", "--name-only", status.current, "HEAD", "--", "web", cwd=repo)
    if changed and not build_frontend():
        raise UpdateError(
            "Updated, but the frontend build failed; see daemon.log. Not restarting."
        )
    return status
