"""Slack adapter for the messaging platform protocol."""

import logging
import urllib.request
from pathlib import Path

from yuki_conductor.config import UPLOADS_DIR
from yuki_conductor.formatting import markdown_to_mrkdwn
from yuki_conductor.messaging.platform import Attachment, OutgoingMessage

logger = logging.getLogger(__name__)


class SlackPlatform:
    """Implements MessagingPlatform on top of slack-bolt's Web API client."""

    name = "slack"

    def __init__(self, client, bot_token: str):
        self._client = client
        self._bot_token = bot_token

    def send(self, conversation_key: str, msg: OutgoingMessage) -> None:
        """Post a reply to a thread, uploading any attachments first."""
        channel = self._lookup_channel(conversation_key)
        if channel is None:
            logger.error(f"No channel known for thread_ts={conversation_key}; cannot send")
            return

        for att in msg.attachments:
            try:
                self._client.files_upload_v2(
                    channel=channel,
                    thread_ts=conversation_key,
                    file=str(att.local_path),
                    filename=att.filename,
                )
            except Exception:
                logger.error(f"Failed to upload attachment {att.local_path}", exc_info=True)

        if msg.text:
            self._client.chat_postMessage(
                channel=channel,
                thread_ts=conversation_key,
                text=markdown_to_mrkdwn(msg.text),
            )

    def set_processing(self, conversation_key: str, message_id: str, on: bool) -> None:
        """Toggle the hourglass reaction on the source message."""
        channel = self._lookup_channel(conversation_key)
        if channel is None:
            return
        try:
            if on:
                self._client.reactions_add(
                    channel=channel, name="hourglass_flowing_sand", timestamp=message_id
                )
            else:
                self._client.reactions_remove(
                    channel=channel, name="hourglass_flowing_sand", timestamp=message_id
                )
        except Exception:
            logger.debug("Reaction toggle failed", exc_info=True)

    def get_session_id(self, conversation_key: str) -> str | None:
        from yuki_conductor.store import SessionStore

        value = SessionStore().get(conversation_key)
        return value or None

    def set_session_id(
        self, conversation_key: str, session_id: str, title_hint: str | None = None
    ) -> None:
        from yuki_conductor.store import SessionStore

        store = SessionStore()
        kwargs = {
            "channel_id": self._lookup_channel(conversation_key),
            "session_type": "slack",
        }
        if title_hint is not None:
            kwargs["title"] = title_hint
        store.set(conversation_key, session_id, **kwargs)

    def _lookup_channel(self, thread_ts: str) -> str | None:
        """Resolve the Slack channel for a thread from the session store."""
        from yuki_conductor.store import SessionStore

        return SessionStore().get_channel(thread_ts)


def download_slack_files(files: list[dict], bot_token: str) -> list[Attachment]:
    """Download Slack file attachments to the uploads directory."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[Attachment] = []
    for f in files:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        name = f.get("name", "unknown")
        dest = UPLOADS_DIR / name
        if dest.exists():
            base_stem = Path(name).stem
            suffix = Path(name).suffix
            counter = 1
            while dest.exists():
                dest = UPLOADS_DIR / f"{base_stem}_{counter}{suffix}"
                counter += 1
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bot_token}"})
            with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:
                out.write(resp.read())
            saved.append(
                Attachment(
                    filename=dest.name,
                    local_path=dest,
                    mime_type=f.get("mimetype"),
                )
            )
            logger.info(f"Downloaded Slack file {name} -> {dest}")
        except Exception:
            logger.error(f"Failed to download Slack file {name}", exc_info=True)
    return saved
