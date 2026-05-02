"""SQLite store for web-platform conversations and messages.

Slack conversations stay in the legacy `sessions` table; only the web
chat persists structured messages here. The web UI is the only consumer
that needs scrollback.
"""

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from yuki_conductor.config import DB_FILE


@dataclass
class StoredAttachment:
    id: str
    filename: str
    url: str
    mime_type: str | None = None


@dataclass
class StoredMessage:
    id: str
    conversation_id: str
    role: str  # "user" | "assistant"
    text: str
    attachments: list[StoredAttachment] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class StoredConversation:
    id: str
    platform: str
    title: str | None
    project: str | None
    claude_session_id: str | None
    created_at: float
    updated_at: float


class ConversationStore:
    """Web-platform conversations + their message scrollback."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or DB_FILE
        self._lock = threading.Lock()
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self._db_path))

    def _init_tables(self) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conversations (
                        id TEXT PRIMARY KEY,
                        platform TEXT NOT NULL,
                        title TEXT,
                        project TEXT,
                        claude_session_id TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL REFERENCES conversations(id),
                        role TEXT NOT NULL,
                        text TEXT NOT NULL,
                        attachments_json TEXT,
                        created_at REAL NOT NULL
                    )
                    """
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_conv_created "
                    "ON messages(conversation_id, created_at)"
                )
                con.commit()
            finally:
                con.close()

    def create_conversation(
        self,
        platform: str = "web",
        title: str | None = None,
        project: str | None = None,
    ) -> StoredConversation:
        now = time.time()
        conv = StoredConversation(
            id=uuid.uuid4().hex,
            platform=platform,
            title=title,
            project=project,
            claude_session_id=None,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "INSERT INTO conversations "
                    "(id, platform, title, project, claude_session_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        conv.id,
                        conv.platform,
                        conv.title,
                        conv.project,
                        conv.claude_session_id,
                        conv.created_at,
                        conv.updated_at,
                    ),
                )
                con.commit()
            finally:
                con.close()
        return conv

    def get_conversation(self, conv_id: str) -> StoredConversation | None:
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    "SELECT id, platform, title, project, claude_session_id, "
                    "created_at, updated_at FROM conversations WHERE id = ?",
                    (conv_id,),
                ).fetchone()
                if not row:
                    return None
                return StoredConversation(*row)
            finally:
                con.close()

    def list_conversations(self, platform: str | None = None) -> list[StoredConversation]:
        with self._lock:
            con = self._connect()
            try:
                if platform is not None:
                    rows = con.execute(
                        "SELECT id, platform, title, project, claude_session_id, "
                        "created_at, updated_at FROM conversations WHERE platform = ? "
                        "ORDER BY updated_at DESC",
                        (platform,),
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT id, platform, title, project, claude_session_id, "
                        "created_at, updated_at FROM conversations ORDER BY updated_at DESC"
                    ).fetchall()
                return [StoredConversation(*r) for r in rows]
            finally:
                con.close()

    def update_conversation(
        self,
        conv_id: str,
        *,
        title: str | None = None,
        project: str | None = None,
        claude_session_id: str | None = None,
    ) -> bool:
        sets = []
        args: list[object] = []
        if title is not None:
            sets.append("title = ?")
            args.append(title)
        if project is not None:
            sets.append("project = ?")
            args.append(project)
        if claude_session_id is not None:
            sets.append("claude_session_id = ?")
            args.append(claude_session_id)
        if not sets:
            return False
        sets.append("updated_at = ?")
        args.append(time.time())
        args.append(conv_id)
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    f"UPDATE conversations SET {', '.join(sets)} WHERE id = ?",
                    tuple(args),
                )
                con.commit()
                return cur.rowcount > 0
            finally:
                con.close()

    def delete_conversation(self, conv_id: str) -> bool:
        with self._lock:
            con = self._connect()
            try:
                con.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
                cur = con.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
                con.commit()
                return cur.rowcount > 0
            finally:
                con.close()

    def add_message(
        self,
        conv_id: str,
        role: str,
        text: str,
        attachments: list[StoredAttachment] | None = None,
    ) -> StoredMessage:
        msg = StoredMessage(
            id=uuid.uuid4().hex,
            conversation_id=conv_id,
            role=role,
            text=text,
            attachments=attachments or [],
            created_at=time.time(),
        )
        attachments_json = json.dumps(
            [
                {
                    "id": a.id,
                    "filename": a.filename,
                    "url": a.url,
                    "mime_type": a.mime_type,
                }
                for a in msg.attachments
            ]
        )
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "INSERT INTO messages "
                    "(id, conversation_id, role, text, attachments_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (msg.id, conv_id, role, text, attachments_json, msg.created_at),
                )
                con.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (msg.created_at, conv_id),
                )
                con.commit()
            finally:
                con.close()
        return msg

    def list_messages(
        self,
        conv_id: str,
        before: float | None = None,
        limit: int = 100,
    ) -> list[StoredMessage]:
        with self._lock:
            con = self._connect()
            try:
                if before is not None:
                    rows = con.execute(
                        "SELECT id, conversation_id, role, text, attachments_json, created_at "
                        "FROM messages WHERE conversation_id = ? AND created_at < ? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (conv_id, before, limit),
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT id, conversation_id, role, text, attachments_json, created_at "
                        "FROM messages WHERE conversation_id = ? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (conv_id, limit),
                    ).fetchall()
                msgs: list[StoredMessage] = []
                for r in rows:
                    raw = json.loads(r[4]) if r[4] else []
                    atts = [
                        StoredAttachment(
                            id=a["id"],
                            filename=a["filename"],
                            url=a["url"],
                            mime_type=a.get("mime_type"),
                        )
                        for a in raw
                    ]
                    msgs.append(
                        StoredMessage(
                            id=r[0],
                            conversation_id=r[1],
                            role=r[2],
                            text=r[3],
                            attachments=atts,
                            created_at=r[5],
                        )
                    )
                msgs.reverse()  # ascending by time
                return msgs
            finally:
                con.close()
