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
    from yuki_conductor.harness.base import RunResult

    fake = RunResult(text="hello from claude", session_id="claude-1")
    monkeypatch.setattr("yuki_conductor.messaging.conversation.run_agent", lambda *a, **k: fake)

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


def test_reply_reports_context_occupation(client, monkeypatch):
    """The chat's context meter reads it straight off the message list."""
    from yuki_conductor.harness.base import RunResult

    fake = RunResult(
        text="done", session_id="claude-1",
        input_tokens=50000, output_tokens=300,
        context_tokens=40000, context_window=200000, model="claude-opus-5-5",
    )
    monkeypatch.setattr(
        "yuki_conductor.messaging.conversation.run_agent", lambda *a, **k: fake
    )

    conv = client.post("/api/conversations", json={"title": None}).json()
    client.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hi"})

    deadline = time.time() + 5
    while time.time() < deadline:
        msgs = client.get(f"/api/conversations/{conv['id']}/messages").json()
        if any(m["role"] == "assistant" for m in msgs):
            break
        time.sleep(0.05)

    msgs = client.get(f"/api/conversations/{conv['id']}/messages").json()
    user, assistant = msgs[0], msgs[1]
    assert (assistant["context_tokens"], assistant["context_window"]) == (40000, 200000)
    assert assistant["model"] == "claude-opus-5-5"
    assert (user["context_tokens"], user["context_window"], user["model"]) == (None, None, None)


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


def test_file_download_flag_sets_attachment_disposition(client):
    files = {"file": ("notes.txt", b"hi", "text/plain")}
    url = client.post("/api/uploads", files=files).json()["url"]

    assert client.get(url).headers["content-disposition"].startswith("inline")
    assert client.get(url + "?download=1").headers["content-disposition"].startswith("attachment")


def test_html_file_is_sandboxed(client):
    files = {"file": ("page.html", b"<h1>hi</h1>", "text/html")}
    url = client.post("/api/uploads", files=files).json()["url"]

    html = client.get(url)
    assert html.headers["content-type"].startswith("text/html")
    assert html.headers["content-security-policy"].startswith("sandbox")
    assert "allow-same-origin" not in html.headers["content-security-policy"]

    txt = client.post("/api/uploads", files={"file": ("a.txt", b"x", "text/plain")}).json()
    assert "content-security-policy" not in client.get(txt["url"]).headers


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
    r = client.post("/api/push", json={"conversation_id": conv["id"], "text": "progress update"})
    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "conversation_id": conv["id"],
        "platform": "web",
        "created": False,
    }

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


def test_push_with_attachment_stores_it(client, tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG")
    conv = client.post("/api/conversations", json={"title": "watched"}).json()
    r = client.post("/api/push", json={"conversation_id": conv["id"], "attachments": [str(img)]})
    assert r.status_code == 200

    [msg] = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert msg["text"] == web_server.PUSH_MARKER
    [att] = msg["attachments"]
    assert att["filename"] == "pic.png"
    assert client.get(att["url"]).content == b"\x89PNG"


def test_push_rejects_missing_attachment(client, tmp_path):
    conv = client.post("/api/conversations", json={"title": None}).json()
    for bad in [str(tmp_path / "nope.png"), "relative.png"]:
        r = client.post(
            "/api/push",
            json={"conversation_id": conv["id"], "text": "hi", "attachments": [bad]},
        )
        assert r.status_code == 400
    assert client.get(f"/api/conversations/{conv['id']}/messages").json() == []


def test_push_routes_slack_thread_to_slack_platform(client, monkeypatch, tmp_path):
    from types import SimpleNamespace

    sent = []
    slack = SimpleNamespace(name="slack", send=lambda key, msg: sent.append((key, msg)))
    monkeypatch.setattr(web_server, "_receivers", [SimpleNamespace(platform=slack)])
    web_server.store.set("1700000000.0001", value="", channel_id="C1", session_type="slack")
    img = tmp_path / "pic.png"
    img.write_bytes(b"png")

    r = client.post(
        "/api/push",
        json={"conversation_id": "1700000000.0001", "text": "hi", "attachments": [str(img)]},
    )
    assert r.status_code == 200
    assert r.json()["platform"] == "slack"
    [(key, msg)] = sent
    assert key == "1700000000.0001"
    assert msg.text == f"hi\n\n{web_server.PUSH_MARKER}"
    assert msg.pushed is True
    assert [a.local_path for a in msg.attachments] == [img]


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


def test_reader_page_renders_reply_as_article(client):
    conv = client.post("/api/conversations", json={"title": "Voices"}).json()
    first = web_server.conv_store.add_message(
        conv["id"], "assistant", "# Heading\n\nFirst **reply** <script>x</script>"
    )
    web_server.conv_store.add_message(conv["id"], "assistant", "second reply")
    web_server.conv_store.add_message(conv["id"], "user", "a follow-up")

    r = client.get(f"/read/{conv['id']}/{first.id}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    page = r.text
    assert "<article>" in page and "<h1>Voices</h1>" in page
    assert "<strong>reply</strong>" in page
    assert "<script>" not in page  # raw HTML in a reply is escaped

    # `last` skips the trailing user message and returns the newest assistant reply.
    assert "second reply" in client.get(f"/read/{conv['id']}/last").text

    assert client.get(f"/read/{conv['id']}/nope").status_code == 404
    assert client.get("/read/nope/last").status_code == 404


def test_frontend_cache_headers(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    (dist / "assets" / "index-abc.js").write_text("//")
    monkeypatch.setattr(web_server, "_WEB_DIST", dist)
    api = web_server.FastAPI()
    web_server.mount_static(api)
    c = TestClient(api)

    for url in ("/", "/index.html"):
        assert c.get(url).headers["cache-control"] == "no-store"
    assert "immutable" in c.get("/assets/index-abc.js").headers["cache-control"]


def test_archive_conversation_hides_it_everywhere(client):
    conv = client.post("/api/conversations", json={"title": "shelve"}).json()
    r = client.post(f"/api/conversations/{conv['id']}/archive")
    assert r.status_code == 200
    assert all(c["id"] != conv["id"] for c in client.get("/api/conversations").json())
    assert client.get(f"/api/conversations/{conv['id']}").status_code == 404
    assert client.get(f"/api/conversations/{conv['id']}/messages").status_code == 404
    assert client.post(f"/api/conversations/{conv['id']}/archive").status_code == 404


def test_search_api(client):
    from yuki_conductor import web_server

    conv = client.post("/api/conversations", json={"title": "flaky ci"}).json()
    msg = web_server.conv_store.add_message(conv["id"], "assistant", "the runner timed out")
    r = client.get("/api/search", params={"q": "timed"})
    assert r.status_code == 200
    [hit] = r.json()
    assert hit["conversation_id"] == conv["id"]
    assert hit["title"] == "flaky ci"
    assert hit["message_id"] == msg.id
    assert "\x02timed\x03" in hit["snippet"]
    assert isinstance(hit["score"], float)

    assert client.get("/api/search", params={"q": '"( AND'}).status_code == 200
    assert client.get("/api/search").json() == []

    client.post(f"/api/conversations/{conv['id']}/archive")
    assert client.get("/api/search", params={"q": "timed"}).json() == []


def test_chat_ws_answers_ping_with_pong(client):
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_text("ping")
        assert ws.receive_json() == {"type": "pong"}


def test_model_is_picked_per_conversation_and_used_for_its_turns(client, monkeypatch):
    from yuki_conductor.harness.base import RunResult

    conv = client.post("/api/conversations", json={"title": "m"}).json()
    assert conv["model"] is None
    r = client.patch(f"/api/conversations/{conv['id']}", json={"model": "sonnet"})
    assert r.status_code == 200 and r.json()["model"] == "sonnet"
    assert r.json()["title"] == "m"  # untouched

    seen = []
    monkeypatch.setattr(
        "yuki_conductor.messaging.conversation.run_agent",
        lambda harness, prompt, **kw: seen.append(kw["model"]) or RunResult(text="ok", session_id="s"),
    )
    client.post(f"/api/conversations/{conv['id']}/messages", json={"text": "hi"})
    deadline = time.time() + 5
    while not seen and time.time() < deadline:
        time.sleep(0.05)
    assert seen == ["sonnet"]

    # A rename leaves the model alone; null returns to the harness default.
    client.patch(f"/api/conversations/{conv['id']}", json={"title": "renamed"})
    assert client.get(f"/api/conversations/{conv['id']}").json()["model"] == "sonnet"
    client.patch(f"/api/conversations/{conv['id']}", json={"model": None})
    assert client.get(f"/api/conversations/{conv['id']}").json()["model"] is None


def test_harnesses_list_their_models(client):
    harnesses = client.get("/api/harnesses").json()["harnesses"]
    [claude] = [h for h in harnesses if h["name"] == "claude-code"]
    [codex] = [h for h in harnesses if h["name"] == "codex"]
    assert claude["default_label"] == "sonnet"
    assert codex["default_label"] == "sol"
    assert {"id": "sonnet", "label": "sonnet"} in claude["models"]
    assert {"id": "opus", "label": "opus"} in claude["models"]
    assert codex["default_model"] == "gpt-6.1-sol"
    assert {"id": "gpt-6-astra", "label": "astra"} in codex["models"]
    assert {"id": "gpt-6.1-sol", "label": "sol"} in codex["models"]

