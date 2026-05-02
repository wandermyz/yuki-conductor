# Zellij Session Management — Implementation Documentation

## Overview

The Zellij session management feature extends yuki-conductor's web UI to create, attach to, and manage Claude Code sessions running inside Zellij terminal multiplexer sessions. Users can interact with full Claude Code terminal sessions directly from the browser, alongside the existing Slack-based sessions.

## Architecture

### Data Flow

```
Browser (xterm.js)
    ↕ WebSocket (binary frames + JSON resize messages)
FastAPI WebSocket endpoint
    ↕ PTY (master/slave fd pair)
zellij attach --create <session>
    ↕ Zellij internal multiplexing
claude --worktree <name>
```

### Components

**Backend:**
- `zellij_manager.py` — Wraps the `zellij` CLI for session lifecycle (create, list, kill)
- `web_server.py` — REST endpoints for CRUD + WebSocket endpoint for terminal bridging
- `store.py` — Extended with `session_type` and `project` columns

**Frontend:**
- `Terminal.tsx` — xterm.js component with WebSocket connection and resize handling
- `App.tsx` — Sidebar with project grouping, session type badges, alive indicators, and a detail view with Attach/Resume/Delete actions

### Session Lifecycle

```
[Create] → POST /api/sessions/zellij
               → zellij attach --create-background <name>
               → zellij action write-chars "claude --worktree <name>\n"
               → Store in SQLite with session_type="zellij"

[Attach] → WebSocket /ws/terminal/zellij:<name>
               → Wait for initial resize message from xterm.js
               → Set PTY size via TIOCSWINSZ on slave fd
               → Spawn "zellij attach <name> --create" in PTY
               → Bridge: PTY stdout → WebSocket, WebSocket → PTY stdin
               → On disconnect: close PTY fd, terminate process (Zellij session persists)

[Close]  → DELETE /api/sessions/zellij/zellij:<name>
               → zellij kill-session <name>
               → zellij delete-session <name>
               → Session record stays in DB (alive=false)

[Resume] → POST /api/sessions/zellij/zellij:<name>/reopen
               → zellij attach --create-background <name>
               → zellij action write-chars "claude --continue --worktree <name>\n"

[Delete] → DELETE /api/sessions/zellij/<key> (kill Zellij)
         → DELETE /api/sessions/<key> (remove DB record)
```

### Database Schema

The sessions table was extended with auto-migration (backward compatible):

```sql
ALTER TABLE sessions ADD COLUMN session_type TEXT DEFAULT 'slack';
ALTER TABLE sessions ADD COLUMN project TEXT;
```

Zellij sessions use `zellij:<session_name>` as the primary key (the `key` column), keeping the single-table design. Slack sessions continue using `thread_ts` as the key.

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/sessions` | List all sessions (includes `session_type`, `project`, `alive` for Zellij) |
| POST | `/api/sessions/zellij` | Create new Zellij session |
| DELETE | `/api/sessions/zellij/{key}` | Kill Zellij process (keep DB record) |
| POST | `/api/sessions/zellij/{key}/reopen` | Resume a stopped session with `--continue` |
| DELETE | `/api/sessions/{key}` | Delete session record from DB |
| PATCH | `/api/sessions/{key}` | Update session title |
| WebSocket | `/ws/terminal/{key}` | Interactive terminal bridge |

**Route ordering matters:** The specific `/api/sessions/zellij/...` routes must be registered before the generic `/api/sessions/{path}` catch-all, or FastAPI will match the wrong handler.

## Lessons Learned

### 1. Zellij Session Creation Requires Special Handling

**Problem:** Initially tried `zellij --session <name> action new-pane -- claude ...` to create sessions. This only works from inside an existing Zellij session.

**Solution:** Use `zellij attach --create-background` to create a detached session, then `zellij --session <name> action write-chars` to type the claude command into the session's terminal. A 0.5s sleep is needed between creation and write-chars to let the session initialize.

### 2. Blocking PTY Reads Starve the Async Event Loop

**Problem:** Using `os.read(master_fd, 4096)` in the default thread pool executor (`run_in_executor(None, ...)`) blocked all other async operations. The `/api/sessions` endpoint took 43 seconds to respond while a terminal WebSocket was active.

**Solution:** Two changes:
1. Use a **dedicated `ThreadPoolExecutor`** (10 threads, named "pty") instead of the default executor
2. Use **`select.select()` with a 0.5s timeout** instead of blocking `os.read`, combined with a `threading.Event` stop signal so reader threads exit promptly on WebSocket disconnect

```python
def _blocking_read():
    while not stop_event.is_set():
        ready, _, _ = select.select([master_fd], [], [], 0.5)
        if ready:
            data = os.read(master_fd, 4096)
            if not data:
                return None
            return data
    return None
```

Without the `select()` approach, `asyncio.Task.cancel()` cannot interrupt a thread blocked in `os.read`, causing thread leaks on page refresh.

### 3. Zellij "Exited" Sessions Linger After Kill

**Problem:** `zellij kill-session <name>` exits the session but doesn't remove it. `zellij list-sessions --short` still lists it, making alive detection incorrect (sessions appeared alive after being killed).

**Solution:**
- After `kill-session`, also run `zellij delete-session` to clean up
- Parse the full `zellij list-sessions` output (not `--short`) and filter out lines containing "EXITED"
- Strip ANSI escape codes from the output to extract session names:
  ```python
  clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
  ```

### 4. Terminal Resize Requires Explicit SIGWINCH

**Problem:** Setting `TIOCSWINSZ` on the PTY master fd should automatically send `SIGWINCH` to the child process, but Zellij (spawned with `start_new_session=True`) didn't pick up size changes.

**Solution:** After `ioctl(TIOCSWINSZ)`, explicitly send `SIGWINCH` to the process group:
```python
os.killpg(os.getpgid(proc.pid), signal.SIGWINCH)
```

Also, wait for the initial resize message from xterm.js before spawning Zellij, so the PTY starts at the correct dimensions rather than defaulting to 80x24.

### 5. Terminal Environment Variables Must Be Set Explicitly

**Problem:** The PTY subprocess inherits the daemon's environment, which lacks `TERM`, `SHELL`, and locale settings. This caused backspace to produce spaces, `TERM not set` errors in commands like `git branch`, and broken key bindings in zsh.

**Solution:** Explicitly set environment variables on both the PTY attach and background session creation:
```python
env.update({
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
    "SHELL": "/bin/zsh",
    "LC_ALL": "en_US.UTF-8",
    "LANG": "en_US.UTF-8",
})
```

### 6. iOS Safari Scroll Prevention Is Layered

**Problem:** On iPad/iOS Safari, the page was scrollable when viewing the terminal, despite `overflow: hidden` on the app container. Safari has elastic bounce scrolling that ignores overflow on child elements.

**Solution:** Multiple CSS layers needed:
- `100dvh` (dynamic viewport height) instead of `100vh` to account for Safari's collapsing URL bar
- `overflow: hidden` and `overscroll-behavior: none` on `html, body`
- `overscroll-behavior: contain` on the xterm viewport to trap scroll within the terminal
- Avoid `touch-action: none` or JS `preventDefault()` on touchmove, as these block mouse/touch signals from reaching Zellij

### 7. FastAPI Route Order Matters With Path Parameters

**Problem:** `DELETE /api/sessions/zellij/zellij:Z6` was matched by the generic `DELETE /api/sessions/{thread_ts:path}` (with `thread_ts = "zellij/zellij:Z6"`) instead of the specific `DELETE /api/sessions/zellij/{session_key:path}`.

**Solution:** Register specific routes before generic catch-all routes. FastAPI matches routes in registration order when both patterns could match.

### 8. xterm.js Resize Observer Needs Debouncing

**Problem:** `ResizeObserver` fires rapidly during CSS transitions (sidebar collapse/expand), causing excessive resize messages and visual jitter.

**Solution:** Debounce the fit + resize with a 100ms `setTimeout`:
```typescript
const resizeObserver = new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(doResize, 100);
});
```

## Frontend UX Design

### Session Detail Flow (Zellij)

Clicking a Zellij session in the sidebar shows a **detail view** first (not the terminal):
- Status indicator (green dot = running, gray = stopped)
- **Attach** button (green, if session is alive)
- **Resume** button (purple, if session is stopped — creates new Zellij session with `claude --continue`)
- **Delete** button (red outline — kills Zellij if alive, removes from DB)

Only after clicking Attach/Resume does the terminal appear. The terminal view has a **Close** button that kills the Zellij process and returns to the detail view.

### Sidebar Features

- **Project grouping** with collapsible headers (click arrow/name to fold)
- **Collapsible sidebar** (click `<<`/`>>` to shrink to 40px)
- **Type badges**: "S" (purple) for Slack, "Z" (green) for Zellij
- **Alive dots**: Green with glow for running, gray for stopped
- **New session button** (`+`) per project group with inline worktree name form

## Dependencies

**Python (all stdlib, no new packages):**
- `pty`, `fcntl`, `termios`, `select`, `signal`, `struct` — PTY management
- `concurrent.futures.ThreadPoolExecutor` — dedicated thread pool for PTY reads
- `asyncio` — WebSocket async handling (already used by FastAPI)

**Frontend:**
- `@xterm/xterm` — terminal emulator
- `@xterm/addon-fit` — auto-resize terminal to container
