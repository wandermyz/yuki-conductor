"""Platform-agnostic messaging core.

Slack and the web chat both feed `IncomingMessage` objects into
`handle_incoming_message`, which orchestrates the Claude run and dispatches
the response back through the originating `MessagingPlatform`.
"""

from yuki_conductor.messaging.conversation import handle_incoming_message
from yuki_conductor.messaging.platform import (
    Attachment,
    IncomingMessage,
    MessagingPlatform,
    OutgoingMessage,
)

__all__ = [
    "Attachment",
    "IncomingMessage",
    "MessagingPlatform",
    "OutgoingMessage",
    "handle_incoming_message",
]
