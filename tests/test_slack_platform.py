"""Tests for the Slack processing indicator (assistant status + reaction fallback)."""

import time

from yuki_conductor.messaging import slack_platform
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
