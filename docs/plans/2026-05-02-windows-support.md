# Windows Support — Plan

Add support for running yuki-conductor on Windows (PowerShell 7+). Today the
project is macOS-only: the daemon manages a `launchctl` LaunchAgent, the web
server imports POSIX-only modules at module scope (`pty`, `fcntl`, `termios`),
the launcher is a bash script, and several environment assumptions
(`/bin/zsh`, `/opt/homebrew/bin`, `:` PATH separator) are baked in. The goal of
this plan is twofold: (1) catalog every Windows blocker we currently have and
how to fix it, and (2) design a Windows-native daemon that is the equivalent of
the existing macOS LaunchAgent.

## Background

The existing daemon (`src/yuki_conductor/daemon.py`) wraps `launchctl
{load,unload,list}` and writes a plist into
`~/Library/LaunchAgents/com.user.yuki-conductor.plist`. It runs `uv run
--project <repo> yuki-conductor run` with `KeepAlive` and `RunAtLoad`, captures
stdout/stderr to `~/.yuki-conductor/daemon{,.err}.log`, and rebuilds the web
frontend on `daemon restart`.

The runtime that the LaunchAgent supervises (`runtime.start()`) is mostly
platform-agnostic Python — Slack Bolt Socket Mode, FastAPI/uvicorn, croniter,
SQLite, the messaging core. The Windows-incompatible code is concentrated in:

- `daemon.py` — entirely macOS-specific (plist + launchctl).
- `web_server.py` — top-level `import pty/fcntl/termios` and a Zellij PTY
  bridge that uses `pty.openpty`, `fcntl.ioctl(TIOCSWINSZ)`,
  `os.killpg(SIGWINCH)`, `select.select` on PTY fds, and hard-codes
  `SHELL=/bin/zsh`.
- `zellij_manager.py` — assumes a `zellij` binary on PATH and a POSIX shell.
- `bin/yuki-conductor` — bash wrapper.
- `config.py` — `PLIST_PATH` is defined unconditionally and points at
  `~/Library/LaunchAgents`.

Personal context: the user wants to run the daemon on a Windows host with
PowerShell 7. Slack Socket Mode + the FastAPI web UI + cron is the must-have;
Zellij terminal sessions are a nice-to-have but not a Windows blocker — the
Zellij feature can be disabled on Windows in the first cut.

## Goals

1. The CLI entry point (`uv run yuki-conductor ...`) imports cleanly on
   Windows — no top-level POSIX-only imports.
2. `yuki-conductor run` works on Windows for: Slack Socket Mode, FastAPI web
   UI (chat + sessions list), cron scheduler, Claude CLI subprocess.
3. `yuki-conductor daemon {install,uninstall,restart,status,log}` works on
   Windows, equivalent to the macOS LaunchAgent: auto-starts on user login,
   restarts on crash, captures stdout/stderr to log files, and rebuilds the
   web frontend on `restart`.
4. Out of scope (Windows): Zellij terminal WebSocket bridge. The terminal
   endpoints return a clear "not supported on Windows" error; the rest of the
   web UI keeps working.

## Caveats / fix list

Each item below is a Windows blocker in the current code, with the proposed
fix. Severity = `blocker` if `yuki-conductor run` won't even start, `feature`
if a specific feature breaks but the daemon survives.

### A. Hard import-time failures (blocker)

`src/yuki_conductor/web_server.py` imports `pty`, `fcntl`, `termios` and
`signal` at module top. The first three modules don't exist in Windows
Python. Because `web_server` is imported by `runtime.start()`, the daemon
won't even reach `slack_app`.

**Fix:** Move these imports — and the `_blocking_read` / PTY bridge logic —
out of `web_server.py` into a new `pty_bridge.py` module that is only
imported on POSIX. `web_server.py` registers the `/ws/terminal/...` route
conditionally:

```python
if sys.platform != "win32":
    from yuki_conductor.pty_bridge import register_terminal_ws
    register_terminal_ws(api)
else:
    @api.websocket("/ws/terminal/{session_key:path}")
    async def _no_terminal(ws: WebSocket, session_key: str):
        await ws.close(code=1011, reason="Terminal not supported on Windows")
```

`zellij_manager` is also imported unconditionally by `web_server` (used in
`/api/sessions` to compute `alive_sessions`). It currently shells out to
`zellij list-sessions`. On Windows we either (a) make `zellij_manager` a thin
no-op stub on Windows that returns `[]` from `list_sessions()` and raises
`NotImplementedError` from `create_session`/`kill_session`, or (b) gate the
Zellij API routes behind the same platform check. Prefer (a) — fewer call
sites change.

### B. Daemon module is macOS-only (blocker for `daemon` subcommand)

`daemon.py` is plist + launchctl end-to-end, and `config.py` defines
`PLIST_PATH = ~/Library/LaunchAgents/...` unconditionally. On Windows that
path is meaningless.

**Fix:** Split into platform adapters (see "Windows daemon design" below).
`config.py` should only export the path constants used by the runtime
itself (data dir, db, logs); the LaunchAgent path moves into the macOS
adapter. The `daemon` CLI subcommand dispatches by `sys.platform`.

### C. POSIX-only paths/separators in plist generation (blocker on macOS only — but worth flagging)

`daemon.py:62` does `":".join(extra_paths)`. This only runs on macOS today,
so it's not a Windows bug, but the equivalent code in the Windows adapter
must use `os.pathsep` (`;`) and a Windows-appropriate PATH. List candidates:
`%LOCALAPPDATA%\Programs\Python\Python3xx`, the user's `~\.local\bin` (uv's
default), and any explicit `CLAUDE_BIN` directory.

### D. bash wrapper (blocker for `bin/yuki-conductor`)

`bin/yuki-conductor` is `#!/usr/bin/env bash` + `exec uv run --project ...`.

**Fix:** The pip console-script entry point (`yuki-conductor =
yuki_conductor.cli:main` in `pyproject.toml`) already produces a
`yuki-conductor.exe` shim on Windows when the project is installed.
Document `uv run yuki-conductor ...` as the canonical invocation on Windows
(matches what the README already shows for macOS) and add a thin
`bin/yuki-conductor.cmd` for parity:

```cmd
@echo off
uv run --project "%~dp0.." yuki-conductor %*
```

The bash version stays for macOS users.

### E. Hard-coded `/bin/zsh` (feature: terminal)

`web_server.py:183` and `zellij_manager.py:25` set `SHELL=/bin/zsh`. Both
are inside the Zellij/PTY paths, which we're skipping on Windows (item A
covers it). No additional fix needed — but note for the future Windows PTY
support: ConPTY via `pywinpty` is the equivalent.

### F. `subprocess.run(text=True)` and locale encoding (feature: robustness)

`claude_runner.py:55`, `slack_app.py` (`claude --version` probe),
`zellij_manager.py` all use `text=True` without an explicit encoding. On
Windows, the default text encoding follows the active code page (often
`cp1252`), which can corrupt UTF-8 JSON returned by `claude -p
--output-format json`.

**Fix:** Add `encoding="utf-8", errors="replace"` to every subprocess that
reads `claude` output. Lower priority but worth doing in the same PR as the
daemon work.

### G. Tool discovery via `shutil.which` (feature)

`shutil.which("uv")` and `shutil.which("pnpm")` work on Windows because
`PATHEXT` covers `.exe`/`.cmd`, but only if the user's PATH includes the
relevant install dirs. Document this in the Windows install notes; no code
change required.

### H. Default `CLAUDE_WORKING_DIR` (feature)

`config.py:26` defaults to `~/Projects/wandering-vibe`. `os.path.expanduser`
handles `~` on Windows (`C:\Users\<user>\Projects\wandering-vibe`), so this
just works as long as the user sets `CLAUDE_WORKING_DIR` in `.env`. Confirm
in docs.

### I. SQLite, FastAPI/uvicorn, slack-bolt, croniter

All cross-platform. No fix needed. WAL journal mode and concurrent access
behave the same on Windows NTFS as on macOS APFS for our usage pattern
(single writer, occasional reader from web).

### J. Web build pipeline

`pnpm install --frozen-lockfile && pnpm build` works on Windows once `pnpm`
is on PATH. No change. The build step is invoked by `daemon restart` —
which means the Windows daemon adapter must invoke it the same way (call
`_build_web_frontend()` from shared code, not from the macOS adapter).

### K. Log file paths

`LOG_FILE = DATA_DIR / "daemon.log"` in `config.py` — already cross-platform
(`Path.home()` resolves to `C:\Users\<user>` on Windows). The macOS
LaunchAgent writes to it via `StandardOutPath`; the Windows daemon adapter
must redirect stdout/stderr explicitly when launching the supervised
process.

## Windows daemon design

The Windows equivalent of a per-user LaunchAgent is a per-user **Scheduled
Task** triggered by `OnLogon` with `RestartOnFailure`. We use Task Scheduler
rather than a Windows Service because:

- A per-user task runs in the user's session, with the user's environment
  (uv, pnpm, claude on PATH, the user's `~/.yuki-conductor/.env`). A Windows
  Service runs as `LocalSystem` (or a service account) and would need
  explicit credentialing + `runas` to access the user's PATH.
- Task Scheduler doesn't require admin elevation for `OnLogon` triggers in
  the user's own task folder.
- The CLI surface (`schtasks.exe` or PowerShell `ScheduledTasks` module) is
  scriptable from Python.

### Adapter layout

```
src/yuki_conductor/
  daemon.py                 — dispatcher: routes by sys.platform
  daemon_macos.py           — current code (plist + launchctl), renamed
  daemon_windows.py         — new: Task Scheduler adapter
  daemon_common.py          — shared helpers: _project_dir, _build_web_frontend
```

`daemon.py` becomes a 20-line dispatcher:

```python
def handle_daemon(action: str) -> None:
    if sys.platform == "darwin":
        from yuki_conductor.daemon_macos import handle_daemon as impl
    elif sys.platform == "win32":
        from yuki_conductor.daemon_windows import handle_daemon as impl
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")
    impl(action)
```

`_build_web_frontend()` and `_project_dir()` move to `daemon_common.py` and
are called from both adapters.

### Windows adapter mechanics

**Task name:** `YukiConductor` (in the user's `\` task folder — i.e. no
custom folder, simplest invocation).

**Task definition (XML, written to a temp file then registered with
`schtasks /Create /XML`):**

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{DOMAIN\user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{DOMAIN\user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RestartOnFailure>
      <Interval>PT15S</Interval>
      <Count>9999</Count>
    </RestartOnFailure>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions>
    <Exec>
      <Command>{path-to-pwsh.exe}</Command>
      <Arguments>-NoProfile -WindowStyle Hidden -File "{repo}\bin\yuki-conductor-daemon.ps1"</Arguments>
      <WorkingDirectory>{repo}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
```

The launcher script `bin/yuki-conductor-daemon.ps1` is a thin PowerShell
wrapper that:

1. Redirects stdout to `$env:USERPROFILE\.yuki-conductor\daemon.log` and
   stderr to `daemon.err.log` (Task Scheduler doesn't capture stdio
   directly — must be done by the launched process).
2. Execs `uv run --project <repo> yuki-conductor run`.

```powershell
# bin/yuki-conductor-daemon.ps1
$ErrorActionPreference = "Continue"
$DataDir = Join-Path $env:USERPROFILE ".yuki-conductor"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$LogOut = Join-Path $DataDir "daemon.log"
$LogErr = Join-Path $DataDir "daemon.err.log"
$Repo   = Split-Path -Parent $PSScriptRoot
& uv run --project $Repo yuki-conductor run *> $LogOut 2> $LogErr
```

### CLI mapping

| Subcommand | Implementation                                                               |
| ---------- | ---------------------------------------------------------------------------- |
| `install`  | Generate XML; `schtasks /Create /TN YukiConductor /XML <tmp>.xml /F`; `schtasks /Run /TN YukiConductor` |
| `uninstall`| `schtasks /End /TN YukiConductor` (ignore failure); `schtasks /Delete /TN YukiConductor /F` |
| `restart`  | `_build_web_frontend()`; `schtasks /End /TN YukiConductor`; `schtasks /Run /TN YukiConductor` |
| `status`   | `schtasks /Query /TN YukiConductor /FO LIST` — parse `Status:` line; print `Running` / `Ready` / `Not installed` |
| `log`      | Print paths to `daemon.log` / `daemon.err.log`; tail last 20 lines (already cross-platform code in `daemon.py:120-127`) |

`schtasks` returns non-zero on missing tasks; treat that as "not installed".
We discover the user identity via `os.environ["USERDOMAIN"] + "\\" +
os.environ["USERNAME"]` (or `whoami` fallback) when generating the XML.

### Why not a Windows Service?

- Requires admin to install (`sc.exe create`) — friction for personal use.
- Service session is non-interactive: no user PATH, no claude auth tokens
  in `%USERPROFILE%`, no Slack browser-cached creds.
- We'd need `pywin32`'s `win32serviceutil` boilerplate, which is more code
  than the schtasks XML.

If we ever need machine-wide install, NSSM (`nssm.exe`) wraps an arbitrary
executable as a Service in ~1 command — we can revisit then. Not now.

### Known gaps to document

- The Windows daemon will only run while the user is logged in (Logon
  trigger). For headless servers the user must enable "auto-logon" or we
  switch to a Service later.
- `RestartOnFailure` in Task Scheduler counts process exits as failures
  only when the exit code is non-zero. `runtime.start()` blocks forever on
  `threading.Event().wait()` so a clean exit is unlikely; if it ever
  returns 0 the task won't restart. Acceptable for v1.

## Implementation phases

Each phase is independently reviewable and shippable.

### Phase 1 — Make `import` clean on Windows (blocker fix only)

Smallest change that lets `uv run yuki-conductor run` start on Windows. No
daemon, no Zellij — Slack/web/cron only.

1. Extract PTY bridge from `web_server.py` into `pty_bridge.py`; gate
   registration on `sys.platform != "win32"`.
2. Make `zellij_manager.list_sessions()` return `[]` on Windows; other
   methods raise `NotImplementedError` with a clear message.
3. Add `bin/yuki-conductor.cmd`.
4. Add `encoding="utf-8", errors="replace"` to every `subprocess.run` that
   reads stdout (claude_runner, slack_app version probe, zellij_manager).
5. README: add a "Windows" section pointing at `uv run yuki-conductor run`
   and noting Zellij is unavailable.

Acceptance: on a Windows box with uv + claude + Slack tokens configured,
`uv run yuki-conductor run` stays up, Slack DMs round-trip through Claude,
and the web UI loads at `http://localhost:2333`.

### Phase 2 — Windows daemon

1. Rename `daemon.py` → `daemon_macos.py`. Move shared helpers to
   `daemon_common.py`.
2. New `daemon.py` dispatcher.
3. New `daemon_windows.py` with `_install/_uninstall/_restart/_status/_log`
   wrapping `schtasks`.
4. New `bin/yuki-conductor-daemon.ps1` launcher.
5. Tests: unit-test the XML generator (snapshot test on the rendered XML),
   mock `subprocess.run` for `schtasks` calls.
6. README: add `daemon install` flow for Windows.

Acceptance: `yuki-conductor daemon install` registers the task; reboot/
re-login starts the daemon; Slack messages route to Claude;
`yuki-conductor daemon status` reports running; `yuki-conductor daemon
uninstall` removes the task cleanly.

### Phase 3 (deferred) — ConPTY-based terminal

If we want browser terminal sessions on Windows, replace the PTY bridge
with `pywinpty` (ConPTY). Out of scope here.

## Testing strategy

- All existing tests must keep passing on macOS (no behavior change for
  the macOS adapter beyond a rename).
- New unit tests for the Windows adapter mock `subprocess.run` and assert
  the right `schtasks` invocations and XML content. They should run on
  macOS too — no real `schtasks.exe` invocation in CI.
- Smoke test on a real Windows host once Phase 1 lands: confirm
  `uv run yuki-conductor run` doesn't crash and the web UI loads.

## Risks

- **Slack Bolt Socket Mode on Windows**: pure-Python WebSocket; should be
  fine, but worth verifying first thing in Phase 1 as it's the load-bearing
  feature.
- **`uv run` startup latency on Windows**: noticeably slower than macOS;
  Task Scheduler's `RestartOnFailure` interval (15s) gives plenty of
  headroom.
- **Path length / OneDrive redirected `%USERPROFILE%`**: if the user's home
  is under OneDrive, SQLite over a synced folder can corrupt. Document
  setting `YUKI_CONDUCTOR_DATA_DIR` to a non-synced path on such machines.
