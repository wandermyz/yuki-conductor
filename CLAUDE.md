# yuki-conductor Development Reference

## Project Structure

```
src/yuki_conductor/
  cli.py            — argparse entry point (run, daemon, simulate)
  config.py         — env loading, path constants, CHAT_APPS parsing
  store.py          — SQLite-backed session & model stores
  claude_runner.py  — subprocess wrapper for claude CLI (stream-json reader)
  stream_events.py  — normalizes stream-json records into UI progress events
  skills.py         — Claude Code skill-plugin injection for spawned sessions
  runtime.py        — process orchestrator (starts receivers + web + cron)
  slack_app.py      — Slack Bolt handlers + SlackSocketReceiver
  messaging/        — platform-agnostic messaging core
    platform.py        — MessagingPlatform / ChatAppReceiver Protocols + types
    conversation.py    — handle_incoming_message: shared run_claude orchestration
    slack_platform.py  — Slack adapter
    web_platform.py    — Web chat adapter
  cron_scheduler.py — cron task scheduler (reads ~/.yuki-conductor/workspace/cron.yaml)
  daemon.py         — macOS LaunchAgent management
  web_server.py     — FastAPI HTTP server (agent conductor web UI)
web/                — React + Vite frontend (pnpm, TypeScript)
plugins/
  yuki-conductor/   — Claude Code plugin bundled in this repo (cron skill)
```

## Workspace

The personal workspace lives outside the repo at `~/.yuki-conductor/` (override with `YUKI_CONDUCTOR_DATA_DIR`). This is where all personal information — cron task definitions, secrets, attachments, the SQLite DB — is stored. **Do not include any personal information in the repo itself** — anything in the repo can end up in git.

Key files:
- `~/.yuki-conductor/.env` — secrets and env overrides (loaded at startup, overrides the Claude env file)
- `~/.zshenv.d/claude.zsh` — exports `ANTHROPIC_AUTH_TOKEN` (read from `.secrets/claude-auth-token`), shared with interactive shells; `config.py` sources this one file under `zsh -f` at startup so the LaunchAgent authenticates the same way a terminal does. The endpoint is *not* here — `ANTHROPIC_BASE_URL` lives in `~/.claude/settings.json` under `env`, which applies to Claude Code only rather than every process that starts a zsh.
- `~/.yuki-conductor/.secrets/` — one secret per file, mode 600 (referenced by the above; never in `workspace/`, which syncs to Obsidian)
- `~/.yuki-conductor/workspace/yuki-conductor.db` — SQLite database for session and model tracking
- `~/.yuki-conductor/workspace/cron.yaml` — Cron task definitions (see `cron.example.yaml` for format)
- `~/.yuki-conductor/workspace/system-prompt.md` — optional personal system prompt appended to every spawned Claude run (see Skill Injection)
- `~/.yuki-conductor/workspace/attachments/`, `uploads/` — runtime file storage
- `~/.yuki-conductor/daemon.log`, `daemon.err.log` — LaunchAgent logs

## Chat Apps

The daemon's chat surfaces are selected by `CHAT_APPS` (comma-separated):

- `slack_socket` (default) — slack-bolt Socket Mode using `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`.
- any other name — resolved as an installed chat-app plugin via the
  `yuki_conductor.chat_plugins` entry-point group (see
  `docs/plans/chat-plugin-architecture.md`). Plugins live in separate packages;
  install one into the same environment and enable it by its entry-point name.
- empty / `none` — no chat receivers. Web server and cron scheduler still run; cron notifications are logged instead of posted.

Multiple values may be combined: `CHAT_APPS=slack_socket,my_plugin`. Each session is platform-tagged so replies always route back to the originating chat app.

## Skill Injection

yuki-conductor spawns headless `claude -p` runs whose working directory often
points at some other project. To teach those runs about yuki-conductor's own
capabilities (cron scheduling, connected messaging surfaces),
`claude_runner.py` injects Claude Code **plugins** per-session via `--plugin-dir`,
plus an `--append-system-prompt`. Because these are session-scoped, they are
invisible when the user runs Claude Code directly — nothing is written to
`~/.claude/skills`.

**A headless `claude -p` run does not receive the available-skills listing that
interactive sessions get.** Plugin skills resolve by name via the `Skill` tool,
but the model never discovers them on its own — asked "what skills do you have",
it will truthfully answer "none". So `skills.system_prompt()` builds the appended
prompt dynamically: it reads the `name` and `description` frontmatter from every
discovered `skills/*/SKILL.md` and lists them, with an explicit routing rule
(`yuki-conductor-cron` for all scheduling). Entry-point plugins are picked up
automatically — no hardcoded names.

A headless run also loads no settings by default, so `run_claude` passes
`--setting-sources user,project,local`. That makes user-scope skills in
`~/.claude/skills` resolvable by name — though, like plugin skills, still not
*discoverable*, so anything you want used must be named in the prompt.

`skills.py` collects the plugin dirs:

- the bundled `plugins/yuki-conductor` plugin (`yuki-conductor-cron` for the cron
  scheduler);
- any dirs contributed by installed packages through the
  `yuki_conductor.skill_plugins` entry-point group (each entry point is a
  zero-arg callable returning a plugin dir path). This lets an installed chat
  plugin ship its own skill without this public repo naming it.

Finally, if `~/.yuki-conductor/workspace/system-prompt.md` exists, its contents
are appended verbatim. That file is outside the repo, so it's where private
capabilities belong — internal CLIs, user-scope skills, MCP servers. **Never name
those in the repo-side prompt.** Absent or empty file is a no-op.

The spawned run also gets `YUKI_CONDUCTOR_PROJECT` in its env so injected skills
can locate and invoke the yuki-conductor CLI.

## Cron Scheduler

The daemon supports scheduled tasks via `~/.yuki-conductor/workspace/cron.yaml`. Each task specifies a cron expression, a description, a Claude prompt, and optionally `chat_app` (`slack_socket` or an installed chat plugin's name) to control where the notification goes. When the cron fires, the routed platform opens a new thread and runs Claude Code with the prompt, posting the result. The thread is session-tracked, so follow-up replies in that thread continue the conversation.

Routing rule when `chat_app:` is omitted: pick the first enabled platform, with `slack` preferred. If `chat_app:` names a platform that isn't in `CHAT_APPS`, the task is skipped with a warning rather than misrouted.

Required env var (only when `slack_socket` is enabled): `SLACK_CRON_CHANNEL` — the Slack channel ID to post cron results to.

## Key Commands

- `uv run pytest` — run all tests
- `uv run ruff check .` — run linter
- `uv run ruff check --fix .` — run linter with auto-fix
- `uv run yuki-conductor --help` — show CLI help
- `uv run yuki-conductor simulate message "test"` — test without Slack
- `uv run yuki-conductor web rebuild` — rebuild frontend + auto-reload connected browsers
- `cd web && pnpm dev` — start frontend dev server (proxies /api to port 2333)
- `cd web && pnpm build` — build frontend for production (output: web/dist/)

## Daemon Management

The `daemon` subcommand (`install`/`uninstall`/`restart`/`status`/`log`) is
dispatched by platform in `daemon.py`:

- macOS (`daemon_macos.py`) — a per-user LaunchAgent (`launchctl` + plist).
- Windows (`daemon_windows.py`) — a per-user Task Scheduler task
  (`schtasks` + a Logon-triggered task named `YukiConductor`) that launches
  `bin/yuki-conductor-daemon.ps1`. No admin elevation required; runs only
  while the user is logged in.

Shared helpers (uv discovery, web build, log tailing) live in
`daemon_common.py`.

To restart the Windows daemon manually instead of via the task, find the
running `yuki-conductor` process, kill it, and spawn a new one:

```
# Find and kill
taskkill /f /im yuki-conductor.exe 2>/dev/null; tasklist | grep yuki
# Or: Get-Process *yuki* | Stop-Process -Force

# Start in background
uv run yuki-conductor run &
```

## Frontend Deployment Gotchas

The production daemon serves the **built** frontend from `web/dist/`.

**After frontend-only changes**, run:
```
uv run yuki-conductor web rebuild
```
This rebuilds the frontend and, if the daemon is running, broadcasts a WebSocket reload to all connected browsers automatically. No daemon restart needed.

**After backend changes** (Python code), you must restart the daemon — the running process keeps old code in memory.

Other things that can go wrong:

1. **Worktree builds don't carry over**: If you build frontend in a git worktree, the hashed asset filenames (e.g. `index-BFz3Cwkn.js`) differ from the main branch. After merging, always rebuild in the main worktree.
2. **Hash mismatch after merge**: `web/dist/` is gitignored, so only `index.html` is tracked. If a merge updates `index.html` to reference new hashed filenames but the actual JS/CSS files on disk are from an older build, the page loads blank (404 on assets). Always rebuild after any merge that touches frontend code.

## Pre-commit / Pre-PR Checks

Before making any commit or creating a PR, you MUST run both:

1. `uv run pytest` — all tests must pass
2. `uv run ruff check .` — linter must report no errors

Do not commit or open a PR if either fails.

## Code Style

Keep the code clean. **No backward compatibility is required.** This project has a single user and no external consumers, so:

- Don't support old formats of `.env`, `cron.yaml`, the SQLite schema, CLI flags, or any other config. When a format changes, update the file in place and delete the old code path.
- Don't add migration shims, deprecated-field fallbacks, or "if old format then…" branches. Just change the format and the code together.
- Don't keep removed functions/flags as aliases or re-exports. Delete them.
- Don't write `# kept for backwards compatibility` comments — there is no such requirement.
- When renaming or restructuring, update all call sites and config files in the same change. Don't leave a transition period.

## Architecture

- **Session tracking**: SQLite database at `~/.yuki-conductor/workspace/yuki-conductor.db` maps `thread_ts → (session_id, channel_id)` and `channel_id → model`
- **Web server**: FastAPI on port 2333 (env: `WEB_PORT`), serves React frontend and `/api/sessions` + chat endpoints. Starts in a daemon thread alongside any enabled chat-app receivers.
- **Concurrency**: slack-bolt's default thread pool (10 threads); each handler blocks on `subprocess.run`
- **Claude invocation**: `claude -p --dangerously-skip-permissions --output-format stream-json --verbose [-r session_id] "prompt"`. stdout is NDJSON; `stream_events.py` normalizes each record into a `StreamEvent` (tool calls, text, thinking, tool results) which `run_claude(on_event=...)` delivers live, and the terminal `result` record becomes the returned `ClaudeResult`.
- **Progress surfaces**: platforms opt into live steps by implementing `on_stream_event`. Web buffers steps in `ConnectionManager` (memory only — never persisted) and broadcasts `{"type": "step"}`; `GET /api/chat/steps` replays the buffer so a reconnecting browser resumes mid-run. Slack folds steps into the shimmering assistant status (coalesced to one `setStatus` call per couple of seconds) and posts nothing until the final reply. Platforms without the hook just get the final response.
- **Environment**: Must unset `CLAUDECODE` env var in subprocess to avoid nested session errors

## Testing

- `test_store.py` — unit tests for SQLite store (roundtrip, concurrent access, separate tables)
- `test_claude_runner.py` — mocked subprocess tests (flag construction, errors, timeouts)
- `test_cli_simulate.py` — integration tests via simulate commands
