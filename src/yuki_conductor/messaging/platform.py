"""Types and protocol for messaging platforms (Slack, web, etc.)."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Attachment:
    """A file accompanying a message — either uploaded by the user or produced by Claude."""

    filename: str
    local_path: Path
    mime_type: str | None = None


@dataclass
class IncomingMessage:
    """A message arriving from a platform, normalized for the conversation core."""

    platform: str
    conversation_key: str
    message_id: str
    text: str
    attachments: list[Attachment] = field(default_factory=list)
    is_thread_start: bool = False
    title_hint: str | None = None
    model: str | None = None
    cwd: str | None = None


@dataclass
class OutgoingMessage:
    """A message produced by Claude, ready to be sent back through a platform."""

    text: str
    attachments: list[Attachment] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class MessagingPlatform(Protocol):
    """Abstract messaging surface. Slack, Teams CLI, and the web chat all implement this."""

    name: str

    def send(self, conversation_key: str, msg: OutgoingMessage) -> None: ...

    def set_processing(self, conversation_key: str, message_id: str, on: bool) -> None: ...

    def get_session_id(self, conversation_key: str) -> str | None:
        """Return the Claude session id to resume, or None to start fresh."""
        ...

    def set_session_id(
        self, conversation_key: str, session_id: str, title_hint: str | None = None
    ) -> None:
        """Persist the Claude session id (and optional initial title) for resume."""
        ...

    def start_thread(self, text: str, title: str | None = None) -> str:
        """Open a new conversation thread with an initial message and return its key.

        Used by the cron scheduler (and any other producer) to seed a thread
        the platform owns. Slack creates a top-level message and returns its
        `thread_ts`; Teams CLI returns a fresh `teams:{uuid}`; web creates a
        new conversation row.
        """
        ...


class ChatAppReceiver(Protocol):
    """A non-blocking entry point that wires a chat app's inbound stream
    into `handle_incoming_message`."""

    name: str
    platform: MessagingPlatform

    def start(self) -> None:
        """Start the receiver. Must not block; spawn threads as needed."""
        ...
