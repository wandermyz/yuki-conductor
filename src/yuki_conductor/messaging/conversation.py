"""Platform-agnostic core: turn an IncomingMessage into a Claude run + reply."""

import logging
import re
from pathlib import Path

from yuki_conductor.claude_runner import run_claude
from yuki_conductor.config import ATTACHMENTS_DIR
from yuki_conductor.messaging.platform import (
    Attachment,
    IncomingMessage,
    MessagingPlatform,
    OutgoingMessage,
)

logger = logging.getLogger(__name__)

_ATTACHMENT_RE = re.compile(r"<attachment>(.*?)</attachment>")


def _build_prompt(text: str, attachments: list[Attachment]) -> str:
    """Prepend <attachment> tags so Claude can read uploaded files from disk."""
    if not attachments:
        return text
    prefix = "".join(f"<attachment>{a.local_path}</attachment>\n" for a in attachments)
    return prefix + text


def _split_response(text: str) -> tuple[str, list[Attachment]]:
    """Pull <attachment>filename</attachment> tags out of Claude's reply.

    Filenames are looked up under ATTACHMENTS_DIR. Missing files are dropped
    with a warning rather than failing the whole reply.
    """
    matches = _ATTACHMENT_RE.findall(text)
    if not matches:
        return text, []

    attachments: list[Attachment] = []
    for filename in matches:
        path = ATTACHMENTS_DIR / filename
        if not path.exists():
            logger.warning(f"Claude referenced missing attachment: {path}")
            continue
        attachments.append(Attachment(filename=Path(filename).name, local_path=path))

    cleaned = _ATTACHMENT_RE.sub("", text).strip()
    return cleaned, attachments


def handle_incoming_message(
    platform: MessagingPlatform,
    msg: IncomingMessage,
) -> None:
    """Run Claude for an incoming message and send the reply through the platform.

    This is the only place that knows about session resolution, attachment
    plumbing, and the run_claude contract. Platforms own their own session-id
    persistence (Slack via SessionStore, web via ConversationStore).
    """
    platform.set_processing(msg.conversation_key, msg.message_id, on=True)
    try:
        session_id = (
            None if msg.is_thread_start else platform.get_session_id(msg.conversation_key)
        )

        prompt = _build_prompt(msg.text, msg.attachments)
        result = run_claude(prompt, session_id=session_id, model=msg.model)

        if result.session_id:
            platform.set_session_id(
                msg.conversation_key,
                result.session_id,
                title_hint=msg.title_hint if msg.is_thread_start else None,
            )

        reply_text, reply_attachments = _split_response(result.text)
        platform.send(
            msg.conversation_key,
            OutgoingMessage(text=reply_text, attachments=reply_attachments),
        )
    finally:
        platform.set_processing(msg.conversation_key, msg.message_id, on=False)
