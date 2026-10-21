"""The PR watcher, with `gh api` mocked (GitHub REST shapes)."""

import threading

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
    """Set `gh.pr`, `gh.issue_comments`, `gh.reviews`, `gh.inline`."""

    class Gh:
        pr = {"state": "open", "merged": False}
        issue_comments: list = []
        reviews: list = []
        inline: list = []
        fail: Exception | None = None
        calls: list = []

    def fake(args, cwd):
        Gh.calls.append(args)
        if Gh.fail:
            raise Gh.fail
        path = args[1]
        if path.endswith("/issues/7/comments"):
            return Gh.issue_comments
        if path.endswith("/pulls/7/reviews"):
            return Gh.reviews
        if path.endswith("/pulls/7/comments"):
            return Gh.inline
        assert path == "repos/o/r/pulls/7"
        return Gh.pr

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


def _comment(body, login="me", when=LATER, url=None, kind="User"):
    return {"user": {"login": login, "type": kind}, "body": body, "created_at": when,
            "html_url": url or f"{PR}#issuecomment-{abs(hash((body, when)))}"}


def _status(task):
    return taskboard.store.get(task.id).status


def _github(task):
    return [c for c in taskboard.store.comments(task.id) if c.author == "github"]


def test_new_comment_requeues_with_it_on_the_timeline(gh):
    task = _in_review()
    gh.issue_comments = [_comment("rename foo")]
    gh.inline = [{"user": {"login": "me", "type": "User"}, "body": "nit", "path": "a.py", "line": 3,
                  "created_at": LATER, "html_url": f"{PR}#r1", "pull_request_review_id": None}]
    pr_watcher.check_all()
    assert _status(task) == TODO
    assert [c.body for c in _github(task)] == ["rename foo", "`a.py:3`\n\nnit"]
    assert _github(task)[0].external_author == "me" and not _github(task)[0].delivered
    assert ["api", "repos/o/r/pulls/7/comments", "--paginate"] in gh.calls


def test_agent_bot_old_and_empty_comments_are_ignored(gh):
    task = _in_review()
    gh.issue_comments = [
        _comment(f"Done.\n\n_🤖 Posted by yuki-conductor agent (task #1)_\n{GITHUB_AGENT_MARKER}"),
        _comment("Coverage report", login="codecov", kind="Bot"),
        _comment("Deploy preview ready", login="vercel[bot]"),
        _comment("from before the review", when=EARLIER),
    ]
    gh.reviews = [{"id": 1, "user": {"login": "me", "type": "User"}, "body": "",
                   "submitted_at": LATER, "html_url": f"{PR}#review-1"}]
    pr_watcher.check_all()
    assert _status(task) == READY_FOR_REVIEW
    assert _github(task) == []
    assert taskboard.store.get(task.id).pr_checked_at is not None


def test_inline_comments_count_from_review_submission(gh):
    """Drafted before the last poll, submitted after it: still new."""
    task = _in_review()
    taskboard.store.update_fields(task.id, pr_checked_at=pr_watcher._ts("2030-01-01T00:00:00Z"))
    gh.reviews = [{"id": 9, "user": {"login": "me", "type": "User"}, "body": "",
                   "submitted_at": "2030-01-02T00:00:00Z", "html_url": f"{PR}#review-9"}]
    gh.inline = [{"user": {"login": "me", "type": "User"}, "body": "use a set", "path": "x.py",
                  "line": None, "original_line": 8, "created_at": "2029-12-31T00:00:00Z",
                  "html_url": f"{PR}#r9", "pull_request_review_id": 9}]
    pr_watcher.check_all()
    assert _status(task) == TODO
    assert _github(task)[0].body == "`x.py:8`\n\nuse a set"


def test_comments_are_imported_once(gh):
    task = _in_review()
    gh.issue_comments = [_comment("fix it", url=f"{PR}#c1")]
    pr_watcher.check_all()
    # The agent handles it and the task returns to review; the next poll sees
    # the same comment again (its timestamp is still after the baseline).
    claim(task.id)
    transition(task.id, READY_FOR_REVIEW, reason="turn finished")
    taskboard.store.update_fields(task.id, pr_checked_at=0.5)
    pr_watcher.check_all()
    assert _status(task) == READY_FOR_REVIEW
    assert len(_github(task)) == 1


@pytest.mark.parametrize(
    "pr, reason",
    [({"state": "closed", "merged": True}, "PR merged"),
     ({"state": "closed", "merged": False}, "PR closed without merging")],
)
def test_merged_or_closed_is_done(gh, pr, reason):
    task = _in_review()
    gh.pr = pr
    gh.issue_comments = [_comment("late remark")]
    pr_watcher.check_all()
    assert _status(task) == DONE
    assert taskboard.store.comments(task.id)[-1].body.endswith(reason)
    assert taskboard.store.get(task.id).pr_state == ("MERGED" if pr["merged"] else "CLOSED")


def test_poll_records_the_pr_state(gh):
    task = _in_review()
    gh.pr = {"state": "open", "merged": False, "draft": True}
    pr_watcher.check_all()
    assert taskboard.store.get(task.id).pr_state == "DRAFT"
    assert _status(task) == READY_FOR_REVIEW


def test_gh_failure_leaves_the_task_alone(gh):
    task = _in_review()
    gh.fail = pr_watcher.GhError("HTTP 502")
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


def test_pr_state(gh):
    assert pr_watcher.pr_state(PR, None) == "OPEN"
    gh.pr = {"state": "open", "merged": False, "draft": True}
    assert pr_watcher.pr_state(PR, None) == "DRAFT"
    gh.pr = {"state": "closed", "merged": False}
    assert pr_watcher.pr_state(PR, None) == "CLOSED"
    gh.pr = {"state": "closed", "merged": True}
    assert pr_watcher.pr_state(PR, None) == "MERGED"


def test_refresh_runs_once_at_a_time(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    runs = []

    def slow_check_all():
        runs.append(1)
        entered.set()
        release.wait(5)

    monkeypatch.setattr(pr_watcher, "check_all", slow_check_all)
    assert pr_watcher.refresh() is True
    assert entered.wait(5)
    assert pr_watcher.refresh() is False  # one already in flight
    release.set()
    for _ in range(100):
        if not pr_watcher._refreshing.locked():
            break
        threading.Event().wait(0.02)
    entered.clear()
    assert pr_watcher.refresh() is True
    assert entered.wait(5)
    assert runs == [1, 1]


def test_refresh_one_runs_the_full_check_in_review(gh):
    task = _in_review()
    gh.pr = {"state": "closed", "merged": True}
    pr_watcher.refresh_one(taskboard.store.get(task.id))
    assert _status(task) == DONE
    assert taskboard.store.get(task.id).pr_state == "MERGED"


def test_check_skips_a_task_that_left_review(gh):
    task = _in_review()
    stale = taskboard.store.get(task.id)
    transition(task.id, TODO, reason="comment")
    gh.pr = {"state": "closed", "merged": True}
    pr_watcher.check(stale)
    assert _status(task) == TODO and gh.calls == []
