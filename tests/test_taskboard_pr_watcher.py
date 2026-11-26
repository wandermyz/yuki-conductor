"""The PR watcher, with `gh api` mocked (GitHub REST shapes)."""

import re
import subprocess
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


def _in_review(pr_url=PR):
    task = taskboard.store.create(title="t", project="p", harness="claude-code", use_worktree=False)
    claim(task.id)
    taskboard.store.update_fields(task.id, pr_url=pr_url)
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


@pytest.fixture
def graphql(monkeypatch):
    """`graphql.prs` maps "owner/repo#n" to a GraphQL pullRequest node (or None)."""

    class Fake:
        prs: dict = {}
        queries: list = []

    def fake(query):
        Fake.queries.append(query)
        data = {}
        for alias, owner, repo, number in re.findall(
            r'(p\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\) '
            r"\{ pullRequest\(number: (\d+)\)", query
        ):
            data[alias] = {"pullRequest": Fake.prs.get(f"{owner}/{repo}#{number}")}
        return data

    Fake.prs, Fake.queries = {}, []
    monkeypatch.setattr(pr_watcher, "_graphql", fake)
    monkeypatch.setattr(pr_watcher, "_gh", lambda *a: pytest.fail("poll made a REST call"))
    return Fake


OTHER = "https://github.com/x/y/pull/3"


def test_poll_finishes_merged_and_closed_prs_in_one_query(graphql):
    merged, closed, still_open = _in_review(), _in_review(OTHER), _in_review(
        "https://github.com/o/r/pull/8")
    graphql.prs = {"o/r#7": {"state": "MERGED", "isDraft": False},
                   "x/y#3": {"state": "CLOSED", "isDraft": False},
                   "o/r#8": {"state": "OPEN", "isDraft": True}}
    pr_watcher.poll()
    assert len(graphql.queries) == 1
    assert _status(merged) == DONE and _status(closed) == DONE
    assert taskboard.store.comments(merged.id)[-1].body.endswith("PR merged")
    assert taskboard.store.get(closed.id).pr_state == "CLOSED"
    assert _status(still_open) == READY_FOR_REVIEW
    assert taskboard.store.get(still_open.id).pr_state == "DRAFT"


def test_poll_finishes_a_pr_already_recorded_as_merged(graphql):
    """The ↻ button can record MERGED while the task is still running."""
    task = _in_review()
    taskboard.store.update_fields(task.id, pr_state="MERGED")
    graphql.prs = {"o/r#7": {"state": "MERGED", "isDraft": False}}
    pr_watcher.poll()
    assert _status(task) == DONE


def test_poll_ignores_comments_and_unknown_prs(graphql):
    task = _in_review()
    other = _in_review(OTHER)
    graphql.prs = {"o/r#7": {"state": "OPEN", "isDraft": False}}  # x/y#3: not found
    pr_watcher.poll()
    assert _status(task) == READY_FOR_REVIEW and _status(other) == READY_FOR_REVIEW
    assert _github(task) == [] and taskboard.store.get(task.id).pr_checked_at is None


def test_poll_without_review_prs_asks_nothing(graphql):
    task = taskboard.store.create(title="t", project="p", harness="claude-code", use_worktree=False)
    transition(task.id, NEEDS_INPUT, reason="x")
    taskboard.store.update_fields(task.id, pr_url=PR)
    pr_watcher.poll()
    assert graphql.queries == []


def test_poll_failure_leaves_tasks_alone(monkeypatch):
    task = _in_review()

    def fail(query):
        raise pr_watcher.GhError("HTTP 502")

    monkeypatch.setattr(pr_watcher, "_graphql", fail)
    pr_watcher.poll()
    assert _status(task) == READY_FOR_REVIEW


def test_statuses_batches_large_lists(graphql, monkeypatch):
    monkeypatch.setattr(pr_watcher, "_BATCH", 2)
    pr_watcher.statuses([f"https://github.com/o/r/pull/{n}" for n in range(5)])
    assert [q.count("pullRequest(") for q in graphql.queries] == [2, 2, 1]


def test_graphql_reads_partial_answers(monkeypatch):
    """gh exits 1 when one PR fails, but the others are still in `data`."""
    out = ('{"data": {"p0": {"pullRequest": {"state": "OPEN", "isDraft": false}}, '
           '"p1": {"pullRequest": null}}, "errors": [{"message": "Could not resolve"}]}')
    seen = {}

    def run(args, **kw):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 1, out, "gh: Could not resolve")

    monkeypatch.setattr(pr_watcher.subprocess, "run", run)
    assert pr_watcher.statuses([PR, "https://github.com/o/r/pull/8", PR]) == {PR: "OPEN"}
    assert seen["args"][:3] == ["gh", "api", "graphql"]


def test_graphql_raises_when_the_query_fails(monkeypatch):
    monkeypatch.setattr(
        pr_watcher.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "HTTP 502"),
    )
    with pytest.raises(pr_watcher.GhError, match="HTTP 502"):
        pr_watcher.statuses([PR])


def test_loop_polls_until_stopped(monkeypatch):
    stop = threading.Event()
    runs = []

    def poll():
        runs.append(1)
        if len(runs) == 2:
            stop.set()

    monkeypatch.setattr(pr_watcher, "poll", poll)
    thread = threading.Thread(target=pr_watcher.loop, args=(stop, 0.01))
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and runs == [1, 1]
