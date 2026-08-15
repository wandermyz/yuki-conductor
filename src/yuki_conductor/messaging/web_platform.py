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

# Upper bound on per-conversation step history kept for WS replay.
MAX_BUFFERED_STEPS = 200


class ConnectionManager:
    """Tracks global WebSocket subscribers and broadcasts conversation events.

    All connected clients receive every conversation event (each payload
    includes ``conversation_id``).  Threads call `broadcast()` from worker
    threads; we marshal the actual `send_json` onto the asyncio loop the
    socket lives on.

    Intermediate Claude steps are also buffered per in-flight conversation so
    a client that connects (or reconnects) mid-run can replay what it missed —
    they are deliberately not persisted, and are dropped when the run ends.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[tuple[object, asyncio.AbstractEventLoop]] = []
        self._processing: dict[str, str] = {}  # conv_id -> message_id
        self._steps: dict[str, list[dict]] = {}  # conv_id -> buffered step events
        self._step_seq: dict[str, int] = {}  # conv_id -> next step sequence number

    def add(self, ws, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._clients.append((ws, loop))

    def remove(self, ws) -> None:
        with self._lock:
            self._clients = [(s, lp) for (s, lp) in self._clients if s is not ws]

    def broadcast(self, conv_id: str, payload: dict) -> None:
        enriched = {**payload, "conversation_id": conv_id}
        with self._lock:
            targets = list(self._clients)
        for ws, loop in targets:
            try:
                asyncio.run_coroutine_threadsafe(ws.send_json(enriched), loop)
            except Exception:
                logger.debug("WS broadcast failed", exc_info=True)

    def broadcast_all(self, payload: dict) -> None:
        """Send a payload to every connected client (no conversation_id)."""
        with self._lock:
            targets = list(self._clients)
        for ws, loop in targets:
            try:
                asyncio.run_coroutine_threadsafe(ws.send_json(payload), loop)
            except Exception:
                logger.debug("WS broadcast_all failed", exc_info=True)

    def set_processing(self, conv_id: str, message_id: str, on: bool) -> None:
        with self._lock:
            if on:
                self._processing[conv_id] = message_id
                self._steps[conv_id] = []
                self._step_seq[conv_id] = 0
            else:
                self._processing.pop(conv_id, None)
                # Steps outlive the run: the finished turn keeps its trace until
                # the next turn on this conversation resets it.

    def get_processing(self) -> dict[str, str]:
        with self._lock:
            return dict(self._processing)

    def add_step(self, conv_id: str, step: dict) -> None:
        """Buffer an intermediate step and broadcast it.

        Each step gets a per-conversation sequence number so a client replaying
        the buffer after a reconnect can drop steps it already rendered.
        """
        with self._lock:
            buf = self._steps.get(conv_id)
            if buf is None:
                # Run already finished (or never started) — nothing to attach to.
                return
            seq = self._step_seq.get(conv_id, 0)
            self._step_seq[conv_id] = seq + 1
            step = {**step, "seq": seq}
            buf.append(step)
            # Long agentic runs emit hundreds of steps; the UI only shows a
            # tail, so keep the buffer bounded rather than growing forever.
            if len(buf) > MAX_BUFFERED_STEPS:
                del buf[: len(buf) - MAX_BUFFERED_STEPS]
        self.broadcast(conv_id, {"type": "step", "step": step})

    def get_steps(self) -> dict[str, list[dict]]:
        """Snapshot of buffered steps for every in-flight conversation."""
        with self._lock:
            return {cid: list(steps) for cid, steps in self._steps.items()}


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
        self._store.record_usage(
            message_id=stored_msg.id,
            conversation_id=conversation_key,
            input_tokens=msg.input_tokens,
            output_tokens=msg.output_tokens,
            cost_usd=msg.cost_usd,
        )
        self._manager.broadcast(
            conversation_key,
            {"type": "message", "message": serialize_message(stored_msg)},
        )

    def set_processing(self, conversation_key: str, message_id: str, on: bool) -> None:
        self._manager.set_processing(conversation_key, message_id, on)
        self._manager.broadcast(
            conversation_key,
            {"type": "processing", "on": on, "message_id": message_id},
        )

    def on_stream_event(self, conversation_key: str, event) -> None:
        """Push an intermediate step to subscribed browsers (not persisted)."""
        self._manager.add_step(conversation_key, event.to_dict())

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

    def send_notification(self, text: str) -> None:
        self._manager.broadcast_all({"type": "notification", "text": text})

    def start_thread(self, text: str, title: str | None = None) -> str:
        """Create a fresh web conversation seeded with `text` as the first
        assistant message, and return its id."""
        conv = self._store.create_conversation(
            platform="web", title=title or text[:80]
        )
        stored = self._store.add_message(
            conv.id, role="assistant", text=text, attachments=[]
        )
        self._manager.broadcast(
            conv.id, {"type": "message", "message": serialize_message(stored)}
        )
        return conv.id

    def reserve_thread(self, title: str | None = None) -> str:
        """Create an empty conversation and return its id.

        Lets a producer (the cron scheduler) know the conversation id *before*
        running Claude, so the run can be told where to push interim messages
        via `yuki-conductor send`. Pair with `discard_thread` to clean up if
        the run turns out to have nothing to say.
        """
        conv = self._store.create_conversation(platform="web", title=title)
        return conv.id

    def discard_thread(self, conversation_key: str) -> bool:
        """Delete a reserved conversation, unless something has posted to it."""
        if self._store.list_messages(conversation_key, limit=1):
            return False
        return self._store.delete_conversation(conversation_key)

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
