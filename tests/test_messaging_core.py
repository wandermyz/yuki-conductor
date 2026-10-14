"""Tests for the platform-agnostic conversation core."""

from unittest.mock import MagicMock, patch

from yuki_conductor.harness.base import RunResult
from yuki_conductor.messaging import IncomingMessage, handle_incoming_message
from yuki_conductor.messaging.platform import OutgoingMessage


class StubPlatform:
    name = "stub"

    def __init__(self) -> None:
        self.sent: list[tuple[str, OutgoingMessage]] = []
        self.processing: list[tuple[str, str, bool]] = []
        self.sessions: dict[str, str] = {}
        self.title_hints: dict[str, str | None] = {}
        self.harnesses: dict[str, str] = {}

    def send(self, conversation_key: str, msg: OutgoingMessage) -> None:
        self.sent.append((conversation_key, msg))

    def set_processing(self, conversation_key: str, message_id: str, on: bool) -> None:
        self.processing.append((conversation_key, message_id, on))

    def get_session_id(self, conversation_key: str) -> str | None:
        return self.sessions.get(conversation_key)

    def get_harness(self, conversation_key: str) -> str | None:
        return self.harnesses.get(conversation_key)

    def set_session_id(
        self,
        conversation_key: str,
        session_id: str,
        title_hint: str | None = None,
        harness: str = "claude-code",
    ) -> None:
        self.sessions[conversation_key] = session_id
        self.title_hints[conversation_key] = title_hint
        self.harnesses.setdefault(conversation_key, harness)


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

    fake = RunResult(text="hello back", session_id="claude-session-1")
    with patch("yuki_conductor.messaging.conversation.run_agent", return_value=fake) as mock_run:
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

    fake = RunResult(text="continuing", session_id="claude-session-1")
    with patch("yuki_conductor.messaging.conversation.run_agent", return_value=fake) as mock_run:
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
        "yuki_conductor.messaging.conversation.run_agent",
        side_effect=RuntimeError("boom"),
    ):
        try:
            handle_incoming_message(platform, msg)
        except RuntimeError:
            pass

    assert platform.processing[-1] == ("conv-1", "msg-1", False)


def test_split_response_accepts_absolute_paths(tmp_path):
    from yuki_conductor.messaging import split_response

    img = tmp_path / "out.png"
    img.write_bytes(b"png")
    text, atts = split_response(f"look <attachment>{img}</attachment>")
    assert text == "look"
    assert [(a.filename, a.local_path) for a in atts] == [("out.png", img)]


def test_split_response_resolves_bare_names_under_attachments_dir(tmp_path, monkeypatch):
    from yuki_conductor.messaging import conversation

    monkeypatch.setattr(conversation, "ATTACHMENTS_DIR", tmp_path)
    (tmp_path / "a.txt").write_text("x")
    text, atts = conversation.split_response("<attachment>a.txt</attachment> <attachment>gone.txt</attachment>")
    assert text == ""
    assert [a.local_path for a in atts] == [tmp_path / "a.txt"]


def test_push_conversation_id_only_for_pushable_platforms():
    platform = StubPlatform()
    fake = RunResult(text="ok", session_id="s")
    for name, expected in [("web", "k"), ("slack", "k"), ("echo", None)]:
        msg = IncomingMessage(
            platform=name, conversation_key="k", message_id="m", text="hi", is_thread_start=True
        )
        with patch("yuki_conductor.messaging.conversation.run_agent", return_value=fake) as run:
            handle_incoming_message(platform, msg)
        assert run.call_args.kwargs["push_conversation_id"] == expected


# --------------------------------------------------------------------------
# Harness selection
# --------------------------------------------------------------------------


def _run_and_capture_harness(platform, msg, monkeypatch, enabled=("claude-code", "other")):
    """Run one turn with exactly `enabled` harnesses on, first = default.

    "other" is a stand-in for a second harness such as pi. The enabled
    registry is a module-level dict read by resolve_harness and
    default_harness, so it's swapped in place and restored afterwards.
    """
    from yuki_conductor import harness as harness_pkg

    other = MagicMock(name="other-harness")
    other.name = "other"
    known = {**harness_pkg.KNOWN_HARNESSES, "other": other}
    saved = dict(harness_pkg.HARNESSES)
    harness_pkg.HARNESSES.clear()
    harness_pkg.HARNESSES.update({n: known[n] for n in enabled})
    fake = RunResult(text="ok", session_id="s-new")
    try:
        with patch("yuki_conductor.messaging.conversation.run_agent", return_value=fake) as run:
            handle_incoming_message(platform, msg)
    finally:
        harness_pkg.HARNESSES.clear()
        harness_pkg.HARNESSES.update(saved)
    return (run.call_args.args[0] if run.call_args else None), platform


def _start(key="conv-h", harness=None):
    return IncomingMessage(
        platform="web", conversation_key=key, message_id="m", text="hi",
        is_thread_start=True, harness=harness,
    )


def test_new_conversation_uses_the_requested_harness(monkeypatch):
    harness, platform = _run_and_capture_harness(StubPlatform(), _start(harness="other"), monkeypatch)
    assert harness.name == "other"
    assert platform.harnesses["conv-h"] == "other"


def test_new_conversation_without_a_choice_uses_the_first_enabled_harness(monkeypatch):
    harness, platform = _run_and_capture_harness(
        StubPlatform(), _start(), monkeypatch, enabled=("other", "claude-code")
    )
    assert harness.name == "other"
    assert platform.harnesses["conv-h"] == "other"


def test_first_message_uses_the_harness_the_conversation_was_created_with(monkeypatch):
    """Web creates the conversation, with its harness, before the first message.

    Regression: the first message is a thread start, and once ignored the stored
    harness, so a conversation created as pi ran its first turn on the default.
    """
    platform = StubPlatform()
    platform.harnesses["conv-h"] = "other"
    harness, _ = _run_and_capture_harness(platform, _start(), monkeypatch)
    assert harness.name == "other"


def test_existing_conversation_keeps_its_harness(monkeypatch):
    """A session id only works with the harness that created it."""
    platform = StubPlatform()
    platform.sessions["conv-h"] = "s-old"
    platform.harnesses["conv-h"] = "other"
    reply = IncomingMessage(
        platform="web", conversation_key="conv-h", message_id="m2", text="more",
        harness="claude-code",
    )
    harness, _ = _run_and_capture_harness(platform, reply, monkeypatch)
    assert harness.name == "other"
    assert platform.harnesses["conv-h"] == "other"


def test_unknown_harness_falls_back_to_the_default(monkeypatch):
    harness, _ = _run_and_capture_harness(StubPlatform(), _start(harness="nope"), monkeypatch)
    assert harness.name == "claude-code"


def test_new_conversation_asking_for_a_disabled_harness_gets_the_default(monkeypatch):
    harness, platform = _run_and_capture_harness(
        StubPlatform(), _start(harness="other"), monkeypatch, enabled=("claude-code",)
    )
    assert harness.name == "claude-code"
    assert platform.harnesses["conv-h"] == "claude-code"


def test_conversation_bound_to_a_disabled_harness_fails_instead_of_switching(monkeypatch):
    """Running it on another harness couldn't resume its session anyway."""
    platform = StubPlatform()
    platform.sessions["conv-h"] = "s-old"
    platform.harnesses["conv-h"] = "other"
    reply = IncomingMessage(platform="web", conversation_key="conv-h", message_id="m2", text="hi")
    harness, _ = _run_and_capture_harness(platform, reply, monkeypatch, enabled=("claude-code",))
    assert harness is None  # nothing ran
    [(key, sent)] = platform.sent
    assert key == "conv-h"
    assert "isn't enabled" in sent.text and "other" in sent.text
    assert platform.processing[-1] == ("conv-h", "m2", False)


def test_claude_model_pin_only_reaches_claude_code(monkeypatch):
    """pi would get `--model opus[1m]`, a model name it doesn't know."""
    from yuki_conductor import harness as harness_pkg

    other = MagicMock(name="other-harness")
    other.name = "other"
    saved = dict(harness_pkg.HARNESSES)
    harness_pkg.HARNESSES.clear()
    harness_pkg.HARNESSES.update({"claude-code": harness_pkg.KNOWN_HARNESSES["claude-code"], "other": other})
    fake = RunResult(text="ok", session_id="s")
    try:
        for name, expected in (("claude-code", "opus[1m]"), ("other", None)):
            msg = IncomingMessage(
                platform="web", conversation_key=f"c-{name}", message_id="m", text="hi",
                is_thread_start=True, harness=name, model="opus[1m]",
            )
            with patch("yuki_conductor.messaging.conversation.run_agent", return_value=fake) as run:
                handle_incoming_message(StubPlatform(), msg)
            assert run.call_args.kwargs["model"] == expected, name
    finally:
        harness_pkg.HARNESSES.clear()
        harness_pkg.HARNESSES.update(saved)
