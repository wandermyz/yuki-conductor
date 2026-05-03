# yuki-conductor

Bridge Slack messages to Claude Code CLI. Messages sent to the bot start a new Claude session; replies in the thread resume the same session for continuous conversation.

## Quick Start

### Prerequisites

- macOS, or Windows 10/11 with PowerShell 7+ (foreground `run` only — daemon install is macOS-only for now; see [docs/plans/2026-05-02-windows-support.md](docs/plans/2026-05-02-windows-support.md))
- [uv](https://docs.astral.sh/uv/) package manager
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Slack workspace with admin access

### Setup

1. **Create a Slack app** — Follow [docs/slack-app-setup.md](docs/slack-app-setup.md)

2. **Configure environment**
   ```bash
   mkdir -p ~/.yuki-conductor
   cp .env.template ~/.yuki-conductor/.env
   # Edit ~/.yuki-conductor/.env with your Slack tokens
   ```

3. **Test locally**
   ```bash
   # Test without Slack (calls real Claude CLI)
   uv run yuki-conductor simulate message "What is 2+2?"

   # Resume the session
   uv run yuki-conductor simulate reply <thread_ts> "And 3+3?"
   ```

4. **Run in foreground**
   ```bash
   uv run yuki-conductor run
   ```

5. **Install as daemon** (auto-starts on login)
   ```bash
   uv run yuki-conductor daemon install
   uv run yuki-conductor daemon status
   ```

## CLI Reference

```
yuki-conductor run                              # Start listener (foreground)
yuki-conductor daemon install                   # Install + load LaunchAgent
yuki-conductor daemon uninstall                 # Unload + remove LaunchAgent
yuki-conductor daemon restart                   # Restart daemon
yuki-conductor daemon status                    # Check if running
yuki-conductor daemon log                       # Show log file paths + recent output
yuki-conductor simulate message "hello"         # Test without Slack
yuki-conductor simulate reply <ts> "follow up"  # Resume session
```

## Windows

`yuki-conductor run` works on Windows; the macOS daemon installer
(`yuki-conductor daemon ...`) does not. Use PowerShell:

```powershell
mkdir $env:USERPROFILE\.yuki-conductor
copy .env.template $env:USERPROFILE\.yuki-conductor\.env
# Edit the .env in your editor of choice
uv run yuki-conductor run
```

Caveats:

- Zellij terminal sessions are disabled on Windows (the
  `/ws/terminal/...` WebSocket returns "not supported"). The rest of the
  web UI (chat, sessions list, cron) works the same as on macOS.
- If your `%USERPROFILE%` is redirected into OneDrive, set
  `YUKI_CONDUCTOR_DATA_DIR` to a non-synced path to avoid SQLite
  corruption.

A Windows-native daemon (Task Scheduler-based) is planned — see
[docs/plans/2026-05-02-windows-support.md](docs/plans/2026-05-02-windows-support.md).

## Development

```bash
uv run pytest
```

## How It Works

1. The bot listens for Slack messages via [Socket Mode](https://api.slack.com/apis/socket-mode)
2. Each new message spawns `claude -p --dangerously-skip-permissions --output-format json`
3. The response is posted as a thread reply
4. Thread replies look up the stored `session_id` and resume with `claude -r <session_id>`
5. Session mappings (`thread_ts -> session_id`) and per-channel model settings are persisted in the SQLite DB at `~/.yuki-conductor/workspace/yuki-conductor.db`
