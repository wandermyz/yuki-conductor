"""Worktree creation against a real temp repo with a bare origin."""

import shutil
import subprocess
from pathlib import Path

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
    lost_worktree = Path(path).resolve()
    assert lost_worktree.is_relative_to(repo.resolve())
    shutil.rmtree(lost_worktree)
    task = taskboard.store.update_fields(task.id, worktree_path=path, branch=branch)
    path2, _ = ensure_worktree(task, str(repo))
    assert git(path2, "rev-parse", "HEAD") == work


def test_not_a_repo(tmp_path):
    with pytest.raises(WorktreeError, match="fetch"):
        ensure_worktree(_task(), str(tmp_path))


def test_default_branch_falls_back_to_main(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    assert worktree.default_branch(str(tmp_path)) == "main"


def test_default_branch_without_origin_head_finds_master(repo):
    git(repo, "remote", "set-head", "origin", "--delete")
    git(repo, "update-ref", "refs/remotes/origin/master", "origin/trunk")
    assert worktree.default_branch(str(repo)) == "master"


def test_parallel_worktrees_on_one_repo(repo):
    import threading

    tasks = [_task(f"t{i}") for i in range(4)]
    errors = []

    def make(task):
        try:
            ensure_worktree(task, str(repo))
        except WorktreeError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=make, args=(t,)) for t in tasks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------


def _pushed_task(repo):
    task = _task()
    path, branch = ensure_worktree(task, str(repo))
    git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "work")
    git(path, "push", "origin", branch)
    return taskboard.store.update_fields(task.id, worktree_path=path, branch=branch)


def test_cleanup_removes_worktree_and_both_branches(repo):
    task = _pushed_task(repo)
    worktree.cleanup(task, str(repo))
    assert not (repo / ".claude" / "worktrees" / f"task-{task.id}").exists()
    assert git(repo, "branch", "--list", task.branch) == ""
    assert git(repo, "ls-remote", "--heads", "origin", task.branch) == ""


def test_cleanup_tolerates_everything_already_gone(repo):
    task = _pushed_task(repo)
    task = taskboard.store.update_fields(task.id, pr_url="https://github.com/o/r/pull/1")
    git(repo, "push", "origin", "--delete", task.branch)  # GitHub deleted it on merge
    worktree.cleanup(task, str(repo))
    worktree.cleanup(task, str(repo))  # and again: idempotent
    assert git(repo, "branch", "--list", task.branch) == ""


def test_cleanup_tolerates_a_worktree_git_no_longer_knows(repo):
    task = _pushed_task(repo)
    path = repo / ".claude" / "worktrees" / f"task-{task.id}"
    git(repo, "worktree", "remove", "--force", str(path))
    path.mkdir()  # an empty leftover directory, not a working tree
    worktree.cleanup(task, str(repo))
    assert not path.exists()
    worktree.cleanup(task, str(repo))  # and with nothing on disk at all


def test_cleanup_refuses_to_drop_an_unknown_nonempty_directory(repo):
    task = _pushed_task(repo)
    path = repo / ".claude" / "worktrees" / f"task-{task.id}"
    git(repo, "worktree", "remove", "--force", str(path))
    path.mkdir()
    (path / "stray.txt").write_text("x")
    with pytest.raises(WorktreeError, match="not a working tree"):
        worktree.cleanup(task, str(repo))
    assert (path / "stray.txt").exists()


def test_cleanup_keeps_unpushed_work(repo):
    task = _task()
    path, branch = ensure_worktree(task, str(repo))
    git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "local only")
    task = taskboard.store.update_fields(task.id, worktree_path=path, branch=branch)
    kept = worktree.cleanup(task, str(repo))
    assert "never pushed" in kept[0]
    assert git(repo, "branch", "--list", branch) != ""


def test_cleanup_keeps_the_branch_of_an_open_pr(repo):
    task = _pushed_task(repo)
    kept = worktree.cleanup(task, str(repo), keep_remote=True)
    assert "still open" in kept[0]
    assert git(repo, "ls-remote", "--heads", "origin", task.branch) != ""
    assert git(repo, "branch", "--list", task.branch) == ""


def test_cleanup_never_deletes_other_branches(repo):
    task = _pushed_task(repo)
    task = taskboard.store.update_fields(task.id, branch="trunk")
    worktree.cleanup(task, str(repo))
    assert git(repo, "ls-remote", "--heads", "origin", "trunk") != ""


def test_done_triggers_cleanup_and_records_failure(repo, monkeypatch):
    from yuki_conductor.task_store import NEEDS_INPUT
    from yuki_conductor.taskboard import cleanup
    from yuki_conductor.taskboard.transitions import transition

    class Projects:
        def get_path(self, name):
            return str(repo)

    monkeypatch.setattr(cleanup, "ProjectStore", Projects)
    monkeypatch.setattr(cleanup.pr_watcher, "pr_state", lambda url, cwd: "OPEN")
    task = _pushed_task(repo)
    transition(task.id, NEEDS_INPUT, reason="x")
    transition(task.id, "done", reason="marked done")
    assert cleanup.run(task.id)
    stored = taskboard.store.get(task.id)
    assert stored.worktree_path is None and stored.cleanup_error is None

    broken = _pushed_task(repo)
    git(repo, "remote", "set-url", "origin", "/nowhere")
    assert not cleanup.run(broken.id)
    assert "push" in taskboard.store.get(broken.id).cleanup_error


def test_marking_done_keeps_an_open_prs_branch(repo, monkeypatch):
    from yuki_conductor.taskboard import cleanup

    class Projects:
        def get_path(self, name):
            return str(repo)

    monkeypatch.setattr(cleanup, "ProjectStore", Projects)
    monkeypatch.setattr(cleanup.pr_watcher, "pr_state", lambda url, cwd: "OPEN")
    task = _pushed_task(repo)
    taskboard.store.update_fields(task.id, pr_url="https://github.com/o/r/pull/1")
    assert cleanup.run(task.id)
    assert git(repo, "ls-remote", "--heads", "origin", task.branch) != ""
    assert "still open" in taskboard.store.comments(task.id)[-1].body


def test_archive_removes_the_worktree_first(repo, monkeypatch):
    from yuki_conductor.task_store import NEEDS_INPUT
    from yuki_conductor.taskboard import actions, cleanup
    from yuki_conductor.taskboard.transitions import transition

    class Projects:
        def get_path(self, name):
            return str(repo)

    monkeypatch.setattr(cleanup, "ProjectStore", Projects)
    task = _pushed_task(repo)
    transition(task.id, NEEDS_INPUT, reason="x")
    actions.archive(task.id)
    stored = taskboard.store.get(task.id)
    assert stored.archived_at is not None and stored.worktree_path is None
    assert not Path(task.worktree_path).exists()
    assert git(repo, "ls-remote", "--heads", "origin", task.branch) == ""


@pytest.mark.parametrize("pr_state", ["MERGED", "OPEN"])
def test_reopen_starts_a_new_worktree(repo, monkeypatch, pr_state):
    from yuki_conductor.task_store import NEEDS_INPUT, TODO
    from yuki_conductor.taskboard import actions, cleanup
    from yuki_conductor.taskboard.transitions import transition

    class Projects:
        def get_path(self, name):
            return str(repo)

    monkeypatch.setattr(cleanup, "ProjectStore", Projects)
    monkeypatch.setattr(cleanup.pr_watcher, "pr_state", lambda url, cwd: pr_state)
    task = _pushed_task(repo)
    taskboard.store.update_fields(task.id, pr_url="https://github.com/o/r/pull/1", session_id="s1")
    transition(task.id, NEEDS_INPUT, reason="x")
    transition(task.id, "done", reason="marked done")
    with pytest.raises(actions.BoardError):
        actions.reopen(task.id + 1000)
    reopened = actions.reopen(task.id)
    assert reopened.status == TODO and reopened.session_id == "s1"
    assert reopened.worktree_path is None
    assert not Path(task.worktree_path).exists()
    if pr_state == "MERGED":
        assert reopened.pr_url is None and reopened.branch is None
        assert any("Previous pull request" in c.body for c in taskboard.store.comments(task.id))
    else:
        # Still open: back on the PR's branch, from what was pushed.
        assert reopened.pr_url and reopened.branch == task.branch
        path, branch = ensure_worktree(reopened, str(repo))
        assert git(path, "rev-parse", "HEAD") == git(repo, "rev-parse", f"origin/{branch}")
    with pytest.raises(actions.BoardError):
        actions.reopen(task.id)  # not Done any more
