"""Slack adapter for the messaging platform protocol."""

import logging
import threading
import time
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
# How often the updater thread checks for a new step label. Slack rate-limits
# setStatus, so steps are coalesced into at most one call per interval rather
# than one call per event.
STATUS_POLL_SECONDS = 2

# Slack renders the status inline, so keep step labels short.
STATUS_LABEL_LIMIT = 80

# Slack visibly truncates messages past ~4000 characters, so long replies are
# split across several messages in the same thread rather than cut off. Only
# Slack has this limit — web chat posts the full text in one piece.
MESSAGE_LIMIT = 4000
# Room for the ``` fences _balance_fences may add to either end of a chunk.
_FENCE_RESERVE = 8


def _split_text(text: str, limit: int) -> list[str]:
    """Break text into <=limit chunks, preferring paragraph/line/word boundaries."""
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        # Prefer the latest natural boundary in the window; fall back to a hard
        # cut only when a single run of text is longer than the whole limit.
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks or [""]


def _balance_fences(chunks: list[str]) -> list[str]:
    """Close an open code fence at a chunk's end and reopen it in the next one.

    Splitting mid-fence would otherwise leave one message with an unterminated
    ``` and render the next as plain text.
    """
    out: list[str] = []
    carry = False
    for chunk in chunks:
        body = f"```\n{chunk}" if carry else chunk
        carry = body.count("```") % 2 == 1
        out.append(f"{body}\n```" if carry else body)
    return out


def _for_slack(text: str) -> list[str]:
    """Convert markdown to mrkdwn and split it into Slack-sized messages."""
    return _balance_fences(_split_text(markdown_to_mrkdwn(text), MESSAGE_LIMIT - _FENCE_RESERVE))


def _status_for(event) -> str | None:
    """Render a stream event as a Slack status line, or None to keep the last one.

    Tool results and plain init events aren't worth a status change — they'd
    make the line flicker without telling the user anything new.
    """
    if event.kind == "tool_use":
        name = event.detail.get("name") or "tool"
        summary = event.detail.get("summary") or ""
        text = f"is running {name}: {summary}" if summary else f"is running {name}"
    elif event.kind == "text":
        text = "is writing: " + (event.detail.get("text") or event.label)
    elif event.kind == "thinking":
        text = "is thinking: " + (event.detail.get("text") or event.label)
    else:
        return None
    text = " ".join(text.split())
    return text if len(text) <= STATUS_LABEL_LIMIT else text[: STATUS_LABEL_LIMIT - 1] + "…"


class SlackPlatform:
    """Implements MessagingPlatform on top of slack-bolt's Web API client."""

    name = "slack"

    def __init__(self, client, bot_token: str):
        self._client = client
        self._bot_token = bot_token
        self._refreshers: dict[str, tuple[threading.Event, threading.Thread]] = {}
        self._refreshers_lock = threading.Lock()
        # Latest status label per thread, published by the updater thread.
        self._pending_status: dict[str, str] = {}

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
            for chunk in _for_slack(msg.text):
                self._client.chat_postMessage(
                    channel=channel,
                    thread_ts=conversation_key,
                    text=chunk,
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

    def on_stream_event(self, conversation_key: str, event) -> None:
        """Fold an intermediate step into the thread's shimmering status line.

        Nothing is posted as a message — the status is the only surface used
        until the final reply lands.
        """
        status = _status_for(event)
        if status is None:
            return
        with self._refreshers_lock:
            if conversation_key in self._refreshers:
                self._pending_status[conversation_key] = status

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
            last_pushed = STATUS_TEXT
            last_push_at = time.monotonic()
            while not stop.wait(STATUS_POLL_SECONDS):
                with self._refreshers_lock:
                    desired = self._pending_status.get(thread_ts, last_pushed)
                stale = time.monotonic() - last_push_at >= STATUS_REFRESH_SECONDS
                if desired == last_pushed and not stale:
                    continue
                if not self._set_status(channel, thread_ts, desired):
                    return
                last_pushed = desired
                last_push_at = time.monotonic()

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
            self._pending_status.pop(thread_ts, None)
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
            for chunk in _for_slack(text):
                self._client.chat_postMessage(
                    channel=slack_app_dm_channel(),
                    text=chunk,
                )
        except Exception:
            logger.warning("Failed to send Slack notification", exc_info=True)

    def start_thread(self, text: str, title: str | None = None) -> str:
        """Post a top-level message to the cron channel and return its `ts`.

        A long body opens the thread with its first chunk and hangs the rest
        underneath, so the channel shows one entry rather than several.
        """
        channel = slack_cron_channel()
        chunks = _for_slack(text)
        response = self._client.chat_postMessage(channel=channel, text=chunks[0])
        thread_ts = response["ts"]
        for chunk in chunks[1:]:
            self._client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk)
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
