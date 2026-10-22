"""`yuki-conductor task …`, end to end through the HTTP API."""

import io
import json

import pytest
from fastapi.testclient import TestClient

from yuki_conductor import cli_http, cli_task, taskboard, web_server
from yuki_conductor.cli import main
from yuki_conductor.task_store import NEEDS_INPUT
from yuki_conductor.taskboard import actions, routes
from yuki_conductor.taskboard.transitions import claim, transition


class FakeProjects:
    def get_path(self, name):
        return "/repo" if name == "proj" else None


@pytest.fixture
def api(monkeypatch):
    """Route the CLI's HTTP calls into an in-process app."""
    enabled = {"claude-code": object()}
    monkeypatch.setattr(actions, "ProjectStore", FakeProjects)
    monkeypatch.setattr(actions, "HARNESSES", enabled)
    monkeypatch.setattr(routes, "HARNESSES", enabled)
    monkeypatch.setattr(actions, "default_harness", lambda: "claude-code")
    monkeypatch.delenv("YUKI_TASK_ID", raising=False)
    client = TestClient(web_server.create_api())
    sent = []

    def fake_request(method, path, payload=None, *, what="Request"):
        sent.append((method, path, payload))
        resp = client.request(method, path, json=payload)
        if resp.status_code >= 400:
            print(f"{what} failed ({resp.status_code}): {resp.json()['detail']}", file=__import__("sys").stderr)
            raise SystemExit(1)
        return resp.json()

    monkeypatch.setattr(cli_task, "api_request", fake_request)
    return sent


def _task(**kw):
    return taskboard.store.create(title="Fix it", project="proj", harness="claude-code", use_worktree=False, **kw)


def test_create_from_shell(api, capsys):
    main(["task", "create", "--project", "proj", "--title", "Add cache", "--worktree", "Use redis"])
    assert "Created task #1: Add cache" in capsys.readouterr().out
    task = taskboard.store.get(1)
    assert task.use_worktree and task.body == "Use redis" and task.parent_task_id is None


def test_create_inside_a_task_links_the_parent(api, monkeypatch):
    parent = _task()
    monkeypatch.setenv("YUKI_TASK_ID", str(parent.id))
    main(["task", "create", "--project", "proj", "--title", "Follow-up", "later"])
    assert taskboard.store.children(parent.id)[0].title == "Follow-up"


def test_create_reports_api_errors(api, capsys):
    with pytest.raises(SystemExit):
        main(["task", "create", "--project", "nope", "--title", "t"])
    assert "isn't registered" in capsys.readouterr().err


def test_comment_as_agent_defaults_to_own_task(api, monkeypatch):
    task = _task()
    monkeypatch.setenv("YUKI_TASK_ID", str(task.id))
    main(["task", "comment", "progress"])
    comment = taskboard.store.comments(task.id)[-1]
    assert (comment.author, comment.body) == ("agent", "progress")


def test_agent_commenting_on_another_task_is_input_for_it(api, monkeypatch):
    mine, other = _task(), _task()
    monkeypatch.setenv("YUKI_TASK_ID", str(mine.id))
    main(["task", "comment", str(other.id), "heads up"])
    comment = taskboard.store.comments(other.id)[-1]
    assert comment.author == "user" and not comment.delivered


def test_done_on_own_running_task(api, monkeypatch, capsys):
    task = _task()
    claim(task.id)
    monkeypatch.setenv("YUKI_TASK_ID", str(task.id))
    main(["task", "done"])
    assert "when this turn ends" in capsys.readouterr().out
    assert taskboard.store.get(task.id).done_requested


def test_done_on_a_waiting_task(api, capsys):
    task = _task()
    transition(task.id, NEEDS_INPUT, reason="x")
    main(["task", "done", str(task.id)])
    assert f"Marked task #{task.id} done" in capsys.readouterr().out
    assert taskboard.store.get(task.id).status == "done"


def test_ask_only_on_own_task(api, monkeypatch, capsys):
    mine, other = _task(), _task()
    monkeypatch.setenv("YUKI_TASK_ID", str(mine.id))
    with pytest.raises(SystemExit):
        main(["task", "ask", str(other.id), "?"])
    assert "own running task" in capsys.readouterr().err


def test_comment_as_user_with_id_quote_and_attachment(api, tmp_path):
    task = _task()
    first = taskboard.store.add_comment(task.id, "agent", "q")
    f = tmp_path / "a.txt"
    f.write_text("x")
    main(["task", "comment", "--quote", str(first.id), "--attach", str(f), str(task.id), "answer"])
    comment = taskboard.store.comments(task.id)[-1]
    assert comment.author == "user"
    assert comment.quote_comment_id == first.id
    assert comment.attachments == [str(f.resolve())]


def test_comment_from_stdin(api, monkeypatch):
    task = _task()
    monkeypatch.setattr("sys.stdin", io.StringIO("multi\nline"))
    main(["task", "comment", str(task.id), "-"])
    assert taskboard.store.comments(task.id)[-1].body == "multi\nline"


def test_missing_id_outside_a_task(api, capsys):
    with pytest.raises(SystemExit):
        main(["task", "comment", "hello"])
    assert "No task id" in capsys.readouterr().err


def test_ask_and_pr(api, monkeypatch, capsys):
    task = _task()
    claim(task.id)
    monkeypatch.setenv("YUKI_TASK_ID", str(task.id))
    main(["task", "ask", "Which DB?"])
    main(["task", "pr", "https://github.com/o/r/pull/3"])
    stored = taskboard.store.get(task.id)
    assert stored.asked_input and stored.pr_url == "https://github.com/o/r/pull/3"
    assert "End your turn now" in capsys.readouterr().out


def test_show_and_list(api, capsys):
    task = _task(body="the body")
    taskboard.store.add_comment(task.id, "user", "a comment")
    taskboard.store.add_comment(
        task.id, "github", "nit", external_author="octo", external_url="https://github.com/x"
    )
    main(["task", "show", str(task.id)])
    out = capsys.readouterr().out
    assert f"# Task #{task.id}: Fix it" in out
    assert "the body" in out and "a comment" in out and "github · octo" in out

    other = _task()
    from yuki_conductor.taskboard.transitions import transition

    transition(other.id, NEEDS_INPUT, reason="x")
    main(["task", "list", "--status", "needs_input"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [f"#{other.id}\tNeeds Input\tproj\tFix it"]


def test_api_request_reports_http_errors(monkeypatch, capsys):
    import urllib.error

    def raise_404(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 404, "nf", {}, io.BytesIO(json.dumps({"detail": "Task #9 not found"}).encode())
        )

    monkeypatch.setattr("urllib.request.urlopen", raise_404)
    with pytest.raises(SystemExit):
        cli_http.api_request("GET", "/api/tasks/9", what="Show")
    assert "Show failed (404): Task #9 not found" in capsys.readouterr().err
