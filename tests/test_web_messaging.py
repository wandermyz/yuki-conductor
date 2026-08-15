"""Smoke tests for the web messaging HTTP endpoints."""

import time

import pytest
from fastapi.testclient import TestClient

from yuki_conductor import web_server


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Build a fresh API with stores pointing at a temp DB and uploads dir."""
    db = tmp_path / "yuki.db"
    monkeypatch.setattr("yuki_conductor.config.DB_FILE", db)
    monkeypatch.setattr("yuki_conductor.config.WEB_UPLOADS_DIR", tmp_path / "uploads-web")

    # web_server captured config values at import time; rebuild stores against the new paths.
    from yuki_conductor import conversation_store, web_server
    from yuki_conductor.conversation_store import ConversationStore
    from yuki_conductor.store import SessionStore

    uploads_web = tmp_path / "uploads-web"
    monkeypatch.setattr(conversation_store, "DB_FILE", db)
    monkeypatch.setattr(web_server, "store", SessionStore(db_path=db))
    monkeypatch.setattr(web_server, "conv_store", ConversationStore(db_path=db))
    monkeypatch.setattr(web_server, "WEB_UPLOADS_DIR", uploads_web)
    monkeypatch.setattr("yuki_conductor.messaging.web_platform.WEB_UPLOADS_DIR", uploads_web)

    api = web_server.create_api()
    return TestClient(api)


def test_create_and_list_conversation(client):
    r = client.post("/api/conversations", json={"title": "smoke"})
    assert r.status_code == 200
    conv = r.json()
    assert conv["id"]
    assert conv["title"] == "smoke"

    listing = client.get("/api/conversations").json()
    assert any(c["id"] == conv["id"] for c in listing)


def test_post_message_invokes_claude_and_persists_reply(client, monkeypatch):
    from yuki_conductor.claude_runner import ClaudeResult

    fake = ClaudeResult(text="hello from claude", session_id="claude-1")
    monkeypatch.setattr(
        "yuki_conductor.messaging.conversation.run_claude", lambda *a, **k: fake
    )

    conv = client.post("/api/conversations", json={"title": None}).json()
    r = client.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hi"})
    assert r.status_code == 201
    user_msg = r.json()
    assert user_msg["role"] == "user"
    assert user_msg["text"] == "hi"

    # Worker thread runs handle_incoming_message; poll briefly for the reply.
    deadline = time.time() + 5
    while time.time() < deadline:
        msgs = client.get(f"/api/conversations/{conv['id']}/messages").json()
        if any(m["role"] == "assistant" for m in msgs):
            break
        time.sleep(0.05)

    msgs = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assistant = [m for m in msgs if m["role"] == "assistant"]
    assert assistant, "no assistant reply received"
    assert assistant[0]["text"] == "hello from claude"


def test_upload_and_download_roundtrip(client):
    files = {"file": ("hello.txt", b"hi there", "text/plain")}
    r = client.post("/api/uploads", files=files)
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "hello.txt"
    assert body["url"].startswith("/api/files/")

    dl = client.get(body["url"])
    assert dl.status_code == 200
    assert dl.content == b"hi there"


def test_post_message_rejects_unknown_attachment_id(client):
    conv = client.post("/api/conversations", json={"title": None}).json()
    r = client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"text": "hi", "attachment_ids": ["does-not-exist"]},
    )
    assert r.status_code == 400


def test_delete_conversation(client):
    conv = client.post("/api/conversations", json={"title": "bye"}).json()
    r = client.delete(f"/api/conversations/{conv['id']}")
    assert r.status_code == 200
    listing = client.get("/api/conversations").json()
    assert all(c["id"] != conv["id"] for c in listing)


def test_push_into_existing_conversation(client):
    conv = client.post("/api/conversations", json={"title": "watched"}).json()
    r = client.post(
        "/api/push", json={"conversation_id": conv["id"], "text": "progress update"}
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "conversation_id": conv["id"], "created": False}

    msgs = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert [m["role"] for m in msgs] == ["assistant"]
    # The marker is persisted, so a reload still distinguishes a push from a reply.
    assert msgs[0]["text"] == f"progress update\n\n{web_server.PUSH_MARKER}"
    assert client.get("/api/chat/conversations/statuses").json()[conv["id"]] == "unread"


def test_push_without_conversation_creates_one(client):
    r = client.post("/api/push", json={"text": "cron fired", "title": "Nightly"})
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True

    conv = client.get(f"/api/conversations/{body['conversation_id']}").json()
    assert conv["title"] == "Nightly"
    msgs = client.get(f"/api/conversations/{body['conversation_id']}/messages").json()
    assert msgs[0]["text"] == f"cron fired\n\n{web_server.PUSH_MARKER}"


def test_push_rejects_unknown_conversation(client):
    r = client.post("/api/push", json={"conversation_id": "nope", "text": "hi"})
    assert r.status_code == 404


def test_push_rejects_empty_text(client):
    conv = client.post("/api/conversations", json={"title": None}).json()
    r = client.post("/api/push", json={"conversation_id": conv["id"], "text": "  "})
    assert r.status_code == 400


def test_push_broadcasts_message_and_status(client, monkeypatch):
    """Both the message and the unread flip must reach connected browsers.

    Persisting alone is not enough: without the broadcast the user only sees the
    push after reloading the page.
    """
    from yuki_conductor import web_server

    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        web_server.ws_manager,
        "broadcast",
        lambda cid, payload: sent.append((cid, payload)),
    )

    conv = client.post("/api/conversations", json={"title": "watched"}).json()
    client.post("/api/push", json={"conversation_id": conv["id"], "text": "ping"})

    types = [p["type"] for _, p in sent]
    assert types == ["message", "status"]
    assert all(cid == conv["id"] for cid, _ in sent)
    assert sent[0][1]["message"]["text"] == f"ping\n\n{web_server.PUSH_MARKER}"
    # No `processing` event bookends a push, so the client alerts off this flag.
    assert sent[0][1]["pushed"] is True
    assert sent[1][1]["status"] == "unread"


def test_push_to_new_conversation_broadcasts_status(client, monkeypatch):
    from yuki_conductor import web_server

    sent: list[dict] = []
    monkeypatch.setattr(
        web_server.ws_manager, "broadcast", lambda cid, payload: sent.append(payload)
    )

    client.post("/api/push", json={"text": "cron fired"})
    assert [p["type"] for p in sent] == ["message", "status"]
    assert sent[0]["pushed"] is True
