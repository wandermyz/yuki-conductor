"""Smoke tests for the web messaging HTTP endpoints."""

import time

import pytest
from fastapi.testclient import TestClient


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
