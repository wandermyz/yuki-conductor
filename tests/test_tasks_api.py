"""The /api/tasks and /api/taskboard endpoints."""

import pytest
from fastapi.testclient import TestClient

from yuki_conductor import taskboard, web_server
from yuki_conductor.task_store import IN_PROGRESS, NEEDS_INPUT, READY_FOR_REVIEW, TODO
from yuki_conductor.taskboard import actions, routes, runner
from yuki_conductor.taskboard.transitions import claim, transition


class FakeProjects:
    def get_path(self, name):
        return {"proj": "/repo", "other": "/other"}.get(name)


@pytest.fixture
def client(monkeypatch):
    enabled = {"claude-code": object(), "pi": object()}
    monkeypatch.setattr(actions, "ProjectStore", FakeProjects)
    monkeypatch.setattr(actions, "HARNESSES", enabled)
    monkeypatch.setattr(routes, "HARNESSES", enabled)
    monkeypatch.setattr(actions, "default_harness", lambda: "claude-code")
    monkeypatch.setattr(taskboard, "active_harvester", None)
    return TestClient(web_server.create_api())


def _create(client, **kw):
    payload = {"title": "Do it", "project": "proj", **kw}
    r = client.post("/api/tasks", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _status(task_id):
    return taskboard.store.get(task_id).status


def test_create_and_get(client, tmp_path):
    f = tmp_path / "spec.md"
    f.write_text("spec")
    task = _create(client, body="**md**", attachments=[str(f)], use_worktree=False)
    assert task["harness"] == "claude-code"
    assert task["status"] == TODO and task["status_label"] == "To Do"
    assert task["editable"] and task["harness_enabled"]
    got = client.get(f"/api/tasks/{task['id']}").json()
    assert got["body"] == "**md**"
    assert got["attachments"][0]["filename"] == "spec.md"
    assert got["comments"] == [] and got["children"] == [] and got["parent"] is None
    # The file is served because the task references it...
    assert client.get(got["attachments"][0]["url"]).text == "spec"
    # ...and nothing else is.
    assert client.get(f"/api/tasks/{task['id']}/file", params={"path": "/etc/hosts"}).status_code == 404


@pytest.mark.parametrize(
    "payload, detail",
    [
        ({"title": " ", "project": "proj"}, "title"),
        ({"title": "t", "project": "nope"}, "isn't registered"),
        ({"title": "t", "project": "proj", "harness": "dsh"}, "isn't enabled"),
        ({"title": "t", "project": "proj", "attachments": ["/no/such/file"]}, "Not a file"),
        ({"title": "t", "project": "proj", "parent_task_id": 999}, "not found"),
    ],
)
def test_create_validation(client, payload, detail):
    r = client.post("/api/tasks", json=payload)
    assert r.status_code in (400, 404)
    assert detail in r.json()["detail"]


def test_web_uploads_resolve_to_paths(client, monkeypatch, tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"png")
    monkeypatch.setattr(routes, "resolve_file", lambda fid: f if fid == "abc" else None)
    task = _create(client, attachment_ids=["abc"])
    assert taskboard.store.get(task["id"]).attachments == [str(f)]
    assert client.post("/api/tasks", json={"title": "t", "project": "proj", "attachment_ids": ["zzz"]}).status_code == 400


def test_edit_adds_web_uploads(client, monkeypatch, tmp_path):
    spec, shot = tmp_path / "spec.md", tmp_path / "shot.png"
    spec.write_text("spec")
    shot.write_bytes(b"png")
    monkeypatch.setattr(routes, "resolve_file", lambda fid: shot if fid == "abc" else None)
    task = _create(client, attachments=[str(spec)])
    r = client.patch(f"/api/tasks/{task['id']}", json={"body": "![shot](/api/files/abc)", "attachment_ids": ["abc"]})
    assert r.status_code == 200, r.text
    assert taskboard.store.get(task["id"]).attachments == [str(spec), str(shot)]
    assert taskboard.store.get(task["id"]).body == "![shot](/api/files/abc)"


def test_list_counts_comments_and_hides_archive(client):
    a = _create(client, title="a")
    _create(client, title="b")
    client.post(f"/api/tasks/{a['id']}/comments", json={"body": "hi"})
    tasks = client.get("/api/tasks").json()
    assert [t["title"] for t in tasks] == ["a", "b"]
    assert tasks[0]["comment_count"] == 1


def test_edit_only_before_pickup(client):
    task = _create(client)
    r = client.patch(f"/api/tasks/{task['id']}", json={"title": "Renamed", "harness": "pi"})
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed" and r.json()["harness"] == "pi"
    assert client.patch(f"/api/tasks/{task['id']}", json={"project": "nope"}).status_code == 400
    claim(task["id"])
    transition(task["id"], NEEDS_INPUT, reason="x")
    transition(task["id"], TODO, reason="x")  # To Do again, but it was picked up once
    assert client.patch(f"/api/tasks/{task['id']}", json={"title": "x"}).status_code == 409


@pytest.mark.parametrize(
    "setup, expected",
    [
        ([NEEDS_INPUT], TODO),
        ([IN_PROGRESS, READY_FOR_REVIEW], TODO),
        ([IN_PROGRESS], IN_PROGRESS),  # delivered when the turn ends
        ([], TODO),
    ],
)
def test_user_comment_requeue_rules(client, setup, expected):
    task = _create(client)
    for step in setup:
        if step == IN_PROGRESS:
            claim(task["id"])
        else:
            transition(task["id"], step, reason="setup")
    r = client.post(f"/api/tasks/{task['id']}/comments", json={"body": "more"})
    assert r.status_code == 201
    assert r.json()["delivered"] is False
    assert _status(task["id"]) == expected


def test_comment_on_done_task_does_not_reopen(client):
    task = _create(client)
    transition(task["id"], NEEDS_INPUT, reason="x")
    client.post(f"/api/tasks/{task['id']}/done")
    client.post(f"/api/tasks/{task['id']}/comments", json={"body": "thanks"})
    assert _status(task["id"]) == "done"


def test_agent_comment_never_requeues(client):
    task = _create(client)
    transition(task["id"], NEEDS_INPUT, reason="x")
    r = client.post(f"/api/tasks/{task['id']}/comments", json={"body": "note", "author": "agent"})
    assert r.json()["delivered"] is True
    assert _status(task["id"]) == NEEDS_INPUT


def test_comment_validation(client):
    task = _create(client)
    assert client.post(f"/api/tasks/{task['id']}/comments", json={"body": " "}).status_code == 400
    assert client.post(f"/api/tasks/{task['id']}/comments", json={"body": "x", "author": "system"}).status_code == 400
    assert client.post("/api/tasks/999/comments", json={"body": "x"}).status_code == 404


def test_comment_racing_the_end_of_a_turn_still_requeues(client, monkeypatch):
    """The turn moves the task to review while the comment is being stored."""
    task = _create(client)
    claim(task["id"])
    real_add = taskboard.store.add_comment

    def add_while_turn_ends(task_id, author, body, **kw):
        if author == "user":
            transition(task_id, READY_FOR_REVIEW, reason="turn finished")
        return real_add(task_id, author, body, **kw)

    monkeypatch.setattr(taskboard.store, "add_comment", add_while_turn_ends)
    client.post(f"/api/tasks/{task['id']}/comments", json={"body": "wait"})
    assert _status(task["id"]) == TODO


def test_quote_must_be_on_the_same_task(client):
    a, b = _create(client), _create(client)
    other = client.post(f"/api/tasks/{b['id']}/comments", json={"body": "x"}).json()
    r = client.post(f"/api/tasks/{a['id']}/comments", json={"body": "y", "quote_comment_id": other["id"]})
    assert r.status_code == 400


def test_edit_rejects_nulls(client):
    task = _create(client)
    for field in ("title", "use_worktree", "body"):
        assert client.patch(f"/api/tasks/{task['id']}", json={field: None}).status_code == 400


def test_quote_reply(client):
    task = _create(client)
    first = client.post(f"/api/tasks/{task['id']}/comments", json={"body": "q"}).json()
    reply = client.post(
        f"/api/tasks/{task['id']}/comments", json={"body": "> q\n\na", "quote_comment_id": first["id"]}
    ).json()
    assert reply["quote_comment_id"] == first["id"]


def test_ask_and_pr(client):
    task = _create(client)
    assert client.post(f"/api/tasks/{task['id']}/ask", json={"question": "Which DB?"}).status_code == 409
    claim(task["id"])
    assert client.post(f"/api/tasks/{task['id']}/ask", json={"question": "Which DB?"}).status_code == 201
    stored = taskboard.store.get(task["id"])
    assert stored.asked_input
    assert taskboard.store.comments(task["id"])[-1].author == "agent"
    r = client.post(f"/api/tasks/{task['id']}/pr", json={"url": "https://github.com/o/r/pull/9/"})
    assert r.json()["pr_url"] == "https://github.com/o/r/pull/9"
    assert r.json()["pr_state"] == "OPEN"
    assert client.post(f"/api/tasks/{task['id']}/pr", json={"url": "https://example.com"}).status_code == 400


def test_cancel_todo_and_in_progress(client, monkeypatch):
    queued = _create(client)
    assert client.post(f"/api/tasks/{queued['id']}/cancel").json()["status"] == NEEDS_INPUT

    running = _create(client)
    claim(running["id"])
    killed = []
    monkeypatch.setattr(runner, "cancel_process", lambda key: killed.append(key) or True)
    assert client.post(f"/api/tasks/{running['id']}/cancel").status_code == 200
    assert killed == [f"task:{running['id']}"]
    assert runner._cancel_pending(running["id"])
    runner.clear_cancel(running["id"])

    assert client.post(f"/api/tasks/{queued['id']}/cancel").status_code == 409


def test_mark_done(client):
    task = _create(client)
    assert client.post(f"/api/tasks/{task['id']}/done").status_code == 409
    transition(task["id"], NEEDS_INPUT, reason="x")
    done = client.post(f"/api/tasks/{task['id']}/done").json()
    assert done["status"] == "done" and done["done_at"]


def test_order_and_top(client):
    a, b, c = (_create(client, title=t) for t in "abc")
    assert client.put("/api/tasks/order", json={"ids": [c["id"], a["id"], b["id"]]}).status_code == 200
    assert [t.title for t in taskboard.store.list_status(TODO)] == ["c", "a", "b"]
    client.post(f"/api/tasks/{b['id']}/top")
    assert [t.title for t in taskboard.store.list_status(TODO)] == ["b", "c", "a"]
    claim(a["id"])
    assert client.put("/api/tasks/order", json={"ids": [a["id"], b["id"]]}).status_code == 400


def test_children_and_parent(client):
    parent = _create(client, title="parent")
    child = _create(client, title="child", parent_task_id=parent["id"])
    assert client.get(f"/api/tasks/{parent['id']}").json()["children"][0]["id"] == child["id"]
    assert client.get(f"/api/tasks/{child['id']}").json()["parent"]["title"] == "parent"


def test_board_state_and_pause(client, monkeypatch):
    from yuki_conductor import config

    monkeypatch.setattr(config, "TASKBOARD_CONCURRENCY", {})
    task = _create(client)
    claim(task["id"])
    state = client.get("/api/taskboard/state").json()
    assert state == {
        "paused": False,
        "running": {"claude-code": 1, "pi": 0},
        "limits": {"claude-code": 10, "pi": 1},
    }
    assert client.put("/api/taskboard/state", json={"paused": True}).json()["paused"] is True
    assert taskboard.board_state.paused()


def test_pr_refresh(client, monkeypatch):
    from yuki_conductor.taskboard import pr_watcher

    monkeypatch.setattr(pr_watcher, "refresh", lambda: True)
    r = client.post("/api/taskboard/pr-refresh")
    assert r.status_code == 202
    assert r.json() == {"started": True}


def test_steps_replay(client, monkeypatch):
    from yuki_conductor.taskboard import live

    task = _create(client)
    monkeypatch.setattr(live, "steps", lambda tid: [{"kind": "text", "label": "hi", "seq": 0}])
    assert client.get(f"/api/tasks/{task['id']}/steps").json()[0]["label"] == "hi"
    assert client.get("/api/tasks/999/steps").status_code == 404


def test_archive(client, monkeypatch):
    from yuki_conductor.taskboard import cleanup

    ran = []
    monkeypatch.setattr(cleanup, "run", lambda tid: ran.append(tid) or True)
    parent = _create(client, title="parent")
    task = _create(client, parent_task_id=parent["id"])
    claim(task["id"])
    r = client.post(f"/api/tasks/{task['id']}/archive")
    assert r.status_code == 409 and "Cancel" in r.json()["detail"]
    assert ran == []
    transition(task["id"], READY_FOR_REVIEW, reason="x")

    assert client.post(f"/api/tasks/{task['id']}/archive").status_code == 200
    assert ran == [task["id"]]
    assert taskboard.store.get(task["id"]).archived_at is not None  # still in the DB
    assert [t["title"] for t in client.get("/api/tasks?old=true").json()] == ["parent"]
    assert client.get(f"/api/tasks/{task['id']}").status_code == 404
    assert client.get(f"/api/tasks/{parent['id']}").json()["children"] == []
    assert client.post(f"/api/tasks/{task['id']}/comments", json={"body": "x"}).status_code == 404
    assert client.post(f"/api/tasks/{task['id']}/archive").status_code == 404


def test_archive_cancels_a_queued_task(client, monkeypatch):
    from yuki_conductor.taskboard import cleanup

    monkeypatch.setattr(cleanup, "run", lambda tid: True)
    task = _create(client)
    assert client.post(f"/api/tasks/{task['id']}/archive").status_code == 200
    stored = taskboard.store.get(task["id"])
    assert stored.status == NEEDS_INPUT and stored.archived_at is not None


def test_archive_fails_when_cleanup_fails(client, monkeypatch):
    from yuki_conductor.taskboard import cleanup

    def failed(tid):
        taskboard.store.update_fields(tid, cleanup_error="git broke")
        return False

    monkeypatch.setattr(cleanup, "run", failed)
    task = _create(client)
    transition(task["id"], NEEDS_INPUT, reason="x")
    r = client.post(f"/api/tasks/{task['id']}/archive")
    assert r.status_code == 500 and "git broke" in r.json()["detail"]
    assert taskboard.store.get(task["id"]).archived_at is None
    assert client.get(f"/api/tasks/{task['id']}").json()["cleanup_error"] == "git broke"


def test_archive_loses_to_a_requeue_during_cleanup(client, monkeypatch):
    from yuki_conductor.taskboard import cleanup

    task = _create(client)
    transition(task["id"], NEEDS_INPUT, reason="x")

    def requeued(tid):
        transition(tid, TODO, reason="new comment")
        return True

    monkeypatch.setattr(cleanup, "run", requeued)
    assert client.post(f"/api/tasks/{task['id']}/archive").status_code == 409
    assert taskboard.store.get(task["id"]).archived_at is None


def test_retry_cleanup(client, monkeypatch):
    from yuki_conductor.taskboard import cleanup

    task = _create(client)
    assert client.post(f"/api/tasks/{task['id']}/cleanup").status_code == 409
    transition(task["id"], NEEDS_INPUT, reason="x")
    transition(task["id"], "done", reason="x")
    monkeypatch.setattr(cleanup, "run", lambda tid: True)
    assert client.post(f"/api/tasks/{task['id']}/cleanup").status_code == 200
    def failed(tid):
        taskboard.store.update_fields(tid, cleanup_error="git broke")
        return False

    monkeypatch.setattr(cleanup, "run", failed)
    r = client.post(f"/api/tasks/{task['id']}/cleanup")
    assert r.status_code == 500 and "git broke" in r.json()["detail"]
