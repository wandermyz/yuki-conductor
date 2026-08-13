"""Tests for streaming intermediate steps out to platforms."""

from unittest.mock import MagicMock, patch

from yuki_conductor.claude_runner import ClaudeResult
from yuki_conductor.messaging import IncomingMessage, handle_incoming_message
from yuki_conductor.messaging.slack_platform import SlackPlatform, _status_for
from yuki_conductor.messaging.web_platform import ConnectionManager, WebPlatform
from yuki_conductor.stream_events import StreamEvent


class StubPlatform:
    """Minimal platform that records the stream events it is handed."""

    name = "stub"

    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    def send(self, conversation_key, msg): ...
    def set_processing(self, conversation_key, message_id, on): ...
    def get_session_id(self, conversation_key): return None
    def set_session_id(self, conversation_key, session_id, title_hint=None): ...

    def on_stream_event(self, conversation_key: str, event: StreamEvent) -> None:
        self.events.append(event)


def _msg() -> IncomingMessage:
    return IncomingMessage(
        platform="stub", conversation_key="conv-1", message_id="m1", text="hi",
    )


def test_conversation_core_forwards_events_to_platform():
    platform = StubPlatform()

    def fake_run(prompt, **kwargs):
        kwargs["on_event"](StreamEvent(kind="tool_use", label="Read(a.py)"))
        return ClaudeResult(text="done", session_id="s1")

    with patch("yuki_conductor.messaging.conversation.run_claude", side_effect=fake_run):
        handle_incoming_message(platform, _msg())

    assert [e.label for e in platform.events] == ["Read(a.py)"]


def test_platform_without_hook_gets_no_callback():
    """Chat plugins that don't implement the hook still run — they just get
    the final response, with on_event passed as None."""

    class NoHook:
        name = "nohook"

        def send(self, conversation_key, msg): self.sent = msg
        def set_processing(self, conversation_key, message_id, on): ...
        def get_session_id(self, conversation_key): return None
        def set_session_id(self, conversation_key, session_id, title_hint=None): ...

    platform = NoHook()
    captured = {}

    def fake_run(prompt, **kwargs):
        captured.update(kwargs)
        return ClaudeResult(text="final answer", session_id="s1")

    with patch("yuki_conductor.messaging.conversation.run_claude", side_effect=fake_run):
        handle_incoming_message(platform, _msg())

    assert captured["on_event"] is None
    assert platform.sent.text == "final answer"


def test_failing_platform_hook_does_not_break_the_run():
    class Exploding(StubPlatform):
        def on_stream_event(self, conversation_key, event):
            raise RuntimeError("nope")

    platform = Exploding()

    def fake_run(prompt, **kwargs):
        kwargs["on_event"](StreamEvent(kind="text", label="x"))
        return ClaudeResult(text="done", session_id="s1")

    with patch("yuki_conductor.messaging.conversation.run_claude", side_effect=fake_run):
        handle_incoming_message(platform, _msg())


# ── Web ───────────────────────────────────────────────────────────────────


def test_web_steps_survive_the_run_and_reset_on_the_next_one():
    manager = ConnectionManager()
    platform = WebPlatform(store=MagicMock(), manager=manager)

    platform.set_processing("conv-1", "m1", on=True)
    platform.on_stream_event("conv-1", StreamEvent(kind="tool_use", label="Read(a.py)"))
    platform.on_stream_event("conv-1", StreamEvent(kind="text", label="writing"))

    steps = manager.get_steps()["conv-1"]
    assert [s["label"] for s in steps] == ["Read(a.py)", "writing"]
    assert [s["seq"] for s in steps] == [0, 1]

    # The finished turn keeps its trace so the UI can show it next to the reply.
    platform.set_processing("conv-1", "m1", on=False)
    assert [s["label"] for s in manager.get_steps()["conv-1"]] == ["Read(a.py)", "writing"]

    # The next turn starts from a clean slate.
    platform.set_processing("conv-1", "m2", on=True)
    assert manager.get_steps()["conv-1"] == []


def test_web_steps_are_dropped_outside_a_run():
    """A late event after the run ended must not resurrect a buffer."""
    manager = ConnectionManager()
    platform = WebPlatform(store=MagicMock(), manager=manager)
    platform.on_stream_event("conv-1", StreamEvent(kind="text", label="stray"))
    assert manager.get_steps() == {}


def test_web_step_buffer_is_bounded():
    from yuki_conductor.messaging.web_platform import MAX_BUFFERED_STEPS

    manager = ConnectionManager()
    manager.set_processing("conv-1", "m1", on=True)
    for i in range(MAX_BUFFERED_STEPS + 25):
        manager.add_step("conv-1", {"kind": "text", "label": f"step {i}", "detail": {}})

    steps = manager.get_steps()["conv-1"]
    assert len(steps) == MAX_BUFFERED_STEPS
    # Sequence numbers keep counting past the trim so clients can still dedupe.
    assert steps[-1]["seq"] == MAX_BUFFERED_STEPS + 24
    assert steps[-1]["label"] == f"step {MAX_BUFFERED_STEPS + 24}"


# ── Slack ─────────────────────────────────────────────────────────────────


def test_slack_status_rendering():
    tool = StreamEvent(
        kind="tool_use", label="Bash(pytest)",
        detail={"name": "Bash", "summary": "pytest -q"},
    )
    assert _status_for(tool) == "is running Bash: pytest -q"

    text = StreamEvent(kind="text", label="Looking", detail={"text": "Looking at the file"})
    assert _status_for(text) == "is writing: Looking at the file"

    # Results and init would only make the status flicker.
    assert _status_for(StreamEvent(kind="tool_result", label="ok", detail={})) is None
    assert _status_for(StreamEvent(kind="init", label="Starting…", detail={})) is None


def test_slack_status_is_length_capped():
    event = StreamEvent(
        kind="tool_use", label="x",
        detail={"name": "Bash", "summary": "y" * 300},
    )
    assert len(_status_for(event)) <= 80


def test_slack_events_do_not_post_messages():
    """Intermediate steps update the status only — no chat_postMessage."""
    client = MagicMock()
    platform = SlackPlatform(client=client, bot_token="t")

    with patch.object(platform, "_lookup_channel", return_value="C1"), \
         patch.object(platform, "_set_status", return_value=True):
        platform.set_processing("1.0", "1.0", on=True)
        platform.on_stream_event(
            "1.0",
            StreamEvent(kind="tool_use", label="Bash", detail={"name": "Bash", "summary": "ls"}),
        )
        pending = platform._pending_status["1.0"]
        platform.set_processing("1.0", "1.0", on=False)

    assert pending == "is running Bash: ls"
    client.chat_postMessage.assert_not_called()


def test_slack_events_ignored_when_no_run_is_active():
    """Without a live status refresher there's nothing to update."""
    platform = SlackPlatform(client=MagicMock(), bot_token="t")
    platform.on_stream_event(
        "1.0", StreamEvent(kind="tool_use", label="Bash", detail={"name": "Bash"})
    )
    assert platform._pending_status == {}
