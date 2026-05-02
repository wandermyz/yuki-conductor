"""Teams CLI adapter — placeholder until the external `teams-cli` binary lands.

The real integration will spawn `teams-cli daemon --stdio` and exchange NDJSON
events over stdin/stdout:

    inbound  ← {"conv_id": str, "msg_id": str, "text": str,
                "attachments": [{"filename": str, "path": str, "mime_type": str|None}],
                "is_thread_start": bool}
    outbound → {"op": "send"|"set_processing"|"start_thread", ...}

For each inbound event the receiver builds an `IncomingMessage` and calls
`handle_incoming_message(self.platform, msg)`; outbound `send` /
`set_processing` from the platform are serialized back to the CLI.

Until that binary exists, the receiver only logs at startup and idles, and
`send`/`set_processing` log the payload they would have shipped. This keeps
the rest of yuki-conductor exercising the same code path.
"""

import logging
import uuid

from yuki_conductor.messaging.platform import OutgoingMessage

logger = logging.getLogger(__name__)

SESSION_TYPE = "teams_cli"
KEY_PREFIX = "teams:"


class TeamsCliPlatform:
    """MessagingPlatform implementation backed by the (future) teams-cli binary."""

    name = "teams_cli"

    def send(self, conversation_key: str, msg: OutgoingMessage) -> None:
        logger.info(
            f"[teams-cli placeholder] send conversation={conversation_key} "
            f"text={msg.text!r} attachments={len(msg.attachments)}"
        )

    def set_processing(self, conversation_key: str, message_id: str, on: bool) -> None:
        logger.debug(
            f"[teams-cli placeholder] processing conversation={conversation_key} "
            f"message={message_id} on={on}"
        )

    def get_session_id(self, conversation_key: str) -> str | None:
        from yuki_conductor.store import SessionStore

        value = SessionStore().get(conversation_key)
        return value or None

    def set_session_id(
        self, conversation_key: str, session_id: str, title_hint: str | None = None
    ) -> None:
        from yuki_conductor.store import SessionStore

        store = SessionStore()
        kwargs: dict = {"session_type": SESSION_TYPE}
        if title_hint is not None:
            kwargs["title"] = title_hint
        store.set(conversation_key, session_id, **kwargs)

    def start_thread(self, text: str, title: str | None = None) -> str:
        from yuki_conductor.store import SessionStore

        conversation_key = f"{KEY_PREFIX}{uuid.uuid4().hex}"
        SessionStore().set(
            conversation_key,
            value="",
            title=title or text[:100],
            session_type=SESSION_TYPE,
        )
        logger.info(
            f"[teams-cli placeholder] start_thread {conversation_key}: {text!r}"
        )
        return conversation_key


class TeamsCliReceiver:
    """Receiver placeholder. Logs at startup and idles."""

    name = "teams_cli"

    def __init__(self) -> None:
        self.platform = TeamsCliPlatform()

    def start(self) -> None:
        logger.warning(
            "Teams CLI receiver is a placeholder; the teams-cli binary is not "
            "wired up yet. Outbound messages will be logged only."
        )
