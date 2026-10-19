"""Worktree creation against a real temp repo with a bare origin."""

import subprocess

import pytest

from yuki_conductor import taskboard
from yuki_conductor.taskboard import worktree
from yuki_conductor.taskboard.worktree import WorktreeError, ensure_worktree, slugify


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "trunk", str(origin)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "init")
    git(seed, "push", "origin", "HEAD:trunk")
    clone = tmp_path / "repo"
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    return clone


def _task(title="Fix the Login bug!"):
    return taskboard.store.create(title=title, project="p", harness="claude-code", use_worktree=True)


def test_slugify():
    assert slugify("Fix the Login bug!") == "fix-the-login-bug"
    assert slugify("???") == "task"
    assert len(slugify("x" * 100)) == 40


def test_creates_branch_from_remote_default(repo):
    task = _task()
    path, branch = ensure_worktree(task, str(repo))
    assert branch == f"task/{task.id}-fix-the-login-bug"
    assert path == str(repo / ".claude" / "worktrees" / f"task-{task.id}")
    assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == branch
    assert git(path, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/trunk")
    # The main checkout doesn't see the worktree as untracked files.
    assert git(repo, "status", "--porcelain") == ""
    assert ".claude/worktrees/" in (repo / ".git" / "info" / "exclude").read_text()


def test_existing_worktree_is_reused(repo):
    task = _task()
    path, branch = ensure_worktree(task, str(repo))
    task = taskboard.store.update_fields(task.id, worktree_path=path, branch=branch)
    assert ensure_worktree(task, str(repo)) == (path, branch)


def test_exclude_not_duplicated_when_already_ignored(repo):
    (repo / ".gitignore").write_text(".claude/worktrees/\n")
    ensure_worktree(_task(), str(repo))
    assert ".claude/worktrees" not in (repo / ".git" / "info" / "exclude").read_text()


def test_branch_survives_a_lost_worktree(repo):
    task = _task()
    path, branch = ensure_worktree(task, str(repo))
    git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "work")
    work = git(path, "rev-parse", "HEAD")
    subprocess.run(["rm", "-rf", path], check=True)
    task = taskboard.store.update_fields(task.id, worktree_path=path, branch=branch)
    path2, _ = ensure_worktree(task, str(repo))
    assert git(path2, "rev-parse", "HEAD") == work


def test_not_a_repo(tmp_path):
    with pytest.raises(WorktreeError, match="fetch"):
        ensure_worktree(_task(), str(tmp_path))


def test_default_branch_falls_back_to_main(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    assert worktree.default_branch(str(tmp_path)) == "main"
