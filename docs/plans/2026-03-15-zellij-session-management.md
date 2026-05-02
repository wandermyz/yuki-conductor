# Zellij Session Management for yuki-conductor

## Context

Currently yuki-conductor only manages Slack-based Claude sessions. This plan adds the ability to create and interact with Claude Code sessions running inside Zellij terminal multiplexer sessions, viewable and controllable directly from the web UI. This enables local terminal-based Claude workflows alongside the existing Slack integration.

## Key Files to Modify/Create

| File | Action |
|------|--------|
| `src/yuki_conductor/store.py` | Modify — add `session_type`, `project` columns |
| `src/yuki_conductor/zellij_manager.py` | **Create** — Zellij CLI wrapper |
| `src/yuki_conductor/web_server.py` | Modify — add WebSocket + REST endpoints |
| `web/src/App.tsx` | Modify — grouped sidebar, type badges, conditional main view |
| `web/src/Terminal.tsx` | **Create** — xterm.js terminal component |
| `web/src/App.css` | Modify — new styles for groups, badges, terminal |
| `web/vite.config.ts` | Modify — add WebSocket proxy |
| `web/package.json` | Modify — add xterm.js deps |

## Implementation Steps

### Step 1: Schema Extension (`store.py`)

Add two columns via auto-migration:
- `session_type TEXT DEFAULT 'slack'` — values: `"slack"` or `"zellij"`
- `project TEXT` — e.g. `"wandering-vibe"`, nullable

Update `set()` to accept `session_type` and `project` params (default `"slack"`, `None`).

Update `list_all()` to return the new fields.

Add `create_zellij_session(session_id: str, title: str, project: str, zellij_session_name: str) -> str` — uses key format `zellij:<session_name>` to distinguish from Slack's `thread_ts` keys.

### Step 2: Zellij Manager (new: `zellij_manager.py`)

Wraps the `zellij` CLI:

- `create_session(name: str, worktree: str, working_dir: str) -> None` — spawns detached Zellij session running `claude --worktree <worktree>`. Uses `subprocess.Popen` with the Zellij `--session` flag.
- `list_sessions() -> list[str]` — parses `zellij list-sessions` output.
- `is_session_alive(name: str) -> bool` — checks if session exists.
- `kill_session(name: str) -> None` — runs `zellij kill-session <name>`.

### Step 3: Backend Endpoints (`web_server.py`)

**REST — `POST /api/sessions/zellij`**
- Body: `{ "name": str, "project": str, "worktree": str }`
- Creates Zellij session via `zellij_manager.create_session()`
- Stores in SessionStore with `session_type="zellij"`
- Returns the new session object

**REST — update `GET /api/sessions`**
- Include `session_type` and `project` in response

**WebSocket — `GET /ws/terminal/{session_key}`**
- Spawns `zellij attach <session_name>` in a PTY (`pty.openpty()` + `subprocess.Popen`)
- Bidirectional bridge: PTY stdout → WebSocket binary, WebSocket → PTY stdin
- Handle resize messages from frontend: `{"type":"resize","cols":N,"rows":N}` → `ioctl(TIOCSWINSZ)`
- Use `asyncio.get_event_loop().run_in_executor()` for blocking PTY reads
- On disconnect: close PTY (detaches from Zellij, session keeps running)

### Step 4: Frontend Dependencies

```bash
cd web && pnpm add @xterm/xterm @xterm/addon-fit
```

### Step 5: Vite WebSocket Proxy (`vite.config.ts`)

Add `/ws` proxy entry with `ws: true` targeting `ws://localhost:2333`.

### Step 6: Terminal Component (new: `web/src/Terminal.tsx`)

React component using xterm.js:
- On mount: create `Terminal`, attach `FitAddon`, open in container ref
- Connect WebSocket to `ws://${location.host}/ws/terminal/${sessionKey}`
- `terminal.onData` → WebSocket send (user input)
- WebSocket `onmessage` → `terminal.write()` (PTY output)
- FitAddon resize → send resize JSON to WebSocket
- On unmount: close WebSocket, dispose terminal
- Import `@xterm/xterm/css/xterm.css`

### Step 7: Frontend Sidebar Grouping & Type Indicators (`App.tsx`)

**Extend `Session` interface** with `session_type: "slack" | "zellij"` and `project: string | null`.

**Group sidebar by project:**
- Compute `Map<string, Session[]>` grouped by `project ?? "Other"`
- Render each group with a header showing project name
- Add a `+` button per project header to create new Zellij session
- Clicking `+` shows an inline form prompting for worktree name
- POST to `/api/sessions/zellij`, refresh list on success

**Session type badge:**
- Small pill/label next to each session title: "Slack" or "Zellij"

**Conditional main view:**
- `session_type === "slack"` → existing metadata detail view
- `session_type === "zellij"` → render `<Terminal sessionKey={...} />` component full-height

### Step 8: CSS Updates (`App.css`)

- `.project-group` / `.project-header` — group containers with project name
- `.session-type-badge` — small colored pill (blue for Slack, green for Zellij)
- `.new-session-btn` — `+` button styling
- `.new-session-form` — inline worktree name input
- `.terminal-container` — full-height container, no padding (xterm needs full area)

## Implementation Order

1. Step 1 (store.py) — foundation, no deps
2. Step 2 (zellij_manager.py) — standalone
3. Step 3 (web_server.py) — depends on 1 & 2
4. Steps 4-5 (frontend deps + vite proxy) — parallel with backend
5. Step 6 (Terminal.tsx) — depends on 4
6. Steps 7-8 (App.tsx + CSS) — depends on 6

## Key Design Decisions

- **Session key format**: Zellij sessions use `zellij:<name>` as the primary key, keeping the single-table design
- **No new Python deps**: `pty`, `fcntl`, `termios` are stdlib; `uvicorn[standard]` already includes WebSocket support
- **PTY bridge**: `zellij attach` runs in a real PTY, enabling full terminal interactivity including colors, cursor movement, etc.
- **Multiple attachments**: Zellij natively supports multiple clients on one session, so opening the same session in two tabs works

## Verification

1. Start backend: `uv run yuki-conductor run`
2. Start frontend dev: `cd web && pnpm dev`
3. Open web UI — sidebar should show existing Slack sessions grouped under "wandering-vibe"
4. Click `+` on project group → enter worktree name → verify Zellij session starts (`zellij list-sessions`)
5. Click the new Zellij session in sidebar → verify interactive terminal appears
6. Type in terminal → verify input reaches Claude in Zellij
7. Open second browser tab with same session → verify both work
8. Close tab → verify Zellij session still alive (`zellij list-sessions`)
9. Run `uv run pytest` — verify existing tests still pass + new store tests pass
