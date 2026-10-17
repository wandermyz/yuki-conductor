"""Platform-agnostic messaging core.

Slack and the web chat both feed `IncomingMessage` objects into
`handle_incoming_message`, which orchestrates the Claude run and dispatches
the response back through the originating `MessagingPlatform`.
"""

from yuki_conductor.messaging.conversation import (
    PUSHABLE_PLATFORMS,
    handle_incoming_message,
    split_response,
)
from yuki_conductor.messaging.platform import (
    Attachment,
    ChatAppReceiver,
    IncomingMessage,
    MessagingPlatform,
    OutgoingMessage,
)
from yuki_conductor.store import ModelStore, SessionStore

__all__ = [
    "PUSHABLE_PLATFORMS",
    "Attachment",
    "ChatAppReceiver",
    "IncomingMessage",
    "MessagingPlatform",
    "ModelStore",
    "OutgoingMessage",
    "SessionStore",
    "handle_incoming_message",
    "split_response",
]
