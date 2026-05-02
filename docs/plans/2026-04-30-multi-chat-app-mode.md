# Multi Chat App Mode — Plan

Generalize yuki-conductor's chat surface from "Slack-mode" (single, exclusive) to a set of chat apps that can run side-by-side. Drop the unused Slack TOKEN mode. Add Teams CLI as a third platform — a placeholder receiver/sender for now, with the actual CLI tool delivered as a separate project later.

## Background

Two prior refactors set the stage:

- **Slack mode dispatcher** (`slack_app.start()`): introduced `SLACK_MODE` env (`NONE` | `SOCKET` | `TOKEN`). TOKEN was never implemented — `_start_token_mode` raises `NotImplementedError`.
- **Web messaging platform** (`docs/plans/2026-04-29-web-messaging-platform.md`, merged): split orchestration into a platform-agnostic core. We already have:
  - `messaging/platform.py` — `MessagingPlatform` Protocol with `send`, `set_processing`, `get_session_id`, `set_session_id`.
  - `messaging/conversation.py` — `handle_incoming_message(platform, msg)` is the only code that talks to `run_claude` and parses `<attachment>` tags.
  - `messaging/slack_platform.py`, `messaging/web_platform.py` — two concrete adapters.
  - `slack_app.py` — Bolt event handlers that build `IncomingMessage` and dispatch into the core.

The abstraction is in place; what's missing is **multiple platforms running simultaneously**, **per-session platform stickiness**, and **a third platform (Teams CLI)** wired into the same machinery.

## Goals

1. Replace single-valued `SLACK_MODE` with multi-valued `CHAT_APPS` (set of enabled platforms).
2. Drop Slack TOKEN mode.
3. Introduce Teams CLI as a chat app — fully integrated with the messaging core, stub receiver/sender for now (real CLI delivered separately).
4. Each session is platform-tagged so replies always route back to the originating chat app.
5. Cron + web continue to work; cron can pick which chat app it notifies.
6. **Out of scope**: building the actual Teams CLI binary, multi-tenant routing, message bridging across apps.

## Target shape

```
CHAT_APPS = "slack_socket,teams_cli"         ← env, comma-separated set; "" or "none" = none

runtime.start()                              ← new orchestrator (extracted from slack_app)
  for app in chat_apps():
      app.start_receiver()                   ← non-blocking; spawns its own threads
  start_web_server()
  start_cron_scheduler(platforms=...)
  block forever (or until any receiver exits)

messaging/
  platform.py                                ← unchanged Protocol + new ChatAppReceiver
  conversation.py                            ← unchanged core
  slack_platform.py                          ← existing; gains start_thread()
  teams_cli_platform.py                      ← new
  web_platform.py                            ← unchanged + start_thread()

SessionStore.session_type ∈ {"slack", "teams_cli", "zellij", "web"}
```

Key invariant: **a session_id is owned by exactly one platform**. The platform name is recorded on first message and never changes. Reply routing reads `session_type` to pick the receiver.

## Detailed changes

### 1. Config: `SLACK_MODE` → `CHAT_APPS`

`config.py`:

```python
class ChatApp(StrEnum):
    SLACK_SOCKET = "slack_socket"
    TEAMS_CLI    = "teams_cli"

def chat_apps() -> set[ChatApp]:
    # Reads CHAT_APPS env (comma-separated). "" / "none" → empty set.
    # Defaults to {SLACK_SOCKET} for back-compat.
    # Honors legacy SLACK_MODE=SOCKET/NONE with a deprecation warning.
    # SLACK_MODE=TOKEN is rejected (mode is removed).
```

### 2. Extract orchestration: new `runtime.py`

Move `start()`, the watchdog, the restart-notification block, and the per-mode startup helpers out of `slack_app.py` into a new `runtime.py`. `slack_app.py` becomes purely the Bolt event-handler module + `create_app()` + `SlackSocketReceiver`.

`runtime.start()` builds the receiver list, registers each receiver's platform under its `name` in a `platforms_by_name` dict, starts `web_server` and `cron_scheduler`, calls `receiver.start()` on each, then blocks on `threading.Event().wait()`.

`ChatAppReceiver` Protocol (in `messaging/platform.py`):

```python
class ChatAppReceiver(Protocol):
    name: str
    platform: MessagingPlatform
    def start(self) -> None: ...               # non-blocking
```

### 3. Slack: `SlackSocketReceiver`

Pull the Bolt + SocketModeHandler setup into a class so it has the same shape as the future Teams CLI receiver. `create_app()` stays in `slack_app.py`; `start()`, `_start_*` helpers move out.

### 4. New: `messaging/teams_cli_platform.py`

Placeholder. Implements `MessagingPlatform` and exposes `TeamsCliReceiver`. The actual CLI is a separate project — for now, the receiver is a no-op idle loop and `send`/`set_processing` log.

The future contract is documented inline so the eventual integrator has a fixed wire format:
- spawn `teams-cli daemon --stdio`
- read NDJSON inbound events: `{conv_id, msg_id, text, attachments, is_thread_start}`
- for each, build an `IncomingMessage` and call `handle_incoming_message`
- write outbound NDJSON for `send` / `set_processing` / `start_thread`

### 5. Session stickiness

`SessionStore` already carries `session_type`. Two small reinforcements:

- **On lookup**: when a platform receives an inbound message, it should not act on a session whose `session_type` belongs to another platform. Add `SessionStore.get_session_type(key)` and have each receiver bail out (log + ignore) on mismatch.
- **On write**: each platform's `set_session_id` already passes its own `session_type` (Slack does today; Teams CLI will).

No schema change needed; `session_type` already exists.

### 6. Cron scheduler: route by platform

After refactor:

- Signature: `start_cron_scheduler(platforms_by_name: dict[str, MessagingPlatform])`.
- Each `cron.yaml` task gains an optional `chat_app:` field (`slack_socket | teams_cli`).
- **Routing rule** when picking a platform for a task:
  1. If `chat_app:` is specified and that app is in `CHAT_APPS` → use it.
  2. If `chat_app:` is specified but the app is not enabled → log a warning and skip the task (do not silently misroute to another platform).
  3. If `chat_app:` is omitted → fall back to the first enabled platform, with `slack_socket` preferred when multiple are enabled. This preserves today's behavior for existing cron files.
  4. If no platforms are enabled at all → log the notification (current `slack_client=None` behavior).
- `_run_cron_task` builds an `OutgoingMessage` and calls `platform.send(conversation_key=...)`. The `conversation_key` for a fresh cron post is currently `thread_ts` returned by `chat_postMessage`; we keep that for Slack but use a UUID for Teams CLI (the receiver/CLI is responsible for mapping it back).

Cron-specific Slack quirks (creating the parent thread post and grabbing its `ts`) move into a new `MessagingPlatform.start_thread(text, title) -> conversation_key`. Each platform implements it: Slack posts to the cron channel and returns the `ts`; Teams CLI returns a fresh `teams:{uuid}`; Web creates a new conversation row and returns its id.

### 7. Cleanup

- Remove `_start_token_mode`, `SlackMode.TOKEN`, and any docs referencing it.
- Update `yuki-conductor/CLAUDE.md` "Slack Mode" section → "Chat Apps", with the new env name and the `slack_socket | teams_cli` values.
- Update `cron.example.yaml` to show the optional `chat_app:` field.

## Refactor order (low-risk first)

1. Add `ChatApp` enum + `chat_apps()` alongside `SlackMode`/`slack_mode()`. No behavior change.
2. Extract `runtime.py` — `runtime.start()` reads `slack_mode()` for now, calls existing `_start_*` helpers. `cli.py` switches to `runtime.start()`.
3. Convert Slack startup into `SlackSocketReceiver` class. `runtime.start()` instantiates it.
4. Switch cron to `platforms_by_name` dict. Add `MessagingPlatform.start_thread`. Implement on `SlackPlatform`.
5. Add `TeamsCliPlatform` + `TeamsCliReceiver` placeholder.
6. Wire `CHAT_APPS` end-to-end. Replace `slack_mode()` callers with `chat_apps()`. Delete `SlackMode`, `slack_mode()`, `_start_token_mode`. Add deprecation translator for `SLACK_MODE`.
7. Tests: `test_chat_apps_config.py` (parsing + legacy translation), `test_runtime.py` (receiver wiring), `test_cron_routing.py` (per-task routing), `test_teams_cli_platform.py` (placeholder behavior). Existing tests must still pass.
8. Docs: update `yuki-conductor/CLAUDE.md` and `.env.template`.

## Open questions

- **Default `CHAT_APPS`.** Keep defaulting to `slack_socket` (back-compat), or default to empty (explicit opt-in)? Recommend `slack_socket` since the existing LaunchAgent doesn't set the env.
- **Teams CLI conversation key format.** Suggest `teams:{uuid}` — keeps it visually disambiguated from Slack `thread_ts` and matches the existing `zellij:{name}` convention.

## Risk / rollback

- **Step 2 (extract `runtime.py`)** is the only step that touches the live LaunchAgent entry point. Keep `slack_app.start()` as a one-line shim to `runtime.start()` until the next pass to make rollback a one-line revert.
- **Step 6 (delete `SlackMode`)** is a hard cutover. Land it only after steps 1–5 have been smoke-tested locally with `CHAT_APPS=slack_socket`. The `SLACK_MODE` deprecation translator stays for one cycle so a stale `.env` doesn't crash on first start.
- Teams CLI placeholder is inert; enabling it without the binary is harmless (logs and idles).
