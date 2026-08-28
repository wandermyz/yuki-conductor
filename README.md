# yuki-conductor

A personal agent conductor for [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

yuki-conductor runs as a background daemon that spawns headless `claude -p` sessions,
tracks them, and routes them to wherever you are — a web UI on your local network, a
Slack thread, or a cron schedule. Sessions are persistent: every conversation maps to a
Claude session id, so replying to a thread days later resumes exactly where it left off.

It started as a Slack bridge. The Slack surface is still there, but it is now one of
several front ends and is entirely optional.

## What it does

- **Web UI** — a React app served by the daemon on port 2333, built for phone and
  desktop. Chat with Claude, watch its intermediate steps stream live, browse every
  session the daemon knows about, and attach files.
- **Chat apps** — Slack via Socket Mode out of the box, plus any chat platform
  installed as a plugin. Each session is tagged with its originating platform, so
  replies always route back to the right place.
- **Scheduled tasks** — cron expressions that open a thread, run a prompt, and post
  the result. Follow-up replies in that thread continue the conversation.
- **Terminal sessions** — attach to a live [Zellij](https://zellij.dev) session from
  the browser through xterm.js, for the times you want the interactive Claude Code
  TUI rather than a headless run (macOS only).
- **Skill injection** — headless runs get an advertised skill listing they would
  otherwise be missing, so a spawned session sees the same skills an interactive one
  would in that directory.
- **Proactive messages** — a running session can push messages into its own web
  conversation while it works, rather than staying silent until the final response.

## Architecture

```
                  ┌──────────────────────────────┐
   web browser ───┤                              │
   Slack       ───┤   yuki-conductor daemon      ├─── claude -p (headless)
   cron        ───┤   (FastAPI + receivers)      ├─── zellij (interactive TUI)
                  └──────────────┬───────────────┘
                                 │
                    SQLite: sessions, models,
                    conversations, statuses
```

Everything personal — the database, cron definitions, secrets, attachments — lives in
`~/.yuki-conductor/`, outside the repo.

## Quick start

### Prerequisites

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code), installed and authenticated
- Node.js and pnpm, to build the web frontend
- macOS, or Windows 10/11 with PowerShell 7+
- A Slack workspace *only* if you want the Slack surface

### Setup

```bash
mkdir -p ~/.yuki-conductor
cp .env.template ~/.yuki-conductor/.env
# Edit ~/.yuki-conductor/.env — see Configuration below
```

To run web-only, with no chat integration at all, set `CHAT_APPS=none` and skip the
Slack tokens entirely.

Build the frontend and start in the foreground:

```bash
cd web && pnpm install && pnpm build && cd ..
uv run yuki-conductor run
```

Open <http://localhost:2333>.

Install it as a daemon that starts at login — a LaunchAgent on macOS, a per-user
Scheduled Task on Windows, neither needing admin rights:

```bash
uv run yuki-conductor daemon install
uv run yuki-conductor daemon status
```

### Configuration

`~/.yuki-conductor/.env`:

| Variable | Purpose |
| --- | --- |
| `CHAT_APPS` | Comma-separated chat surfaces: `slack_socket`, plugin names, or `none` |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack credentials (only with `slack_socket`) |
| `SLACK_CRON_CHANNEL` | Channel that scheduled tasks post to |
| `CLAUDE_WORKING_DIR` | Default directory that spawned sessions run in |
| `CLAUDE_BIN` | Claude CLI path or name (default `claude`) |
| `CLAUDE_TIMEOUT` | Idle timeout in seconds — a run is stopped only after this long with no output |
| `WEB_PORT` | Web UI port (default `2333`) |
| `YUKI_CONDUCTOR_DATA_DIR` | Override the `~/.yuki-conductor` location |

For Slack setup, see [docs/slack-app-setup.md](docs/slack-app-setup.md).

## Web UI

Four tabs, with state kept in the URL hash so a reload or a shared link lands in the
same place.

**Chat** — the main surface. Messages stream Claude's intermediate steps (tool calls,
thinking, results) as they happen, and a browser that reconnects mid-run replays the
buffer and resumes watching. Conversations can be renamed, marked read/unread/done,
cancelled mid-run, and given file attachments. Drafts persist per conversation.

**Sessions** — every session the daemon has tracked, across Slack, chat plugins, and
Zellij, with deep links back into the originating Slack thread.

**Projects** — registered project directories with a file browser, used when creating
new terminal sessions.

**Status** — health of each chat receiver.

## Scheduled tasks

Define tasks in `~/.yuki-conductor/workspace/cron.yaml` (see
[cron.example.yaml](cron.example.yaml)):

```yaml
tasks:
  - name: morning-briefing
    schedule: "0 9 * * 1-5"
    description: "Morning briefing (weekdays at 9am)"
    prompt: >
      Summarize the git log from the past 24 hours and suggest
      the top 1-2 things to focus on today.
```

When a task fires it opens a new thread, runs the prompt, and posts the result. Add
`chat_app:` to pin a task to a particular surface; omit it and the first enabled
platform is used, preferring Slack. A task naming a platform that isn't enabled is
skipped with a warning rather than misrouted.

## Skills

A headless `claude -p` run receives no available-skills listing, in any scope — even
though skills *resolve* correctly when invoked by name. yuki-conductor closes that gap
by building the listing itself and injecting it via `--append-system-prompt`.

Skills are discovered in four tiers, each declared by where the skill lives:

| Tier | Source | Scope |
| --- | --- | --- |
| `YUKI` | bundled and plugin-contributed dirs | every session |
| `PROJECT` | `<cwd>/.claude/skills` | only when that project is the cwd |
| `ALWAYS` | `~/.claude/skills`, listed in `workspace/skills.yaml` | every session, emphasized |
| `USER` | the rest of `~/.claude/skills` | every session |

See [skills.example.yaml](skills.example.yaml). A personal system prompt at
`~/.yuki-conductor/workspace/system-prompt.md` is appended verbatim to every run.

## Outbound messages

A spawned run normally speaks only through its final response. `send` gives it a
second channel:

```bash
uv run yuki-conductor send -c <conversation-id> "halfway done, tests are green"
```

The text is persisted as an assistant message and broadcast over the chat WebSocket,
so connected browsers see it immediately and reconnecting ones find it in scrollback.
This reaches the web platform only.

## CLI reference

```
yuki-conductor run                              # Start the daemon in the foreground
yuki-conductor daemon install|uninstall         # Manage the auto-start daemon
yuki-conductor daemon restart|status|log        # Control and inspect it
yuki-conductor web rebuild                      # Rebuild frontend + hot-reload browsers
yuki-conductor send [-c <id>] "text"            # Push a message into a web conversation
yuki-conductor simulate message "hello"         # Run a prompt with no chat app attached
yuki-conductor simulate reply <ts> "follow up"  # Resume a session by thread id
```

## Windows notes

Everything works except Zellij terminal sessions, which are disabled — the
`/ws/terminal/...` WebSocket reports "not supported". The rest of the web UI behaves
identically.

If `%USERPROFILE%` is redirected into OneDrive, point `YUKI_CONDUCTOR_DATA_DIR` at a
non-synced path; SQLite and file sync do not mix.

## Development

```bash
uv run pytest              # Tests
uv run ruff check .        # Lint
cd web && pnpm dev         # Frontend dev server, proxies /api to port 2333
```

After frontend changes, `uv run yuki-conductor web rebuild` rebuilds and pushes a
reload to connected browsers. After backend changes, restart the daemon — the running
process holds the old code in memory.

## License

MIT — see [LICENSE](LICENSE).
