"""Tests for the platform-agnostic conversation core."""

from unittest.mock import patch

from yuki_conductor.claude_runner import ClaudeResult
from yuki_conductor.messaging import IncomingMessage, handle_incoming_message
from yuki_conductor.messaging.platform import OutgoingMessage


class StubPlatform:
    name = "stub"

    def __init__(self) -> None:
        self.sent: list[tuple[str, OutgoingMessage]] = []
        self.processing: list[tuple[str, str, bool]] = []
        self.sessions: dict[str, str] = {}
        self.title_hints: dict[str, str | None] = {}

    def send(self, conversation_key: str, msg: OutgoingMessage) -> None:
        self.sent.append((conversation_key, msg))

    def set_processing(self, conversation_key: str, message_id: str, on: bool) -> None:
        self.processing.append((conversation_key, message_id, on))

    def get_session_id(self, conversation_key: str) -> str | None:
        return self.sessions.get(conversation_key)

    def set_session_id(
        self, conversation_key: str, session_id: str, title_hint: str | None = None
    ) -> None:
        self.sessions[conversation_key] = session_id
        self.title_hints[conversation_key] = title_hint


def test_thread_start_persists_session_and_sends_reply():
    platform = StubPlatform()

    msg = IncomingMessage(
        platform="web",
        conversation_key="conv-1",
        message_id="msg-1",
        text="hi claude",
        is_thread_start=True,
        title_hint="hi claude",
    )

    fake = ClaudeResult(text="hello back", session_id="claude-session-1")
    with patch("yuki_conductor.messaging.conversation.run_claude", return_value=fake) as mock_run:
        handle_incoming_message(platform, msg)

    mock_run.assert_called_once()
    assert platform.sessions["conv-1"] == "claude-session-1"
    assert platform.title_hints["conv-1"] == "hi claude"
    assert platform.sent == [("conv-1", OutgoingMessage(text="hello back", attachments=[]))]
    assert platform.processing == [
        ("conv-1", "msg-1", True),
        ("conv-1", "msg-1", False),
    ]


def test_thread_reply_resumes_session():
    platform = StubPlatform()
    platform.sessions["conv-1"] = "claude-session-1"

    msg = IncomingMessage(
        platform="web",
        conversation_key="conv-1",
        message_id="msg-2",
        text="follow up",
        is_thread_start=False,
    )

    fake = ClaudeResult(text="continuing", session_id="claude-session-1")
    with patch("yuki_conductor.messaging.conversation.run_claude", return_value=fake) as mock_run:
        handle_incoming_message(platform, msg)

    args, kwargs = mock_run.call_args
    assert kwargs["session_id"] == "claude-session-1"
    assert platform.title_hints.get("conv-1") is None  # not a thread start
    assert platform.sent[0][1].text == "continuing"


def test_processing_off_called_even_on_exception():
    platform = StubPlatform()

    msg = IncomingMessage(
        platform="web",
        conversation_key="conv-1",
        message_id="msg-1",
        text="oops",
        is_thread_start=True,
    )

    with patch(
        "yuki_conductor.messaging.conversation.run_claude",
        side_effect=RuntimeError("boom"),
    ):
        try:
            handle_incoming_message(platform, msg)
        except RuntimeError:
            pass

    assert platform.processing[-1] == ("conv-1", "msg-1", False)
