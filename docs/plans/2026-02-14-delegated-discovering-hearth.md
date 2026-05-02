# Plan: yuki-conductor

## Context

Build a Python CLI tool from scratch that bridges Slack and Claude Code CLI. The tool runs as a macOS daemon, listens to Slack messages via Socket Mode, invokes `claude -p --dangerously-skip-permissions` for each message, replies in Slack threads, and resumes Claude sessions on thread replies.

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Session tracking | JSON file (`~/.yuki-conductor/sessions.json`) | Simple, no extra dependency |
| CLI framework | `argparse` | No extra dependency |
| Daemon type | LaunchAgent (user-level `~/Library/LaunchAgents/`) | No root required |
| Subprocess | `subprocess.run` (sync) | slack-bolt thread pool handles concurrency |
| Build backend | `hatchling` | Standard, well-supported with uv |

## Project Structure

```
yuki-conductor/
  pyproject.toml
  .env.template
  .gitignore
  README.md
  docs/
    slack-app-setup.md          # Slack app creation guide
  src/
    yuki_conductor/
      __init__.py
      cli.py                    # argparse entry point
      daemon.py                 # install/uninstall/restart LaunchAgent
      slack_app.py              # slack-bolt Socket Mode handlers
      claude_runner.py          # subprocess wrapper for claude CLI
      session_store.py          # JSON thread_ts -> session_id mapping
      config.py                 # env loading, path constants
  tests/
    __init__.py
    test_session_store.py
    test_claude_runner.py
    test_cli_simulate.py        # integration tests via simulate commands
```

## Implementation Steps

### Step 1: Project Scaffolding
- Create `pyproject.toml` with dependencies: `slack-bolt>=1.18.0`, `python-dotenv>=1.0.0`; dev: `pytest>=8.0.0`
- Console script: `yuki-conductor = "yuki_conductor.cli:main"`
- Create `.gitignore`, `.env.template`, `src/yuki_conductor/__init__.py`
- Verify `uv run yuki-conductor --help` works

### Step 2: Config + Session Store
- `config.py`: Load `.env`, define paths (`DATA_DIR=~/.yuki-conductor/`, `SESSIONS_FILE`, `LOG_FILE`, `PLIST_PATH`), token accessors
- `session_store.py`: Thread-safe (threading.Lock) JSON read/write, `get(thread_ts) -> session_id | None`, `set(thread_ts, session_id)`
- `tests/test_session_store.py`: roundtrip, missing key, file creation, concurrent access

### Step 3: Claude Runner
- `claude_runner.py`: Run `claude -p --dangerously-skip-permissions --output-format json`, parse JSON for `result` + `session_id`, use `-r session_id` for resume
- Must unset `CLAUDECODE` env var in subprocess to avoid nested session error
- Configurable timeout (default 300s via `CLAUDE_TIMEOUT` env var)
- `tests/test_claude_runner.py`: Mock subprocess, test flag construction, error handling

### Step 4: CLI with Simulate Commands
- `cli.py` subcommands:
  - `run` — start Slack listener (foreground)
  - `daemon install|uninstall|restart|status|log` — manage LaunchAgent
  - `simulate message "text"` — bypass Slack, call claude_runner, print response + fake thread_ts
  - `simulate reply <thread_ts> "text"` — resume session from store, print response
- `tests/test_cli_simulate.py`: End-to-end test via `simulate message` then `simulate reply`

### Step 5: Slack App
- `slack_app.py`: Create `slack_bolt.App` with Socket Mode
- Message handler logic:
  - Skip messages with `subtype` (bot messages, edits, etc.)
  - Top-level message → new Claude session, reply in thread using message `ts` as `thread_ts`
  - Thread reply → look up session_id from store, resume; ignore if thread unknown
  - Add ⏳ reaction while processing, remove after
  - Store `thread_ts → session_id` mapping after each call

### Step 6: Daemon Management
- `daemon.py`: Generate plist XML programmatically
  - `ProgramArguments`: absolute path to `uv`, `run --project <project_dir> yuki-conductor run`
  - `KeepAlive: true`, `RunAtLoad: true`
  - `PATH` includes `~/.local/bin` for `claude` binary
  - Stdout/stderr to `~/.yuki-conductor/daemon.log` / `daemon.err.log`
- Commands: `launchctl load/unload`, plist file management, `launchctl list | grep` for status

### Step 7: Documentation
- `docs/slack-app-setup.md`: Step-by-step Slack app creation (Socket Mode enable, bot scopes: `chat:write`, `channels:history`, `groups:history`, `im:history`, `mpim:history`, `reactions:write`; event subscriptions: `message.channels/groups/im/mpim`; install to workspace)
- `README.md`: Quick-start guide
- `CLAUDE.md`: Development reference for future Claude Code sessions

### Step 8: Git
- `git init`, initial commit with all files
- Subsequent commits per logical step

## CLI Interface

```
yuki-conductor run                                    # Start listener (foreground)
yuki-conductor daemon install                         # Install + load LaunchAgent
yuki-conductor daemon uninstall                       # Unload + remove LaunchAgent
yuki-conductor daemon restart                         # Restart daemon
yuki-conductor daemon status                          # Check if running
yuki-conductor daemon log                             # Show log file paths
yuki-conductor simulate message "hello"               # Test without Slack
yuki-conductor simulate reply "1707900000.000100" "follow up"  # Resume session
```

## Key Implementation Details

- **Thread detection**: `thread_ts` in event → thread reply; absent → new top-level message
- **Session resume**: `claude -p --dangerously-skip-permissions -r <session_id> "prompt"`
- **Concurrency**: slack-bolt default thread pool (10 threads); each handler blocks on `subprocess.run`
- **Bot self-ignore**: Filter `event.get("subtype")` to skip bot messages
- **Large responses**: Initial implementation truncates at Slack's limit with a warning

## Verification

1. `uv run pytest` — unit tests for session_store and claude_runner (mocked)
2. `uv run yuki-conductor simulate message "What is 2+2?"` — should print Claude response + thread_ts
3. `uv run yuki-conductor simulate reply <thread_ts> "And 3+3?"` — should print contextual response
4. `uv run yuki-conductor daemon install && yuki-conductor daemon status` — verify daemon running
5. Send a Slack message to bot channel — verify threaded response (requires real Slack tokens)
