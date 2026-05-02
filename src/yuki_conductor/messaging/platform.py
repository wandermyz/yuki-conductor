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


@dataclass
class OutgoingMessage:
    """A message produced by Claude, ready to be sent back through a platform."""

    text: str
    attachments: list[Attachment] = field(default_factory=list)


class MessagingPlatform(Protocol):
    """Abstract messaging surface. Slack and the web chat both implement this."""

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
