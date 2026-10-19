"""Browser push: VAPID key, subscription API, and fan-out from WebPlatform."""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from yuki_conductor import web_push
from yuki_conductor.conversation_store import ConversationStore
from yuki_conductor.messaging.platform import OutgoingMessage
from yuki_conductor.messaging.web_platform import ConnectionManager, WebPlatform

SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "pk", "auth": "au"}}


@pytest.fixture
def sent(monkeypatch):
    """Capture pushes synchronously instead of hitting a push service."""
    calls: list[dict] = []

    def fake_webpush(sub, data, **kwargs):
        calls.append({"sub": sub, "data": json.loads(data), **kwargs})

    class InlineThread:
        def __init__(self, target, args, **_):
            self._run = lambda: target(*args)

        def start(self):
            self._run()

    monkeypatch.setattr(web_push, "webpush", fake_webpush)
    monkeypatch.setattr(web_push.threading, "Thread", InlineThread)
    return calls


def test_public_key_is_generated_once_and_persisted(isolated_push_subscriptions):
    key = web_push.public_key()
    raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    assert len(raw) == 65 and raw[0] == 4  # uncompressed P-256 point
    assert web_push.VAPID_KEY_FILE.is_file()
    assert web_push.VAPID_KEY_FILE.stat().st_mode & 0o777 == 0o600

    web_push._vapid = None
    assert web_push.public_key() == key


def test_subscription_api_roundtrip(isolated_push_subscriptions):
    from yuki_conductor import web_server

    client = TestClient(web_server.create_api())
    assert client.get("/api/webpush/key").json()["public_key"]

    assert client.post("/api/webpush/subscriptions", json=SUB).status_code == 200
    assert isolated_push_subscriptions.list_all() == [SUB]
    # Re-subscribing the same browser doesn't duplicate it.
    client.post("/api/webpush/subscriptions", json=SUB)
    assert len(isolated_push_subscriptions.list_all()) == 1

    r = client.post("/api/webpush/unsubscribe", json={"endpoint": SUB["endpoint"]})
    assert r.json()["removed"] is True
    assert isolated_push_subscriptions.list_all() == []


def test_web_reply_and_push_notify_subscribers(tmp_path, isolated_push_subscriptions, sent):
    isolated_push_subscriptions.add(SUB["endpoint"], "pk", "au")
    store = ConversationStore(db_path=tmp_path / "conv.db")
    platform = WebPlatform(store, ConnectionManager())
    conv = store.create_conversation(platform="web", title="Weekly report")

    platform.send(conv.id, OutgoingMessage(text="All   done\nhere."))
    platform.send(conv.id, OutgoingMessage(text="interim", pushed=True))

    assert [c["data"] for c in sent] == [
        {"target": {"kind": "conversation", "id": conv.id}, "title": "Weekly report", "body": "All done here."},
        {"target": {"kind": "conversation", "id": conv.id}, "title": "Weekly report", "body": "interim"},
    ]
    assert sent[0]["sub"] == SUB


def test_start_thread_notifies_and_body_is_truncated(tmp_path, isolated_push_subscriptions, sent):
    isolated_push_subscriptions.add(SUB["endpoint"], "pk", "au")
    platform = WebPlatform(ConversationStore(db_path=tmp_path / "conv.db"), ConnectionManager())

    conv_id = platform.start_thread("x" * 500, title="Cron")

    (call,) = sent
    assert call["data"]["target"] == {"kind": "conversation", "id": conv_id}
    assert len(call["data"]["body"]) == web_push.MAX_BODY_CHARS


def test_no_subscribers_sends_nothing(tmp_path, sent):
    platform = WebPlatform(ConversationStore(db_path=tmp_path / "conv.db"), ConnectionManager())
    platform.start_thread("hello")
    assert sent == []


def test_gone_subscription_is_dropped(isolated_push_subscriptions, monkeypatch, sent):
    class Resp:
        status_code = 410

    def gone(sub, **_):
        raise web_push.WebPushException("gone", response=Resp())

    isolated_push_subscriptions.add(SUB["endpoint"], "pk", "au")
    monkeypatch.setattr(web_push, "webpush", gone)
    web_push.notify(web_push.conversation_target("c1"), "t", "body")
    assert isolated_push_subscriptions.list_all() == []
