"""Slack adapter for the messaging platform protocol."""

import logging
import threading
import urllib.request
from pathlib import Path

from yuki_conductor.config import UPLOADS_DIR, slack_app_dm_channel, slack_cron_channel
from yuki_conductor.formatting import markdown_to_mrkdwn
from yuki_conductor.messaging.platform import Attachment, OutgoingMessage

logger = logging.getLogger(__name__)

SESSION_TYPE = "slack"

# Shimmering "<app name> is thinking..." line under the user's message. Slack drops
# the status after two minutes of silence, so a background thread re-sets it.
STATUS_TEXT = "is thinking..."
STATUS_REFRESH_SECONDS = 60


class SlackPlatform:
    """Implements MessagingPlatform on top of slack-bolt's Web API client."""

    name = "slack"

    def __init__(self, client, bot_token: str):
        self._client = client
        self._bot_token = bot_token
        self._refreshers: dict[str, tuple[threading.Event, threading.Thread]] = {}
        self._refreshers_lock = threading.Lock()

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
        """Show the shimmering assistant status while Claude runs.

        Falls back to an hourglass reaction if the status API is unavailable
        (e.g. the app lacks the scope, or the thread isn't one Slack accepts).
        """
        channel = self._lookup_channel(conversation_key)
        if channel is None:
            return

        if on:
            if self._set_status(channel, conversation_key, STATUS_TEXT):
                self._start_status_refresh(channel, conversation_key)
            else:
                self._toggle_reaction(channel, message_id, on=True)
            return

        if self._stop_status_refresh(conversation_key):
            # Posting the reply already clears the status; this covers runs that
            # produced no message (errors, attachment-only replies that failed).
            self._set_status(channel, conversation_key, "")
        else:
            self._toggle_reaction(channel, message_id, on=False)

    def _set_status(self, channel: str, thread_ts: str, status: str) -> bool:
        try:
            self._client.assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status=status
            )
            return True
        except Exception:
            logger.debug("Assistant status update failed", exc_info=True)
            return False

    def _start_status_refresh(self, channel: str, thread_ts: str) -> None:
        stop = threading.Event()

        def refresh() -> None:
            while not stop.wait(STATUS_REFRESH_SECONDS):
                if not self._set_status(channel, thread_ts, STATUS_TEXT):
                    return

        thread = threading.Thread(
            target=refresh, name=f"slack-status-{thread_ts}", daemon=True
        )
        with self._refreshers_lock:
            self._refreshers[thread_ts] = (stop, thread)
        thread.start()

    def _stop_status_refresh(self, thread_ts: str) -> bool:
        """Stop the refresh loop. Returns False if this thread never had a status."""
        with self._refreshers_lock:
            entry = self._refreshers.pop(thread_ts, None)
        if entry is None:
            return False
        stop, thread = entry
        stop.set()
        thread.join(timeout=5)
        return True

    def _toggle_reaction(self, channel: str, message_id: str, on: bool) -> None:
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

    def send_notification(self, text: str) -> None:
        try:
            self._client.chat_postMessage(
                channel=slack_app_dm_channel(),
                text=markdown_to_mrkdwn(text),
            )
        except Exception:
            logger.warning("Failed to send Slack notification", exc_info=True)

    def start_thread(self, text: str, title: str | None = None) -> str:
        """Post a top-level message to the cron channel and return its `ts`."""
        channel = slack_cron_channel()
        response = self._client.chat_postMessage(
            channel=channel, text=markdown_to_mrkdwn(text)
        )
        thread_ts = response["ts"]
        from yuki_conductor.store import SessionStore

        SessionStore().set(
            thread_ts,
            value="",
            channel_id=channel,
            title=title or text[:100],
            session_type=SESSION_TYPE,
        )
        return thread_ts


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
