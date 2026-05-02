# Web Messaging Platform — Plan

Refactor yuki-conductor's Slack integration into a platform-agnostic messaging core, then add a built-in web messaging UI that talks to the same backend Slack does.

## Goals

1. Decouple Claude conversation handling from Slack so additional messaging surfaces (web, future native mobile, future Chrome extension) can plug in.
2. Add a web-based messaging UI inside the existing FastAPI server: chat-style threads, attachments, live updates — feature-parity with the Slack flow as it exists today (text + `<attachment>` upload/download).
3. Define a stable HTTP + WebSocket API surface that a native iOS/Android client could later consume without changes.
4. Out of scope (this plan): Chrome extension, SSE/streaming partial responses, multi-user auth, mobile client.

## Current State (what we are refactoring away from)

- `slack_app.py` owns everything: Slack-bolt handlers, message routing, `<attachment>` upload/download, "hourglass" reaction, cron output formatting, and the Claude invocation glue. It's the only entry point that turns "an inbound message" into "a Claude run + reply."
- `web_server.py` exposes a read-only REST API (sessions list, title edit, zellij CRUD) plus the zellij terminal WebSocket. It has no concept of "conversation messaging."
- `store.SessionStore` is keyed by Slack `thread_ts`, with `session_type` already extended to `slack` / `zellij`. Conversations have no message log — Slack is the source of truth.
- `cron_scheduler.py` calls `slack_client.chat_postMessage` directly when notifying.

## Target Architecture

```
yuki-conductor/
  src/yuki_conductor/
    messaging/
      __init__.py
      platform.py          — MessagingPlatform protocol + types (Conversation, IncomingMessage, OutgoingMessage, Attachment)
      conversation.py      — handle_incoming_message(): the platform-agnostic core (session resolve, run_claude, attachment plumbing)
      slack_platform.py    — Slack-bolt adapter implementing MessagingPlatform
      web_platform.py      — Web adapter implementing MessagingPlatform
    web_server.py          — adds /api/conversations + /api/messages + /ws/conversations/{id}; keeps existing /api/sessions, zellij endpoints
    cron_scheduler.py      — switches to MessagingPlatform.send() (so cron can also notify into the web app, not only Slack)
    store.py               — adds messages table; conversations get a stable internal id independent of platform
    slack_app.py           — thin wrapper: wires Slack-bolt events into slack_platform.py
  web/src/
    App.tsx                — adds top-level routing: "Sessions" (existing) | "Chat" (new)
    chat/
      ChatList.tsx         — left rail: conversations
      ChatThread.tsx       — message list + composer
      Composer.tsx         — text + file picker + send
      Message.tsx          — markdown render + inline attachments
      api.ts               — typed client for /api/conversations, /api/messages, ws
```

### The `MessagingPlatform` abstraction

Three operations every platform must support, plus typed payloads. Async is fine for both Slack-bolt (which is sync but threaded) and FastAPI handlers — we'll keep `conversation.py` sync since `run_claude` is blocking subprocess; platforms wrap it in their own threading model.

```python
@dataclass
class Attachment:
    filename: str
    local_path: Path           # already on disk (downloaded for inbound, generated for outbound)
    mime_type: str | None

@dataclass
class IncomingMessage:
    platform: str              # "slack" | "web"
    conversation_key: str      # platform-stable thread id (slack thread_ts; web conversation uuid)
    message_id: str            # platform-stable message id
    text: str
    attachments: list[Attachment]
    is_thread_start: bool      # if True, create a new Claude session

@dataclass
class OutgoingMessage:
    text: str
    attachments: list[Attachment]

class MessagingPlatform(Protocol):
    name: str                  # "slack" | "web"
    def send(self, conversation_key: str, msg: OutgoingMessage) -> None: ...
    def set_processing(self, conversation_key: str, message_id: str, on: bool) -> None: ...
```

### The shared core: `conversation.handle_incoming_message`

Single function called by both platforms. Replaces the body of `slack_app.handle_message`:

```
def handle_incoming_message(platform: MessagingPlatform, msg: IncomingMessage) -> None:
    1. set_processing(on)
    2. resolve session_id from SessionStore (or None for thread start)
    3. compose prompt: prepend <attachment>{path}</attachment> for each inbound file
    4. result = run_claude(prompt, session_id=session_id, model=...)
    5. persist new session_id; persist title on first message
    6. parse <attachment> tags out of result.text; resolve to Attachment objects
    7. platform.send(conversation_key, OutgoingMessage(text, attachments))
    8. set_processing(off)
```

This is the only code that knows about Claude, attachments, and session storage. Everything Slack-specific (mrkdwn formatting, reactions, file upload via `client.files_upload_v2`) lives behind `SlackPlatform.send`. Everything web-specific (WebSocket fan-out, persisting messages to DB, serving uploaded files) lives behind `WebPlatform.send`.

### Why this split (vs. keeping Slack as the only first-class platform)

- The Slack code path mixes three concerns today (transport, Claude orchestration, attachment lifecycle); pulling them apart is the only way `cron_scheduler` and a future mobile client can share the orchestration layer without copy-paste.
- A `Platform` protocol is cheap — two implementations is enough to validate it; we are not building a generic chat-bot framework.

## Backend API (for web + future mobile)

All under `/api`. JSON in/out unless noted. Designed so a native client can drive a conversation without ever touching the React UI.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/conversations` | List web conversations (filter `?platform=web`); returns id, title, last message preview, updated_at |
| `POST` | `/api/conversations` | Create a new web conversation. Body: `{title?: string, project?: string}`. Returns full Conversation. |
| `GET` | `/api/conversations/{id}` | Conversation metadata + last N messages |
| `PATCH` | `/api/conversations/{id}` | Update title/project |
| `DELETE` | `/api/conversations/{id}` | Delete conversation + its messages |
| `GET` | `/api/conversations/{id}/messages?before=&limit=` | Paginated message history |
| `POST` | `/api/conversations/{id}/messages` | Send a user message. Body: `{text: string, attachment_ids: string[]}`. Returns the persisted user message immediately; Claude reply arrives over WS. |
| `POST` | `/api/uploads` | Multipart upload. Returns `{id, filename, url, mime_type}`. Files saved under `workspace/uploads/web/{conversation_id}/`. |
| `GET` | `/api/files/{id}` | Download an uploaded *or* Claude-produced file (auth-scoped to its conversation). |
| `WS` | `/ws/conversations/{id}` | Live channel: `{type: "message", message: ...}`, `{type: "processing", on: bool}`, `{type: "title", title: ...}` |

Notes:
- Message-send is split from Claude-reply: HTTP returns 201 with the user message; the assistant reply (and any produced attachments) is delivered as a WS event. This is the same shape a mobile app will want and lets us add streaming later without breaking the wire format.
- `/ws/conversations/{id}` is per-conversation; the existing `/ws/terminal/{key}` is unchanged.
- File IDs are opaque server-issued UUIDs, not paths — keeps us free to move storage later (S3, etc.) without API churn.

## Storage Changes

Add two tables. Keep `sessions` (legacy) intact for now to avoid touching the Slack/zellij path mid-refactor; new code reads/writes via a `ConversationStore` that abstracts over both.

```sql
CREATE TABLE conversations (
  id TEXT PRIMARY KEY,         -- uuid for web; "slack:{thread_ts}" mirror for slack convs (optional, deferred)
  platform TEXT NOT NULL,      -- "web" | "slack"
  title TEXT,
  project TEXT,
  claude_session_id TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE messages (
  id TEXT PRIMARY KEY,         -- uuid
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  role TEXT NOT NULL,          -- "user" | "assistant"
  text TEXT NOT NULL,
  attachments_json TEXT,       -- JSON array of {id, filename, url, mime_type}
  created_at REAL NOT NULL
);
CREATE INDEX idx_messages_conv_created ON messages(conversation_id, created_at);
```

Slack conversations stay in `sessions` for this pass — only the web platform persists messages to the new tables. (The Slack source of truth is Slack itself; persisting messages there is a separate, larger migration.)

## Frontend (web/)

- New top-level route in `App.tsx`: tab switcher between **Sessions** (existing zellij + Slack list) and **Chat** (new).
- **Chat** view: two-pane.
  - Left: conversation list, "+ New Chat" button at top.
  - Right: messages (assistant bubbles with markdown via `react-markdown` + `remark-gfm`; user bubbles plain), composer at bottom with textarea + paperclip-style file picker.
  - Image attachments render inline; non-image attachments render as download links.
  - "Claude is thinking…" indicator driven by `processing` WS events.
- WebSocket reconnect with exponential backoff; on reconnect, re-fetch messages since last seen `created_at`.
- Reuses the existing Vite proxy (`/api` → port 2333). No new tooling.

## Refactor Steps (in order)

1. **Introduce `messaging/` package with types only.** No behavior change. Ensures the new types compile and SlackPlatform/WebPlatform have a target.
2. **Extract `SlackPlatform`.** Move `_download_slack_files`, `_extract_and_upload_attachments`, mrkdwn formatting, and reaction add/remove into `slack_platform.py`. `slack_app.py` becomes a thin Bolt-handler wrapper that builds an `IncomingMessage` and calls `conversation.handle_incoming_message`. Verified by: existing Slack flow keeps working end-to-end (manual smoke test).
3. **Add `ConversationStore` and migrate schema.** Auto-migrate on init (same pattern as existing `_init_table`). New tables only — no changes to `sessions`.
4. **Add `WebPlatform` + new endpoints.** Wire `/api/conversations`, `/api/messages`, `/api/uploads`, `/api/files/{id}`, `/ws/conversations/{id}` into `web_server.py`. `WebPlatform.send` writes the assistant message to DB and broadcasts on the conversation's WS topic. `set_processing` broadcasts `{type: "processing"}`.
5. **Build the Chat UI.** New `web/src/chat/` module; add the tab switcher to `App.tsx`. Use minimal styling consistent with existing app.
6. **Switch `cron_scheduler` to `MessagingPlatform.send`.** A cron task gets a configured "destination platform" (defaults to slack for back-compat). Lets cron also notify into a web conversation if desired.
7. **Tests.**
   - Unit: `ConversationStore` roundtrip; `conversation.handle_incoming_message` with a stub platform (asserts it called `run_claude`, persisted session, called `platform.send` with parsed attachments).
   - Integration: `tests/test_web_messaging.py` — POST /messages → poll WS → assert assistant message received. Use FastAPI `TestClient` with `claude_runner.run_claude` monkeypatched.
   - Existing tests must still pass.

## Open Questions

- **Auth.** Today the web server is unauthenticated and bound to `0.0.0.0`. For mobile, we'll need at least a shared-secret token. Suggest deferring to a follow-up: add an optional `WEB_AUTH_TOKEN` env var; when set, all `/api/*` and `/ws/*` require `Authorization: Bearer <token>`. Decision needed before mobile work starts; web-on-localhost can ship without it.
- **Streaming.** The current `claude_runner.run_claude` is a blocking subprocess that returns one `ClaudeResult`. Real streaming requires switching to `--output-format stream-json` and re-architecting `run_claude`. Out of scope here; the WS contract already accommodates a future `{type: "delta"}` event.
- **Slack conversation persistence.** Should Slack conversations also be mirrored into the new `messages` table so the web UI can browse them? Defer — the Sessions tab already lists them, and double-writing is non-trivial.

## Risk / Rollback

- The refactor in steps 1–2 is the riskiest part because it touches the live Slack handler. Mitigation: keep `slack_app.py` exporting the same `start()` and `create_app()` symbols; swap internals only. The `feat/slack-mode-config` work already proved we can change the daemon entry without breaking LaunchAgent.
- If the new web endpoints conflict with existing routes, fall back is "remove the new router import" — the new code lives in its own module.
