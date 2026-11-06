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
- macOS, Linux with systemd, or Windows 10/11 with PowerShell 7+
- A Slack workspace *only* if you want the Slack surface

### Setup

```bash
mkdir -p ~/.yuki-conductor
cp .env.template ~/.yuki-conductor/.env
# Edit ~/.yuki-conductor/.env — see Configuration below
```

Chat surfaces ("channels") are enabled in the **Plugins** tab, which writes
`~/.yuki-conductor/workspace/plugins.yaml`. A fresh install starts with the
built-in Slack channel present but disabled, so web-only needs no configuration
at all — just skip the Slack tokens. Setting `CHANNELS` in `.env` overrides the
registry if you need an escape hatch.

Build the frontend and start in the foreground:

```bash
cd web && pnpm install && pnpm build && cd ..
uv run yuki-conductor run
```

Open <http://localhost:2333>.

On macOS, install it as a LaunchAgent that starts at login (no admin rights
needed):

```bash
uv run yuki-conductor daemon install
uv run yuki-conductor daemon status
```

On Linux, the same commands install a systemd user service
(`~/.config/systemd/user/yuki-conductor.service`). It starts at login; run
`loginctl enable-linger` to start it at boot and keep it running while logged
out.

On Windows the `daemon` subcommand is unsupported — keep `yuki-conductor run`
alive with an external supervisor instead.

### Configuration

`~/.yuki-conductor/.env`:

| Variable | Purpose |
| --- | --- |
| `CHANNELS` | Optional override of the plugin registry: comma-separated channel names, or `none` |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack credentials (only with the Slack channel enabled) |
| `SLACK_CRON_CHANNEL` | Channel that scheduled tasks post to |
| `CLAUDE_WORKING_DIR` | Default directory that spawned sessions run in |
| `CLAUDE_BIN` | Claude CLI path or name (default `claude`) |
| `CLAUDE_TIMEOUT` | Idle timeout in seconds — a run is stopped only after this long with no output |
| `WEB_PORT` | Web UI port (default `2333`) |
| `WEB_HOST` | Web UI bind address (default `127.0.0.1`; set `0.0.0.0` to expose it on the network) |
| `WEB_FAVICON` | Path to a custom favicon image for the web UI (default: bundled `favicon.svg`) |
| `YUKI_CONDUCTOR_DATA_DIR` | Override the `~/.yuki-conductor` location |

For Slack setup, see [docs/slack-app-setup.md](docs/slack-app-setup.md).

### Model aliases and connections

Copy [models.example.yaml](models.example.yaml) to
`~/.yuki-conductor/workspace/models.yaml`. Each entry under `harness` has its
own `connections`, `aliases`, and `default` alias ID. Claude Code and Codex
require an explicit configuration; pi continues using its own provider catalog.

**Upgrading an existing machine:** complete the
[model configuration migration](docs/migrations/model-alias-connections.md)
before updating and restarting. It covers subscription and endpoint setups,
stored selections, verification, and rollback.

An alias names a literal `model`, a `connection`, and an optional display
`label`. Conversations, tasks, Slack pins and cron's `model:` store the alias
ID, so two aliases can run the same model on different endpoints. A null
selection follows the configured default. A deleted alias fails with an error;
it never falls back to another connection.

Connections use `type: subscription` (the CLI's existing signed-in account) or
`type: endpoint` with an explicit `base_url` and `api_key`. Claude endpoints
must speak the Anthropic Messages API; Codex endpoints must speak the Responses
API and often need `/v1` in the URL. No protocol translation is performed.
Per-run overrides take precedence over the CLI's existing provider settings,
while preserving its home directory, skills, authentication and session storage.

API keys must be references: `api_key: {env: GATEWAY_API_KEY}` or
`api_key: {file: ~/.yuki-conductor/.secrets/gateway-api-key}`. Exactly one is
required for an endpoint; plaintext keys are rejected. Relative file paths
resolve beside models.yaml. Keep secret files outside the synced workspace and
repository, restrict Windows ACLs to your user, or use mode `600` on Unix.
Environment references can use the daemon's private `.env`. Never put keys in
URLs. Keys are read only at launch and passed through child environment variables;
they are excluded from generated settings, command lines and picker responses,
and redacted from surfaced results and live steps. Agent processes can access
their own environment, so this is not a secret boundary against agent tools.

Changes to models.yaml and secret files apply to subsequent turns without a
daemon restart. Existing sessions resume on the selected route; switching a route
sends that session's context to the newly selected endpoint.


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

Set `harness:` and `model:` to choose a harness and its configured alias ID;
omit `model:` to follow that harness's default.

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

A spawned run normally speaks only through its final response, and attaches files
by writing `<attachment>/absolute/path</attachment>` in it. `send` gives it a second
channel for messages and files mid-turn:

```bash
uv run yuki-conductor send -c <conversation-id> "halfway done, tests are green"
uv run yuki-conductor send -c <conversation-id> --attach /tmp/plot.png "latest run"
```

The daemon delivers it through whichever platform owns the conversation (web chat
or a Slack thread), the same way it delivers a normal reply.

## CLI reference

```
yuki-conductor run                              # Start the daemon in the foreground
yuki-conductor daemon install|uninstall         # Manage the auto-start daemon (macOS, Linux)
yuki-conductor daemon restart|status|log        # Control and inspect it (macOS, Linux)
yuki-conductor web rebuild                      # Rebuild frontend + hot-reload browsers
yuki-conductor send [-c <id>] [-a FILE] "text"  # Push a message/files into a conversation
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
