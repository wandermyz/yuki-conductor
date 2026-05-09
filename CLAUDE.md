# yuki-conductor Development Reference

## Project Structure

```
src/yuki_conductor/
  cli.py            — argparse entry point (run, daemon, simulate)
  config.py         — env loading, path constants, CHAT_APPS parsing
  store.py          — SQLite-backed session & model stores
  claude_runner.py  — subprocess wrapper for claude CLI
  runtime.py        — process orchestrator (starts receivers + web + cron)
  slack_app.py      — Slack Bolt handlers + SlackSocketReceiver
  messaging/        — platform-agnostic messaging core
    platform.py        — MessagingPlatform / ChatAppReceiver Protocols + types
    conversation.py    — handle_incoming_message: shared run_claude orchestration
    slack_platform.py  — Slack adapter
    teams_cli_platform.py — Teams CLI adapter (placeholder until binary lands)
    web_platform.py    — Web chat adapter
  cron_scheduler.py — cron task scheduler (reads ~/.yuki-conductor/workspace/cron.yaml)
  daemon.py         — macOS LaunchAgent management
  web_server.py     — FastAPI HTTP server (agent conductor web UI)
web/                — React + Vite frontend (pnpm, TypeScript)
```

## Workspace

The personal workspace lives outside the repo at `~/.yuki-conductor/` (override with `YUKI_CONDUCTOR_DATA_DIR`). This is where all personal information — cron task definitions, secrets, attachments, the SQLite DB — is stored. **Do not include any personal information in the repo itself** — anything in the repo can end up in git.

Key files:
- `~/.yuki-conductor/.env` — secrets and env overrides (loaded at startup)
- `~/.yuki-conductor/workspace/yuki-conductor.db` — SQLite database for session and model tracking
- `~/.yuki-conductor/workspace/cron.yaml` — Cron task definitions (see `cron.example.yaml` for format)
- `~/.yuki-conductor/workspace/attachments/`, `uploads/` — runtime file storage
- `~/.yuki-conductor/daemon.log`, `daemon.err.log` — LaunchAgent logs

## Chat Apps

The daemon's chat surfaces are selected by `CHAT_APPS` (comma-separated):

- `slack_socket` (default) — slack-bolt Socket Mode using `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`.
- `teams_cli` — Microsoft Teams via the (in-development) `teams-cli` binary. Currently a placeholder receiver: outbound messages log only.
- empty / `none` — no chat receivers. Web server and cron scheduler still run; cron notifications are logged instead of posted.

Multiple values may be combined: `CHAT_APPS=slack_socket,teams_cli`. Each session is platform-tagged so replies always route back to the originating chat app.

## Cron Scheduler

The daemon supports scheduled tasks via `~/.yuki-conductor/workspace/cron.yaml`. Each task specifies a cron expression, a description, a Claude prompt, and optionally `chat_app` (`slack_socket` or `teams_cli`) to control where the notification goes. When the cron fires, the routed platform opens a new thread and runs Claude Code with the prompt, posting the result. The thread is session-tracked, so follow-up replies in that thread continue the conversation.

Routing rule when `chat_app:` is omitted: pick the first enabled platform, with `slack` preferred. If `chat_app:` names a platform that isn't in `CHAT_APPS`, the task is skipped with a warning rather than misrouted.

Required env var (only when `slack_socket` is enabled): `SLACK_CRON_CHANNEL` — the Slack channel ID to post cron results to.

## Key Commands

- `uv run pytest` — run all tests
- `uv run ruff check .` — run linter
- `uv run ruff check --fix .` — run linter with auto-fix
- `uv run yuki-conductor --help` — show CLI help
- `uv run yuki-conductor simulate message "test"` — test without Slack
- `cd web && pnpm dev` — start frontend dev server (proxies /api to port 2333)
- `cd web && pnpm build` — build frontend for production (output: web/dist/)

## Frontend Deployment Gotchas

The production daemon serves the **built** frontend from `web/dist/`. Two things consistently go wrong:

1. **Stale build**: Editing `web/src/` does nothing until you run `cd web && pnpm build`. The daemon serves whatever was last built into `web/dist/`, not the live source. Always rebuild after frontend changes.
2. **Stale daemon**: The running process keeps the old code in memory. After rebuilding (or after any backend change), you must **restart the daemon** — and make sure the old process on port 2333 is actually dead first, or the new one silently fails to bind.
3. **Worktree builds don't carry over**: If you build frontend in a git worktree, the hashed asset filenames (e.g. `index-BFz3Cwkn.js`) differ from the main branch. After merging, always rebuild in the main worktree.

**Checklist after any frontend or backend change**: rebuild frontend → kill old process → start new daemon → verify endpoint.

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
- **Claude invocation**: `claude -p --dangerously-skip-permissions --output-format json [-r session_id] "prompt"`
- **Environment**: Must unset `CLAUDECODE` env var in subprocess to avoid nested session errors

## Testing

- `test_store.py` — unit tests for SQLite store (roundtrip, concurrent access, separate tables)
- `test_claude_runner.py` — mocked subprocess tests (flag construction, errors, timeouts)
- `test_cli_simulate.py` — integration tests via simulate commands
