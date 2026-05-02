# Web Agent Conductor — Phase 1

Add an HTTP server with a React frontend to the yuki-conductor daemon, serving as the foundation for a full agent conductor UI.

## Context

- The daemon currently runs only a Slack Socket Mode listener
- Session tracking maps `thread_ts → session_id` (no `channel_id` stored)
- To construct Slack thread URLs (`https://wandermyz.slack.com/archives/{channel}/p{ts}`), we need channel_id
- Use pnpm (not npm) for the frontend
- HTTP server on port **2333**

## Architecture

```
yuki-conductor/
  src/yuki_conductor/
    web_server.py        — FastAPI HTTP server (API + static file serving)
    store.py             — Extended: sessions table gets channel_id column
    slack_app.py         — Updated: pass channel_id when storing sessions
  web/                   — React + Vite frontend (pnpm)
    src/
      App.tsx
      components/
        Sidebar.tsx      — Session list
        SessionView.tsx  — Main content (Slack thread link for now)
    ...
```

### Why FastAPI?

- Already Python-based project (stays in one process)
- Async-friendly, can run in a background thread alongside slack-bolt
- Auto-generates OpenAPI docs, useful for future extension
- Lightweight, minimal dependencies

### Why React + Vite?

- User wants a "proper frontend framework" for future extension
- Vite is fast, modern, and works well with pnpm
- React is well-suited for the interactive conductor UI planned later

## Implementation Steps

### 1. Extend the sessions store schema

Add `channel_id` column to the sessions table so we can construct Slack URLs.

**Changes:**
- `store.py`: Migrate `sessions` table from KV to `(thread_ts, channel_id, session_id)`. Add `list_sessions()` method returning all sessions with their channel_id.
- Keep `_SqliteKVStore` for the models table (unchanged).
- Auto-migrate: on init, if `channel_id` column doesn't exist, `ALTER TABLE` to add it (nullable for old rows).

### 2. Update slack_app.py to store channel_id

Pass `channel_id` when calling `store.set()` in the message handler, both for new messages and thread replies.

### 3. Add FastAPI web server

**New file: `web_server.py`**
- `GET /api/sessions` — returns list of sessions with `thread_ts`, `channel_id`, `session_id`, and computed `slack_url`
- Static file serving: serve the built React app from `web/dist/`
- Slack workspace domain: use env var `SLACK_WORKSPACE` (default: `wandermyz`) to construct URLs

**Dependency:** Add `fastapi` and `uvicorn` to `pyproject.toml`.

### 4. Integrate web server into daemon startup

In `slack_app.py`'s `start()`:
- Start the FastAPI/uvicorn server in a background thread on port 2333 before starting the Socket Mode handler
- The Socket Mode handler remains the blocking call

### 5. Scaffold React frontend

```
cd yuki-conductor && pnpm create vite web --template react-ts
```

**`web/src/App.tsx`**: Two-panel layout — sidebar + main content
**`web/src/components/Sidebar.tsx`**: Fetches `GET /api/sessions`, displays session list
**`web/src/components/SessionView.tsx`**: Shows the Slack thread URL as a clickable link

Minimal styling (CSS modules or plain CSS). Keep it clean and extensible.

### 6. Build integration

- `web/vite.config.ts`: configure `base: '/'` and proxy `/api` to port 2333 in dev mode
- For production: `pnpm build` outputs to `web/dist/`, FastAPI serves it as static files
- Add `web/dist/` to `.gitignore`

### 7. Update CLAUDE.md

Document the web server, port, and new commands (`pnpm dev`, `pnpm build` in `web/`).

## Data Flow

```
Browser → GET /api/sessions → FastAPI → SQLite (sessions table) → JSON response
Browser → GET /*             → FastAPI → web/dist/ (static React build)
```

## Slack URL Format

```
https://{SLACK_WORKSPACE}.slack.com/archives/{channel_id}/p{thread_ts_without_dot}
```

Example: thread_ts `1773529834.114199` in channel `D0AEWQ3124B` →
`https://wandermyz.slack.com/archives/D0AEWQ3124B/p1773529834114199`

## Open Questions

- Should old sessions (without channel_id) be shown in the UI? → Yes, but without a Slack link.
- Authentication for the web UI? → Not for now (localhost only).
