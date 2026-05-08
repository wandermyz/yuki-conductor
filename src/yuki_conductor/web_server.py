"""FastAPI HTTP server for the agent conductor web UI."""

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from yuki_conductor import zellij_manager
from yuki_conductor.config import WEB_UPLOADS_DIR
from yuki_conductor.conversation_store import ConversationStore
from yuki_conductor.messaging import Attachment, IncomingMessage, handle_incoming_message
from yuki_conductor.messaging.web_platform import (
    ConnectionManager,
    WebPlatform,
    resolve_file,
    serialize_message,
)
from yuki_conductor.store import SessionStore

logger = logging.getLogger(__name__)

WEB_PORT = int(os.environ.get("WEB_PORT", "2333"))
SLACK_WORKSPACE = os.environ.get("SLACK_WORKSPACE", "wandermyz")

# Resolve path to the built frontend assets
_WEB_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"

store = SessionStore()
conv_store = ConversationStore()
ws_manager = ConnectionManager()


def _slack_thread_url(channel_id: str, thread_ts: str) -> str:
    ts_no_dot = thread_ts.replace(".", "")
    return f"https://{SLACK_WORKSPACE}.slack.com/archives/{channel_id}/p{ts_no_dot}"


def create_api() -> FastAPI:
    api = FastAPI(title="Agent Conductor")

    @api.get("/api/sessions")
    def list_sessions(days: int = Query(default=7, ge=1, le=365)):
        cutoff = time.time() - days * 86400
        sessions = store.list_all()
        alive_sessions = set(zellij_manager.list_sessions())
        result = []
        for s in sessions:
            # Zellij sessions use "zellij:<name>" keys — always include them
            if s["session_type"] == "zellij":
                s["slack_url"] = None
                s["alive"] = s["session_id"] in alive_sessions
                result.append(s)
                continue
            # Teams sessions: include all (key is "teams:<msg_id>")
            if s["session_type"] == "teams_cli":
                s["slack_url"] = None
                s["alive"] = False
                result.append(s)
                continue
            # Slack sessions: filter by timestamp age
            try:
                ts_float = float(s["thread_ts"])
            except (ValueError, TypeError):
                continue
            if ts_float < cutoff:
                continue
            if s["channel_id"]:
                s["slack_url"] = _slack_thread_url(s["channel_id"], s["thread_ts"])
            else:
                s["slack_url"] = None
            result.append(s)
        result.sort(key=lambda s: s["thread_ts"], reverse=True)
        return result

    class TitleUpdate(BaseModel):
        title: str

    @api.patch("/api/sessions/{thread_ts:path}")
    def update_session_title(thread_ts: str, body: TitleUpdate):
        if not store.get(thread_ts):
            raise HTTPException(status_code=404, detail="Session not found")
        store.set_title(thread_ts, body.title)
        return {"ok": True, "title": body.title}

    class ZellijSessionCreate(BaseModel):
        name: str
        project: str
        worktree: str

    @api.post("/api/sessions/zellij")
    def create_zellij_session(body: ZellijSessionCreate):
        zellij_manager.create_session(body.name, body.worktree)
        key = store.create_zellij_session(
            zellij_session_name=body.name,
            title=body.name,
            project=body.project,
        )
        return {
            "thread_ts": key,
            "session_id": body.name,
            "session_type": "zellij",
            "project": body.project,
            "title": body.name,
        }

    @api.delete("/api/sessions/zellij/{session_key:path}")
    def delete_zellij_session(session_key: str):
        """Kill the Zellij process but keep the session record."""
        if not session_key.startswith("zellij:"):
            raise HTTPException(status_code=400, detail="Not a Zellij session")
        zellij_name = session_key[len("zellij:"):]
        zellij_manager.kill_session(zellij_name)
        return {"ok": True}

    @api.post("/api/sessions/zellij/{session_key:path}/reopen")
    def reopen_zellij_session(session_key: str):
        """Reopen a stopped Zellij session with claude --continue."""
        if not session_key.startswith("zellij:"):
            raise HTTPException(status_code=400, detail="Not a Zellij session")
        zellij_name = session_key[len("zellij:"):]
        zellij_manager.create_session(zellij_name, zellij_name, resume=True)
        return {"ok": True, "alive": True}

    @api.delete("/api/sessions/{thread_ts:path}")
    def delete_session(thread_ts: str):
        if not store.delete_session(thread_ts):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"ok": True}

    if sys.platform == "win32":
        @api.websocket("/ws/terminal/{session_key:path}")
        async def terminal_websocket(websocket: WebSocket, session_key: str):
            await websocket.close(code=1011, reason="Terminal not supported on Windows")
    else:
        from yuki_conductor.pty_bridge import register_terminal_ws

        register_terminal_ws(api)

    # ── Web messaging endpoints ────────────────────────────────────────────

    class ConversationCreate(BaseModel):
        title: str | None = None
        project: str | None = None

    class ConversationUpdate(BaseModel):
        title: str | None = None
        project: str | None = None

    class MessageCreate(BaseModel):
        text: str = ""
        attachment_ids: list[str] = []
        model: str | None = None

    def _conv_to_dict(c) -> dict:
        return {
            "id": c.id,
            "platform": c.platform,
            "title": c.title,
            "project": c.project,
            "claude_session_id": c.claude_session_id,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
        }

    @api.get("/api/conversations")
    def list_conversations(platform: str | None = Query(default="web")):
        return [_conv_to_dict(c) for c in conv_store.list_conversations(platform=platform)]

    @api.post("/api/conversations")
    def create_conversation(body: ConversationCreate):
        conv = conv_store.create_conversation(
            platform="web", title=body.title, project=body.project
        )
        return _conv_to_dict(conv)

    @api.get("/api/conversations/{conv_id}")
    def get_conversation(conv_id: str):
        conv = conv_store.get_conversation(conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return _conv_to_dict(conv)

    @api.patch("/api/conversations/{conv_id}")
    def update_conversation(conv_id: str, body: ConversationUpdate):
        if not conv_store.update_conversation(
            conv_id, title=body.title, project=body.project
        ):
            raise HTTPException(status_code=404, detail="Conversation not found")
        # Mirror title into the legacy session store so the Sessions tab matches.
        if body.title and store.get(conv_id) is not None:
            store.set_title(conv_id, body.title)
        return _conv_to_dict(conv_store.get_conversation(conv_id))

    @api.delete("/api/conversations/{conv_id}")
    def delete_conversation(conv_id: str):
        if not conv_store.delete_conversation(conv_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
        store.delete_session(conv_id)
        return {"ok": True}

    @api.get("/api/conversations/{conv_id}/messages")
    def list_messages(
        conv_id: str,
        before: float | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        if not conv_store.get_conversation(conv_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
        msgs = conv_store.list_messages(conv_id, before=before, limit=limit)
        return [serialize_message(m) for m in msgs]

    @api.post("/api/conversations/{conv_id}/messages", status_code=201)
    def post_message(conv_id: str, body: MessageCreate):
        conv = conv_store.get_conversation(conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if not body.text.strip() and not body.attachment_ids:
            raise HTTPException(status_code=400, detail="Empty message")

        # Resolve uploaded files into Attachment objects for Claude.
        incoming_attachments: list[Attachment] = []
        stored_atts = []
        for fid in body.attachment_ids:
            path = resolve_file(fid)
            if path is None:
                raise HTTPException(status_code=400, detail=f"Unknown attachment id {fid}")
            incoming_attachments.append(Attachment(filename=path.name, local_path=path))
            stored_atts.append(
                {"id": fid, "filename": path.name, "url": f"/api/files/{fid}", "mime_type": None}
            )

        # Persist the user message immediately and broadcast it.
        from yuki_conductor.conversation_store import StoredAttachment

        user_msg = conv_store.add_message(
            conv_id,
            role="user",
            text=body.text,
            attachments=[StoredAttachment(**a) for a in stored_atts],
        )
        ws_manager.broadcast(
            conv_id, {"type": "message", "message": serialize_message(user_msg)}
        )

        # Auto-title the conversation from the first message.
        is_thread_start = conv.claude_session_id is None
        if is_thread_start and not conv.title and body.text.strip():
            title = body.text.strip().splitlines()[0][:80]
            conv_store.update_conversation(conv_id, title=title)
            ws_manager.broadcast(conv_id, {"type": "title", "title": title})

        platform = WebPlatform(conv_store, ws_manager)
        msg = IncomingMessage(
            platform="web",
            conversation_key=conv_id,
            message_id=user_msg.id,
            text=body.text,
            attachments=incoming_attachments,
            is_thread_start=is_thread_start,
            title_hint=user_msg.text[:80] if is_thread_start else None,
            model=body.model,
        )

        threading.Thread(
            target=handle_incoming_message,
            args=(platform, msg),
            name=f"web-msg-{user_msg.id}",
            daemon=True,
        ).start()

        return serialize_message(user_msg)

    _upload_default = File(...)

    @api.post("/api/uploads")
    async def upload_file(file: UploadFile = _upload_default):
        import uuid

        file_id = uuid.uuid4().hex
        dest_dir = WEB_UPLOADS_DIR / file_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / (file.filename or "upload.bin")
        with open(dest, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)
        return {
            "id": file_id,
            "filename": dest.name,
            "url": f"/api/files/{file_id}",
            "mime_type": file.content_type,
        }

    @api.get("/api/files/{file_id}")
    def get_file(file_id: str):
        path = resolve_file(file_id)
        if path is None:
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(
            str(path),
            filename=path.name,
            content_disposition_type="inline",
        )

    @api.websocket("/ws/conversations/{conv_id}")
    async def conversation_ws(websocket: WebSocket, conv_id: str):
        if not conv_store.get_conversation(conv_id):
            await websocket.close(code=1008, reason="Unknown conversation")
            return
        await websocket.accept()
        loop = asyncio.get_event_loop()
        ws_manager.add(conv_id, websocket, loop)
        try:
            while True:
                # We don't expect inbound traffic on this socket; just keep it open.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            ws_manager.remove(conv_id, websocket)

    # Serve the React frontend (if built)
    if _WEB_DIST.is_dir():
        api.mount("/", StaticFiles(directory=str(_WEB_DIST), html=True), name="static")

    return api


# Module-level app for `uvicorn yuki_conductor.web_server:app`
app = create_api()


def start_web_server() -> None:
    """Start the web server in a daemon thread (non-blocking)."""
    app = create_api()

    def _run():
        uvicorn.run(app, host="0.0.0.0", port=WEB_PORT, log_level="info")

    thread = threading.Thread(target=_run, daemon=True, name="web-server")
    thread.start()
    logger.info(f"Web server started on port {WEB_PORT}")
