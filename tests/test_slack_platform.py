"""Tests for the Slack processing indicator (assistant status + reaction fallback)."""

import time

from yuki_conductor.messaging import slack_platform
from yuki_conductor.messaging.platform import OutgoingMessage
from yuki_conductor.messaging.slack_platform import SlackPlatform


class FakeClient:
    """Records Slack Web API calls; optionally fails setStatus."""

    def __init__(self, status_fails: bool = False):
        self.status_fails = status_fails
        self.status_calls: list[dict] = []
        self.reaction_calls: list[tuple[str, dict]] = []

    def assistant_threads_setStatus(self, **kwargs):
        self.status_calls.append(kwargs)
        if self.status_fails:
            raise RuntimeError("missing_scope")
        return {"ok": True}

    def reactions_add(self, **kwargs):
        self.reaction_calls.append(("add", kwargs))

    def reactions_remove(self, **kwargs):
        self.reaction_calls.append(("remove", kwargs))


def _platform(client) -> SlackPlatform:
    platform = SlackPlatform(client=client, bot_token="xoxb-test")
    platform._lookup_channel = lambda _thread_ts: "C123"
    return platform


def test_sets_and_clears_assistant_status():
    client = FakeClient()
    platform = _platform(client)

    platform.set_processing("111.222", "111.222", on=True)
    platform.set_processing("111.222", "111.222", on=False)

    assert client.status_calls == [
        {
            "channel_id": "C123",
            "thread_ts": "111.222",
            "status": slack_platform.STATUS_TEXT,
        },
        {"channel_id": "C123", "thread_ts": "111.222", "status": ""},
    ]
    assert client.reaction_calls == []


def test_falls_back_to_reaction_when_status_unavailable():
    client = FakeClient(status_fails=True)
    platform = _platform(client)

    platform.set_processing("111.222", "111.222", on=True)
    platform.set_processing("111.222", "111.222", on=False)

    assert [call[0] for call in client.reaction_calls] == ["add", "remove"]
    assert all(
        call[1]["name"] == "hourglass_flowing_sand" for call in client.reaction_calls
    )
    # Only the initial attempt; no clear call once we've fallen back.
    assert len(client.status_calls) == 1


def test_refresh_thread_keeps_status_alive_and_stops(monkeypatch):
    monkeypatch.setattr(slack_platform, "STATUS_REFRESH_SECONDS", 0.01)
    client = FakeClient()
    platform = _platform(client)

    platform.set_processing("111.222", "111.222", on=True)
    _stop, thread = platform._refreshers["111.222"]
    deadline = time.monotonic() + 5
    while len(client.status_calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(client.status_calls) >= 3, "status was not refreshed"
    platform.set_processing("111.222", "111.222", on=False)

    assert not thread.is_alive()
    assert client.status_calls[-1]["status"] == ""
    assert not platform._refreshers


def _recording_platform():
    client = FakeClient()
    client.posted = []
    client.chat_postMessage = lambda **kw: (client.posted.append(kw), {"ts": "999.000"})[1]
    return client, _platform(client)


def test_long_message_is_split_across_messages():
    """Slack caps message length; long replies continue in the same thread."""
    client, platform = _recording_platform()
    body = "\n\n".join(f"paragraph {i} " + "word " * 100 for i in range(30))

    platform.send("111.222", OutgoingMessage(text=body))

    assert len(client.posted) > 1
    for kw in client.posted:
        assert len(kw["text"]) <= slack_platform.MESSAGE_LIMIT
        assert kw["thread_ts"] == "111.222"
    # Nothing dropped: every word survives somewhere, in order.
    joined = " ".join(kw["text"] for kw in client.posted)
    assert joined.split() == body.split()


def test_short_message_posts_once_unmodified():
    client, platform = _recording_platform()

    platform.send("111.222", OutgoingMessage(text="hello"))

    assert [kw["text"] for kw in client.posted] == ["hello"]


def test_split_reopens_code_fence():
    chunks = slack_platform._for_slack("```\n" + "line of code\n" * 500 + "```")

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0
    assert chunks[1].startswith("```")


def test_start_thread_hangs_overflow_under_the_root_message(monkeypatch):
    monkeypatch.setenv("SLACK_CRON_CHANNEL", "C999")
    client, platform = _recording_platform()
    platform_store_calls = []
    import yuki_conductor.store as store_mod

    class FakeStore:
        def set(self, *a, **kw):
            platform_store_calls.append((a, kw))

    original = store_mod.SessionStore
    store_mod.SessionStore = FakeStore
    try:
        ts = platform.start_thread("word " * 2000)
    finally:
        store_mod.SessionStore = original

    assert ts == "999.000"
    assert len(client.posted) > 1
    assert "thread_ts" not in client.posted[0]
    assert all(kw["thread_ts"] == ts for kw in client.posted[1:])
