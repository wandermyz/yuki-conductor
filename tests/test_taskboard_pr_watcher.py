"""The PR watcher, with `gh` mocked."""

import pytest

from yuki_conductor import taskboard
from yuki_conductor.skills import GITHUB_AGENT_MARKER
from yuki_conductor.task_store import DONE, NEEDS_INPUT, READY_FOR_REVIEW, TODO
from yuki_conductor.taskboard import pr_watcher
from yuki_conductor.taskboard.transitions import claim, transition

PR = "https://github.com/o/r/pull/7"
LATER = "2099-01-01T00:00:00Z"
EARLIER = "2000-01-01T00:00:00Z"


class FakeProjects:
    def get_path(self, name):
        return "/repo"


@pytest.fixture
def gh(monkeypatch):
    """`gh.view` / `gh.inline` are what `gh pr view` / `gh api` return."""

    class Gh:
        view = {"state": "OPEN", "comments": [], "reviews": []}
        inline: list = []
        calls: list = []

    def fake(args, cwd):
        Gh.calls.append(args)
        if isinstance(Gh.view, Exception):
            raise Gh.view
        return Gh.view if args[0] == "pr" else Gh.inline

    Gh.calls = []
    monkeypatch.setattr(pr_watcher, "_gh", fake)
    monkeypatch.setattr(pr_watcher, "ProjectStore", FakeProjects)
    return Gh


def _in_review():
    task = taskboard.store.create(title="t", project="p", harness="claude-code", use_worktree=False)
    claim(task.id)
    taskboard.store.update_fields(task.id, pr_url=PR)
    transition(task.id, READY_FOR_REVIEW, reason="PR opened")
    return task


def _comment(body, login="me", when=LATER):
    return {"author": {"login": login}, "body": body, "createdAt": when, "url": f"{PR}#c"}


def _status(task):
    return taskboard.store.get(task.id).status


def test_new_comment_requeues_with_it_on_the_timeline(gh):
    task = _in_review()
    gh.view = {"state": "OPEN", "comments": [_comment("rename foo")], "reviews": []}
    gh.inline = [{"user": {"login": "me"}, "body": "nit", "path": "a.py", "line": 3,
                  "created_at": LATER, "html_url": f"{PR}#r1"}]
    pr_watcher.check_all()
    assert _status(task) == TODO
    imported = [c for c in taskboard.store.comments(task.id) if c.author == "github"]
    assert [c.body for c in imported] == ["rename foo", "`a.py:3`\n\nnit"]
    assert imported[0].external_author == "me" and not imported[0].delivered
    assert ["api", "repos/o/r/pulls/7/comments", "--paginate"] in gh.calls


def test_agent_bot_old_and_empty_comments_are_ignored(gh):
    task = _in_review()
    gh.view = {
        "state": "OPEN",
        "comments": [
            _comment(f"Done.\n\n_🤖 Posted by yuki-conductor agent (task #1)_\n{GITHUB_AGENT_MARKER}"),
            _comment("Deploy preview ready", login="vercel[bot]"),
            _comment("from before the review", when=EARLIER),
        ],
        "reviews": [{"author": {"login": "me"}, "body": "", "submittedAt": LATER, "state": "APPROVED"}],
    }
    pr_watcher.check_all()
    assert _status(task) == READY_FOR_REVIEW
    assert not any(c.author == "github" for c in taskboard.store.comments(task.id))
    assert taskboard.store.get(task.id).pr_checked_at is not None


def test_comments_are_imported_once(gh):
    task = _in_review()
    gh.view = {"state": "OPEN", "comments": [_comment("fix it", when="2030-01-01T00:00:00Z")], "reviews": []}
    pr_watcher.check_all()
    # The agent handles it and goes back to review; the same comment is old now.
    claim(task.id)
    transition(task.id, READY_FOR_REVIEW, reason="turn finished")
    taskboard.store.update_fields(task.id, pr_checked_at=pr_watcher._ts("2031-01-01T00:00:00Z"))
    pr_watcher.check_all()
    assert _status(task) == READY_FOR_REVIEW
    assert sum(c.author == "github" for c in taskboard.store.comments(task.id)) == 1


@pytest.mark.parametrize("state, reason", [("MERGED", "PR merged"), ("CLOSED", "PR closed without merging")])
def test_merged_or_closed_is_done(gh, state, reason):
    task = _in_review()
    gh.view = {"state": state, "comments": [_comment("late remark")], "reviews": []}
    pr_watcher.check_all()
    assert _status(task) == DONE
    assert taskboard.store.comments(task.id)[-1].body.endswith(reason)


def test_gh_failure_leaves_the_task_alone(gh):
    task = _in_review()
    gh.view = pr_watcher.GhError("HTTP 502")
    pr_watcher.check_all()
    assert _status(task) == READY_FOR_REVIEW
    assert taskboard.store.get(task.id).pr_checked_at is None


def test_only_review_tasks_with_a_pr_are_polled(gh):
    task = taskboard.store.create(title="t", project="p", harness="claude-code", use_worktree=False)
    transition(task.id, NEEDS_INPUT, reason="x")
    taskboard.store.update_fields(task.id, pr_url=PR)
    no_pr = taskboard.store.create(title="u", project="p", harness="claude-code", use_worktree=False)
    claim(no_pr.id)
    transition(no_pr.id, READY_FOR_REVIEW, reason="turn finished")
    pr_watcher.check_all()
    assert gh.calls == []
