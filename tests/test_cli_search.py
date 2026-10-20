"""`yuki-conductor search` and `conversation show`, end to end through the HTTP API."""

import sys

import pytest
from fastapi.testclient import TestClient

from yuki_conductor import cli_search, cli_task, taskboard, web_server
from yuki_conductor.cli import main
from yuki_conductor.conversation_store import ConversationStore


@pytest.fixture
def conv_store(monkeypatch, tmp_path):
    store = ConversationStore(db_path=tmp_path / "conv.db")
    monkeypatch.setattr(web_server, "conv_store", store)
    monkeypatch.setattr(web_server, "WEB_PUBLIC_URL", "https://yuki.example:23333/")
    return store


@pytest.fixture
def api(monkeypatch, conv_store):
    """Route the CLI's HTTP calls into an in-process app."""
    client = TestClient(web_server.create_api())

    def fake_request(method, path, payload=None, *, what="Request"):
        resp = client.request(method, path, json=payload)
        if resp.status_code >= 400:
            print(f"{what} failed ({resp.status_code}): {resp.json()['detail']}", file=sys.stderr)
            raise SystemExit(1)
        return resp.json()

    monkeypatch.setattr(cli_search, "api_request", fake_request)
    monkeypatch.setattr(cli_task, "api_request", fake_request)
    return client


def _task(title, body=""):
    return taskboard.store.create(
        title=title, project="proj", harness="claude-code", use_worktree=False, body=body
    )


def test_search_both_kinds_with_links(api, conv_store, capsys):
    conv = conv_store.create_conversation(title="Deploy chat")
    conv_store.add_message(conv.id, "assistant", "the runner\ntimed out")
    task = _task("Runner timeouts", "raise the limit")

    main(["search", "runner", "timed"])
    out = capsys.readouterr().out
    assert "## Conversations (1)" in out
    assert f"- Deploy chat · `{conv.id}`" in out
    assert "the **runner** **timed** out" in out
    assert f"https://yuki.example:23333/#chat/{conv.id}" in out
    assert "## Tasks (0)" in out  # every term must match

    main(["search", "--tasks", "timeout"])
    out = capsys.readouterr().out
    assert "Conversations" not in out
    assert f"- #{task.id} Runner timeouts · To Do" in out
    assert f"https://yuki.example:23333/#tasks/{task.id}" in out


def test_search_task_hit_names_the_comment(api, capsys):
    task = _task("Other")
    c = taskboard.store.add_comment(task.id, "user", "flaky sprocket test")
    main(["search", "--tasks", "sprocket"])
    assert f"· To Do · comment #{c.id}" in capsys.readouterr().out


def test_tasks_search_route_is_not_a_task_id(api):
    _task("Searchable")
    resp = api.get("/api/tasks/search", params={"q": "search"})
    assert resp.status_code == 200
    assert resp.json()[0]["status_label"] == "To Do"


def test_conversation_show(api, conv_store, capsys):
    conv = conv_store.create_conversation(title="Deploy chat", project="proj")
    conv_store.add_message(conv.id, "user", "why did it fail?")
    conv_store.add_message(conv.id, "assistant", "a KeyError")

    main(["conversation", "show", conv.id])
    out = capsys.readouterr().out
    assert out.startswith("# Deploy chat")
    assert f"https://yuki.example:23333/#chat/{conv.id}" in out
    assert out.index("why did it fail?") < out.index("a KeyError")


def test_conversation_show_unknown(api, capsys):
    with pytest.raises(SystemExit):
        main(["conversation", "show", "nope"])
    assert "not found" in capsys.readouterr().err


def test_task_show_has_a_link(api, capsys):
    task = _task("Linked")
    main(["task", "show", str(task.id)])
    assert f"- Link: https://yuki.example:23333/#tasks/{task.id}" in capsys.readouterr().out
