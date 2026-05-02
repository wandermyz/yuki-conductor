"""Web adapter for the messaging platform protocol.

Persists assistant messages to the conversation store and broadcasts events
to any WebSocket clients subscribed to the conversation.
"""

import asyncio
import logging
import shutil
import threading
import uuid
from collections.abc import Iterable
from pathlib import Path

from yuki_conductor.config import WEB_UPLOADS_DIR
from yuki_conductor.conversation_store import (
    ConversationStore,
    StoredAttachment,
    StoredMessage,
)
from yuki_conductor.messaging.platform import OutgoingMessage

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Tracks WebSocket subscribers per conversation, schedules broadcasts.

    Threads call `broadcast()` (e.g. from `handle_incoming_message` worker
    threads); we marshal the actual `send_json` onto the asyncio loop the
    socket lives on.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, list[tuple[object, asyncio.AbstractEventLoop]]] = {}

    def add(self, conv_id: str, ws, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._subs.setdefault(conv_id, []).append((ws, loop))

    def remove(self, conv_id: str, ws) -> None:
        with self._lock:
            subs = self._subs.get(conv_id) or []
            self._subs[conv_id] = [(s, lp) for (s, lp) in subs if s is not ws]
            if not self._subs[conv_id]:
                self._subs.pop(conv_id, None)

    def broadcast(self, conv_id: str, payload: dict) -> None:
        with self._lock:
            targets = list(self._subs.get(conv_id) or [])
        for ws, loop in targets:
            try:
                asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)
            except Exception:
                logger.debug("WS broadcast failed", exc_info=True)


def _serialize_attachments(atts: list[StoredAttachment]) -> list[dict]:
    return [
        {"id": a.id, "filename": a.filename, "url": a.url, "mime_type": a.mime_type}
        for a in atts
    ]


def serialize_message(msg: StoredMessage) -> dict:
    return {
        "id": msg.id,
        "conversation_id": msg.conversation_id,
        "role": msg.role,
        "text": msg.text,
        "attachments": _serialize_attachments(msg.attachments),
        "created_at": msg.created_at,
    }


def store_uploaded_file(file_id: str, filename: str, source_path: Path) -> StoredAttachment:
    """Move/copy a file into the per-id upload directory and return a StoredAttachment."""
    dest_dir = WEB_UPLOADS_DIR / file_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    if not dest.exists():
        shutil.copyfile(source_path, dest)
    return StoredAttachment(
        id=file_id,
        filename=filename,
        url=f"/api/files/{file_id}",
        mime_type=None,
    )


def resolve_file(file_id: str) -> Path | None:
    """Return the on-disk path for a file_id, or None if not found."""
    if not file_id or "/" in file_id or "\\" in file_id or file_id == "..":
        return None
    dir_path = WEB_UPLOADS_DIR / file_id
    if not dir_path.is_dir():
        return None
    files = [p for p in dir_path.iterdir() if p.is_file()]
    return files[0] if files else None


class WebPlatform:
    """Implements MessagingPlatform for the in-app web chat."""

    name = "web"

    def __init__(self, store: ConversationStore, manager: ConnectionManager):
        self._store = store
        self._manager = manager

    def send(self, conversation_key: str, msg: OutgoingMessage) -> None:
        stored_atts = self._import_outgoing_attachments(msg.attachments)
        stored_msg = self._store.add_message(
            conversation_key,
            role="assistant",
            text=msg.text,
            attachments=stored_atts,
        )
        self._manager.broadcast(
            conversation_key,
            {"type": "message", "message": serialize_message(stored_msg)},
        )

    def set_processing(self, conversation_key: str, message_id: str, on: bool) -> None:
        self._manager.broadcast(
            conversation_key,
            {"type": "processing", "on": on, "message_id": message_id},
        )

    def get_session_id(self, conversation_key: str) -> str | None:
        conv = self._store.get_conversation(conversation_key)
        return conv.claude_session_id if conv else None

    def set_session_id(
        self, conversation_key: str, session_id: str, title_hint: str | None = None
    ) -> None:
        # title_hint is unused: the web flow titles the conversation directly
        # from the first message in web_server.py.
        del title_hint
        self._store.update_conversation(
            conversation_key, claude_session_id=session_id
        )

    def _import_outgoing_attachments(
        self, attachments: Iterable
    ) -> list[StoredAttachment]:
        """Copy Claude-produced files into the served uploads dir."""
        out: list[StoredAttachment] = []
        for att in attachments:
            file_id = uuid.uuid4().hex
            stored = store_uploaded_file(file_id, att.filename, att.local_path)
            out.append(stored)
        return out
