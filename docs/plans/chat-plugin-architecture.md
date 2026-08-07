# Chat Platform Plugin Architecture

## Problem

yuki-conductor currently has two chat platform implementations: a working Slack integration and a placeholder external-CLI adapter. Supporting new platforms — especially company-internal ones backed by MCP servers — requires a design where:

1. Platform-specific implementation details live in **separate repositories**, not in yuki-conductor.
2. Plugins can wrap an MCP server process and expose it as a standard chat platform.
3. Both push-based (like Slack Socket Mode) and pull-based (polling) message delivery models are supported.

## Current Architecture

```
conversation.py (orchestrator)
    ↕ uses MessagingPlatform protocol
    ├── SlackPlatform       (built-in)
    ├── ExternalCliPlatform (built-in, placeholder)
    └── WebPlatform         (built-in, always on)
```

### Current `MessagingPlatform` protocol

| Method | Description |
|--------|-------------|
| `name: str` | Platform identifier |
| `send(conversation_key, msg)` | Post message + attachments to a thread |
| `set_processing(conversation_key, message_id, on)` | Toggle processing indicator (e.g., Slack reaction) |
| `get_session_id(conversation_key)` | Retrieve Claude session ID for resume |
| `set_session_id(conversation_key, session_id, title_hint)` | Persist session ID |
| `start_thread(text, title)` | Create new conversation (for cron), return key |

### Current `ChatAppReceiver` protocol

| Method | Description |
|--------|-------------|
| `name: str` | Receiver identifier |
| `platform: MessagingPlatform` | Associated platform instance |
| `start()` | Non-blocking initialization; spawns listener threads |

### What Slack actually does today (gap analysis baseline)

The Slack integration exercises **every** protocol method plus significant functionality that is *not* captured by the current protocols:

**Covered by the protocol:**
- `send()` → `chat_postMessage` + `files_upload_v2` (text + file attachments in thread)
- `set_processing()` → `reactions_add`/`reactions_remove` (⏳ on user's message)
- `get/set_session_id()` → SessionStore keyed by `thread_ts`
- `start_thread()` → `chat_postMessage` to cron channel, store session row

**NOT covered by the protocol — Slack-specific features that a plugin must also support:**

| Feature | Current implementation | Gap |
|---------|----------------------|-----|
| **Slash commands** (`/yuki-model`, `/yuki-usage`, `/yuki-title`, `/yuki-*`) | Registered via `@app.command()` decorators in `slack_app.py` | No plugin command registration mechanism |
| **Model selection per channel** | `/yuki-model` reads/writes `ModelStore`; model passed in `IncomingMessage` | Model store is Slack-channel-scoped; plugins need equivalent |
| **Title rename** | `!title <new>` in-thread parsed by receiver, calls `store.set_title()` | Receiver-side logic, not in protocol |
| **Restart notification** | `_post_restart_notification()` posts to `SLACK_APP_DM_CHANNEL` on boot | No lifecycle hook for plugins |
| **Connection watchdog** | Monitors `BrokenPipeError`, exits after 10 in 120s | No health/reconnect contract |
| **Inbound attachment download** | `download_slack_files()` — auth'd HTTP download from Slack URLs to `UPLOADS_DIR` | Protocol has no inbound-attachment hook; receiver handles it pre-dispatch |
| **Outbound attachment upload** | `files_upload_v2()` per attachment in `send()` | Covered by `send()`, but attachment transfer mechanism unspecified for MCP |
| **Message filtering** | Only processes `subtype=None` or `file_share`; ignores edits, deletes | Receiver-side; plugins must handle their own filtering |
| **Cross-platform session guard** | Checks `session_type` to reject replies in threads owned by another platform | Receiver-side validation |
| **Markdown → mrkdwn conversion** | `markdown_to_mrkdwn()` applied before all Slack sends | Platform-specific formatting is the plugin's responsibility |
| **Usage statistics** | `/yuki-usage` aggregates from SessionStore | Could be shared utility, but delivery is platform-specific |

## Proposed Architecture

### Overview

```
conversation.py (orchestrator, unchanged)
    ↕ uses MessagingPlatform protocol (extended)
    ├── SlackPlatform           (built-in)
    ├── WebPlatform             (built-in, always on)
    ├── McpBridgePlatform       (built-in generic bridge)
    │     ↕ MCP client (stdio)
    │     └── external-mcp-server  (installed separately, e.g. chat-mcp-server)
    └── [any future plugin via entry point]
```

### Design Decisions

**1. Plugin discovery via Python entry points**

Plugins register themselves using the standard `[project.entry-points]` mechanism:

```toml
# In the external plugin's pyproject.toml
[project.entry-points."yuki_conductor.chat_plugins"]
example_mcp = "example_mcp_plugin:create_receiver"
```

yuki-conductor discovers plugins at startup by iterating `importlib.metadata.entry_points(group="yuki_conductor.chat_plugins")`. Each entry point resolves to a factory function:

```python
def create_receiver(store: SessionStore, model_store: ModelStore) -> ChatAppReceiver:
    ...
```

This means:
- No source code from the plugin needs to exist in this repo.
- Users install the plugin package into the same environment (`uv pip install example-mcp-plugin`).
- `CHAT_APPS=example_mcp` activates the plugin by matching the entry point name.

**2. `CHAT_APPS` becomes open-ended**

Currently `ChatApp` is a `StrEnum` with fixed members. Change this:

- `slack_socket` and `web` remain as known built-in values.
- Any other value in `CHAT_APPS` is looked up in the entry point registry.
- Remove `teams_cli` as a built-in. It becomes the first external plugin.- Unknown names (not built-in, not in registry) raise a clear startup error.

```python
# config.py
BUILTIN_CHAT_APPS = {"slack_socket", "web"}

def chat_apps() -> set[str]:
    raw = os.environ.get("CHAT_APPS", "slack_socket")
    if raw.strip().lower() in ("", "none"):
        return set()
    return {name.strip() for name in raw.split(",")}
```

```python
# runtime.py
def _build_receiver(name: str, store: SessionStore, model_store: ModelStore) -> ChatAppReceiver:
    if name == "slack_socket":
        return SlackSocketReceiver()
    # Check entry points
    eps = entry_points(group="yuki_conductor.chat_plugins")
    for ep in eps:
        if ep.name == name:
            factory = ep.load()
            return factory(store, model_store)
    raise ValueError(f"Unknown chat app: {name!r}")
```

**3. Extended plugin contract (beyond the protocol)**

The `MessagingPlatform` and `ChatAppReceiver` protocols are sufficient for the core message loop. However, the Slack audit reveals additional responsibilities that plugins must handle. Rather than bloating the protocol, we define these as **conventions that plugin authors must implement in their receiver**:

**3a. Inbound attachment handling**

The receiver is responsible for downloading platform-specific attachments to local disk before dispatching `IncomingMessage`. This is how Slack does it today (`download_slack_files()` runs before `handle_incoming_message()`), and the pattern works: `conversation.py` only sees local `Attachment` objects with `local_path` on disk.

For MCP-based plugins, the MCP server must download attachments and return local paths. The `poll_messages` / `new_message` payloads include attachments as local paths, not URLs.

**3b. Outbound attachment handling**

`conversation.py` resolves Claude's `<attachment>filename</attachment>` tags to local files in `ATTACHMENTS_DIR` and passes `Attachment(local_path=...)` to `platform.send()`. The platform is responsible for uploading these to the chat service.

For MCP-based plugins, the `send_message` tool call includes attachment local paths. The MCP server reads the files and uploads them to the platform.

**3c. Attachment transfer over MCP (detail)**

Since the MCP server runs as a local subprocess, it shares the filesystem with yuki-conductor. Attachments are passed as absolute file paths, not streamed over the MCP protocol:

```
Inbound:  MCP server downloads file → writes to UPLOADS_DIR → returns path in poll_messages
Outbound: conversation.py writes to ATTACHMENTS_DIR → path passed in send_message → MCP server reads & uploads
```

Both `UPLOADS_DIR` and `ATTACHMENTS_DIR` paths are passed to the MCP server as initialization parameters (via `initialize` request or env vars).

**3d. Message formatting**

Each platform has its own markup dialect (Slack mrkdwn, Teams HTML/adaptive cards, etc.). Formatting conversion is the plugin's responsibility. `conversation.py` produces standard Markdown; `platform.send()` must convert as needed. Today Slack does this via `markdown_to_mrkdwn()` inside `SlackPlatform.send()`.

**3e. Message filtering and deduplication**

The receiver must filter out irrelevant messages (edits, deletes, bot self-replies) before dispatching. Slack filters on `subtype`; polling-based plugins must deduplicate via seen-message tracking. This stays in the receiver, not the protocol.

**3f. Cross-platform session guard**

When a thread is owned by one platform (stored `session_type`), other platforms must not inject messages into it. Today Slack checks `store.get_session_type()` in the receiver. Plugins must do the same. The `SessionStore` already supports this — plugins just need to check before dispatching.

**4. MCP Bridge: a generic built-in adapter**

For MCP-based plugins, provide a built-in `McpBridgePlatform` + `McpBridgeReceiver` that:

- Launches an MCP server as a subprocess (stdio transport).
- Communicates using standard MCP tool calls to send/receive messages.
- Handles both push (server-initiated notifications) and pull (periodic `poll_messages` tool calls).

The MCP server is the plugin's responsibility to implement. yuki-conductor only cares about the MCP tool contract.

**Required MCP tools (server must implement):**

| Tool | Args | Returns | Description |
|------|------|---------|-------------|
| `send_message` | `conversation_key: str`, `text: str`, `attachments: [{filename, local_path}]` | `void` | Send a message to a conversation. For platforms with threads, this replies in-thread. |
| `set_processing` | `conversation_key: str`, `message_id: str`, `on: bool` | `void` | Toggle typing/processing indicator. May be a no-op. |
| `start_thread` | `channel: str`, `text: str`, `title: str?` | `{conversation_key: str}` | Create a new top-level thread (for cron). Returns the key for future replies. |
| `poll_messages` | — | `{messages: IncomingMessage[]}` | (Pull model) Return new messages since last poll. Each message includes `conversation_key`, `message_id`, `text`, `is_thread_start`, `attachments: [{filename, local_path, mime_type}]`. |
| `download_attachment` | `platform_url: str`, `dest_path: str` | `{local_path: str, mime_type: str?}` | Download a platform-specific file to local disk. Called by bridge when `poll_messages` returns attachments with `platform_url` instead of `local_path`. |

**Optional MCP notifications (push model):**

| Notification | Payload | Description |
|--------------|---------|-------------|
| `new_message` | `IncomingMessage` schema | Server pushes a new incoming message. Payload: `{conversation_key, message_id, text, is_thread_start, title_hint?, attachments: [{filename, local_path, mime_type}]}` |

The bridge auto-detects the delivery model: if the server sends `new_message` notifications, it operates in push mode; otherwise it falls back to polling `poll_messages` on an interval.

**MCP Bridge configuration** lives in the plugin's entry point factory or in `~/.yuki-conductor/.env`:

```env
# Example: external chat plugin configured via env
CHAT_APPS=example_mcp
EXAMPLE_MCP_SERVER_CMD=example-mcp-server --team-id ABC --channel-id XYZ
```

The plugin's `create_receiver` factory reads its own env vars and returns a configured `McpBridgeReceiver`:

```python
# In the external example-mcp-plugin package
from yuki_conductor.messaging.mcp_bridge import McpBridgeReceiver

def create_receiver(store, model_store):
    return McpBridgeReceiver(
        name="example_mcp",
        server_cmd=os.environ["EXAMPLE_MCP_SERVER_CMD"].split(),
        store=store,
        poll_interval=5,
    )
```

**5. Session tracking stays in yuki-conductor**

The `SessionStore` and `ModelStore` are passed to plugin factories. Plugins use them (or delegate to `McpBridgePlatform` which uses them) for session and model persistence. This keeps the databases centralized and avoids requiring plugins to implement their own storage.

**6. Lifecycle hooks**

Add optional lifecycle methods to `ChatAppReceiver`:

```python
class ChatAppReceiver(Protocol):
    name: str
    platform: MessagingPlatform
    def start(self) -> None: ...
    def on_startup_complete(self) -> None: ...   # Called after all receivers started
    def stop(self) -> None: ...                  # Graceful shutdown
```

- `on_startup_complete()` — plugins can post restart notifications (like Slack's `_post_restart_notification()`).
- `stop()` — graceful shutdown for MCP subprocess cleanup, socket close, etc.

**7. Cron platform routing becomes dynamic**

Today `cron_scheduler.py` hardcodes `_PREFERRED_ORDER = ("slack", "teams_cli", "web")`. Replace with:

- Platform preference order derived from `CHAT_APPS` ordering (first = highest priority), with `web` as fallback.
- Task `chat_app:` field matches by platform `name`, which is now arbitrary (not enum-constrained).

## Slack as a Plugin: Validation Walkthrough

To validate the architecture, here is how every current Slack feature would map if Slack were implemented as an external plugin. **We are not migrating Slack now**, but this proves the plugin contract is complete.

### Core message loop (covered by protocol)

| Slack feature | Plugin implementation |
|---------------|----------------------|
| Receive messages (Socket Mode) | `ChatAppReceiver.start()` — launches socket listener, dispatches `handle_incoming_message()` |
| Send replies | `MessagingPlatform.send()` — calls `chat_postMessage` + `files_upload_v2` |
| Processing indicator | `MessagingPlatform.set_processing()` — `reactions_add`/`reactions_remove` |
| Session resume | `get_session_id()` / `set_session_id()` — reads/writes `SessionStore` |
| Cron thread creation | `MessagingPlatform.start_thread()` — posts to cron channel |

### Inbound attachments

| Step | Implementation |
|------|---------------|
| Slack sends `files[]` in event | Receiver extracts file URLs |
| Download files | `download_slack_files()` — HTTP GET with Bearer token to `UPLOADS_DIR` |
| Pass to orchestrator | `IncomingMessage(attachments=[Attachment(local_path=...)])` |

For an MCP plugin, the MCP server would handle the download and return local paths in `poll_messages` or `new_message`.

### Outbound attachments

| Step | Implementation |
|------|---------------|
| Claude produces files | `conversation.py` extracts `<attachment>` tags, resolves from `ATTACHMENTS_DIR` |
| Pass to platform | `OutgoingMessage(attachments=[Attachment(local_path=...)])` |
| Upload to Slack | `platform.send()` calls `files_upload_v2(file=local_path)` per attachment |

For an MCP plugin, `send_message` tool call includes the local paths; the MCP server reads and uploads them.

### Slash commands

| Command | Plugin approach |
|---------|----------------|
| `/yuki-model` | Plugin registers with Slack Bolt internally; reads/writes `ModelStore` (passed via factory) |
| `/yuki-usage` | Plugin queries `SessionStore.stats()` (passed via factory) |
| `/yuki-title` | Plugin parses `!title` in receiver before dispatching |
| `/yuki-*` (skill routing) | Plugin handles internally; calls `run_claude` with skill flag |

These are inherently platform-specific (Slack slash commands, Teams bot commands, etc.). The plugin owns the registration and dispatch. yuki-conductor provides `SessionStore` and `ModelStore` for data access.

### Other Slack-specific features

| Feature | Plugin approach |
|---------|----------------|
| Restart notification | `on_startup_complete()` lifecycle hook |
| Connection watchdog | Internal to plugin's `start()` thread |
| Message filtering (subtype) | Internal to receiver's event handler |
| Cross-platform session guard | Receiver checks `store.get_session_type()` before dispatching |
| `markdown_to_mrkdwn` | Applied inside `platform.send()` — plugin's responsibility |
| Per-channel model | Plugin writes `ModelStore`; passes `model` in `IncomingMessage` |

### Web frontend attachment components

The web frontend's attachment UI is relevant because any new platform's web-viewable sessions need compatible attachment handling:

**Upload component** (`web/src/chat/Chat.tsx`):
- File picker → `POST /api/uploads` → multipart upload → returns `{id, filename, url, mime_type}`
- Pending attachments shown as pills in composer before send
- This is web-platform-only; chat platform plugins don't use the web upload endpoint

**Display component** (`web/src/chat/Chat.tsx`):
- Messages carry `attachments: [{id, filename, url, mime_type}]`
- Images (`image/*` or common extensions) render as inline `<img>` with click-to-open
- Other files render as `📎 filename` download links
- URLs resolve to `/api/files/{file_id}` → `FileResponse` from `WEB_UPLOADS_DIR`

**Impact on plugin architecture:**
- When chat-platform messages are viewed in the web UI (e.g., browsing Slack session history), attachments must be accessible via `/api/files/` URLs.
- Today this only applies to `WebPlatform` sessions. If we add a "view any session in web" feature later, we'd need the platform plugin to either (a) make files available at a web-accessible path, or (b) copy/link files into `WEB_UPLOADS_DIR` with the `{file_id}/{filename}` structure.
- For now, this is out of scope — each platform handles its own attachment display. But the `StoredAttachment` schema (`{id, filename, url, mime_type}`) should be considered the canonical attachment reference format for any future cross-platform session viewer.

## Implementation Plan

### Phase 1: Entry point plugin system

1. **Remove `ChatApp` enum** — replace with open string set in `config.py`.
2. **Update `runtime.py`** — add entry point discovery in `_build_receiver()`, pass `SessionStore` + `ModelStore` to factories.
3. **Delete `teams_cli_platform.py`** — it's a placeholder; remove along with its tests.
4. **Add lifecycle hooks** — `on_startup_complete()` and `stop()` to `ChatAppReceiver` protocol (optional, with default no-ops).
5. **Make cron routing dynamic** — remove hardcoded `_PREFERRED_ORDER`, derive from `CHAT_APPS` order.
6. **Export stable API** — ensure these are importable for external plugins:
   - `yuki_conductor.messaging.platform`: `MessagingPlatform`, `ChatAppReceiver`, `IncomingMessage`, `OutgoingMessage`, `Attachment`
   - `yuki_conductor.store`: `SessionStore`, `ModelStore`
   - `yuki_conductor.config`: `UPLOADS_DIR`, `ATTACHMENTS_DIR`, `WORKSPACE_DIR`

### Phase 2: MCP Bridge

7. **Add `mcp_bridge.py`** — implements `McpBridgePlatform` and `McpBridgeReceiver` using an MCP client over stdio.
8. **Define MCP tool schema** — JSON schema for the required tools + notification, published as `docs/mcp-chat-plugin-spec.json`.
9. **Add integration test** — mock MCP server that exercises the bridge end-to-end (send, receive, attachments, processing indicator).

### Phase 3: External plugin template

10. **Create template repo** — `yuki-conductor-chat-plugin-template` with:
    - `pyproject.toml` with entry point registration
    - Skeleton MCP server implementing the tool contract
    - Example attachment download/upload handling
    - README with setup instructions

## File Changes Summary

| File | Change |
|------|--------|
| `config.py` | Remove `ChatApp` enum, `chat_apps()` returns `set[str]` |
| `runtime.py` | Entry point discovery in `_build_receiver()`, pass stores to factories |
| `messaging/platform.py` | Add `on_startup_complete()`, `stop()` to `ChatAppReceiver` |
| `teams_cli_platform.py` | Delete |
| `test_teams_cli_platform.py` | Delete |
| `cron_scheduler.py` | Dynamic platform routing (remove hardcoded preference order) |
| `messaging/__init__.py` | Export public API types |
| `messaging/mcp_bridge.py` | New: MCP bridge platform + receiver |
| `CLAUDE.md` | Update architecture docs |

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| MCP stdio transport reliability | Health check pings; auto-restart crashed servers with backoff |
| Plugin API changes break external repos | Version the entry point group (`yuki_conductor.chat_plugins.v1`) from the start |
| MCP server startup latency | Start MCP servers eagerly at daemon boot, not on first message |
| Attachment path assumptions across OS | Pass `UPLOADS_DIR`/`ATTACHMENTS_DIR` as absolute paths to MCP server; document contract |
| Plugin fails to guard cross-platform sessions | Document requirement; `McpBridgeReceiver` handles this automatically for MCP-based plugins |
| Large file transfers over MCP | Files are never transferred over MCP — only local paths are exchanged (shared filesystem) |
