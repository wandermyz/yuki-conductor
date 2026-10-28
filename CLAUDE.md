# yuki-conductor Development Reference

## Shared Agent Instructions

`CLAUDE.md` is the single source for this development reference. `AGENTS.md`
is a relative symlink to it so Codex reads the same instructions. Likewise,
`.agents/skills` links to `../.claude/skills` for Codex's native project skill
discovery. Edit the Claude paths to update both tools; keep the links intact.
`.codex/config.toml` raises Codex's instruction size limit to 64 KiB so this
reference fits (project config is loaded for trusted projects).

## Project Structure

```
src/yuki_conductor/
  cli.py            — argparse entry point (run, daemon, simulate)
  config.py         — env loading, path constants, CHANNELS override parsing
  plugins.py        — plugin identity, injection points, discovery + status
  plugin_config.py  — plugins.yaml parsing and surgical field edits
  store.py          — SQLite-backed session & model stores
  task_store.py     — SQLite-backed taskboard tasks + comments, pause switch
  taskboard/        — the taskboard (see docs/plans/2026-10-18-taskboard*.md)
    transitions.py     — the only code that changes a task's status; listener hooks
    harvester.py       — pickup loop: per-harness limits, pause, restart recovery
    runner.py          — one task turn: worktree, prompt, run_agent, next status
    prompts.py         — first-turn / follow-up / fresh-session prompts
    worktree.py        — per-task git worktrees
    live.py            — live steps + change broadcasts to the web UI
    actions.py         — create/edit/comment/cancel/done: what the API and CLI call
    routes.py          — /api/tasks + /api/taskboard HTTP routes
    notify.py          — web push when a task needs the user
    pr_watcher.py      — `gh` check of Ready for Review PRs, run when the Tasks tab opens
    cleanup.py         — removes a Done task's worktree + branches
  cli_task.py       — `yuki-conductor task …` (show/list/comment/ask/pr/create)
  cli_search.py     — `yuki-conductor search` + `conversation show`, with web UI links
  cli_http.py       — CLI → daemon HTTP helper (shared with `send`)
  harness/          — the agent CLIs a turn can run on (see Harnesses)
    base.py            — StreamEvent, RunResult, Harness / StreamParser protocols
    process.py         — run_agent: subprocess, idle watchdog, cancel registry
    claude_code.py     — Claude Code: command line + stream-json parser
    pi.py              — pi: command line + `--mode json` parser
    codex.py           — Codex: `codex exec --json` command line + parser
  skills.py         — skill discovery + system-prompt synthesis for spawned sessions
  runtime.py        — process orchestrator (starts receivers + web + cron)
  slack_app.py      — Slack Bolt handlers + SlackSocketReceiver + create_receiver
  messaging/        — platform-agnostic messaging core
    platform.py        — MessagingPlatform / ChatAppReceiver Protocols + types
    conversation.py    — handle_incoming_message: harness choice + run_agent orchestration
    slack_platform.py  — Slack adapter
    web_platform.py    — Web chat adapter
  cron_scheduler.py — cron task scheduler (reads ~/.yuki-conductor/workspace/cron.yaml)
  cron_config.py    — cron.yaml parsing, cadence prose, surgical field edits
  daemon.py         — daemon management dispatcher (daemon_macos / daemon_linux / daemon_windows)
  web_server.py     — FastAPI HTTP server (agent conductor web UI)
  web_push.py       — browser push notifications (VAPID key, subscriptions, fan-out)
  updater.py        — self-update: git fetch + rebase onto upstream (see Self-update)
web/                — React + Vite frontend (pnpm, TypeScript)
  src/tasks/          — the Tasks tab (list, detail, form)
  src/Markdown.tsx    — shared Markdown renderer; ```mermaid via pele
plugins/
  yuki-conductor/   — bundled skill plugin, injected into every session (cron, send, task skills)
  slack/            — bundled channel plugin (Slack Socket Mode)
examples/
  echo-plugin/      — reference plugin: manifest, channel, skill dir
.claude/skills/     — project-scoped skills, only advertised when cwd is this repo
```

## Workspace

The personal workspace lives outside the repo at `~/.yuki-conductor/` (override with `YUKI_CONDUCTOR_DATA_DIR`). This is where all personal information — cron task definitions, secrets, attachments, the SQLite DB — is stored. **Do not include any personal information in the repo itself** — anything in the repo can end up in git.

Key files:
- `~/.yuki-conductor/.env` — secrets and env overrides (loaded at startup, overrides the Claude env file)
- `~/.yuki-conductor/favicon.png` — optional personal web favicon, served by `/api/favicon` when `.env` sets `WEB_FAVICON` (falls back to the bundled `favicon.svg`)
- `~/.zshenv.d/claude.zsh` — exports `ANTHROPIC_AUTH_TOKEN` (read from `.secrets/claude-auth-token`), shared with interactive shells; `config.py` sources this one file under `zsh -f` at startup so the LaunchAgent authenticates the same way a terminal does. The endpoint is *not* here — `ANTHROPIC_BASE_URL` lives in `~/.claude/settings.json` under `env`, which applies to Claude Code only rather than every process that starts a zsh.
- `~/.yuki-conductor/.secrets/` — one secret per file, mode 600 (referenced by the above; never in `workspace/`, which syncs to Obsidian)
- `~/.yuki-conductor/workspace/yuki-conductor.db` — SQLite database for session and model tracking
- `~/.yuki-conductor/workspace/cron.yaml` — Cron task definitions (see `cron.example.yaml` for format)
- `~/.yuki-conductor/workspace/plugins.yaml` — plugin registry: installed and enabled (see `plugins.example.yaml`)
- `~/.yuki-conductor/workspace/skills.yaml` — promotes user-scope skills to "always available" (see `skills.example.yaml` and Skill Discovery)
- `~/.yuki-conductor/workspace/system-prompt.md` — optional personal system prompt appended to every spawned Claude run (see Skill Discovery)
- `~/.yuki-conductor/workspace/attachments/`, `uploads/` — runtime file storage
- `~/.yuki-conductor/daemon.log`, `daemon.err.log` — daemon logs (LaunchAgent / systemd unit)

## Plugins and Channels

A **plugin** is a named unit that contributes capability at one or more
**injection points**. `plugins.py` owns the concept; `plugin_config.py` owns the
registry file.

| Injection point | Status | Contribution |
| --- | --- | --- |
| `channels` | implemented | a `ChatAppReceiver` factory — a chat surface the daemon listens on |
| `skills` | implemented | a Claude Code plugin dir injected via `--plugin-dir` |
| `cli` | implemented | subcommands grafted onto `yuki-conductor` |
| `triggers` | planned | a source of automation events |

Plugins reach the registry three ways, merged in that precedence order:

1. **builtin** — bundled in-tree under `plugins/<name>/`, with its own
   `yuki-plugin.yaml` like any other plugin. Slack is the only one. There is no
   built-in code path: `_builtin_descriptor` just resolves the directory from
   the repo instead of the registry record, then runs the same manifest
   discovery, and `runtime._build_receiver` calls the same `module:attr`
   factory with the same arguments. "builtin" means *where it lives*, not a
   different kind of plugin. It can be disabled but not removed, since deleting
   the record wouldn't delete the code.
2. **path** — a directory registered in `~/.yuki-conductor/workspace/plugins.yaml`,
   declaring itself in a `yuki-plugin.yaml` manifest at its root. See
   `examples/echo-plugin` for a complete one. If the manifest sets
   `python_path:`, those dirs are **appended** to `sys.path` (appended, not
   prepended, so a plugin can't shadow a stdlib module) when the plugin is
   enabled — that's what makes "just point at a folder" work without an
   editable install.
3. **entry_point** — an installed package declaring `yuki_conductor.chat_plugins`
   / `.skill_plugins` / `.cli_plugins`. Still supported with no manifest; the
   three groups are merged by entry-point name into one plugin.

`discover_plugins()` **never raises.** A missing path yields `status="missing"`,
a failed import `status="error"` with the message; both still render in the UI
and neither is fatal at startup. A *disabled* plugin is never imported at all —
discovery reads manifests without executing plugin code.

Enablement lives in `plugins.yaml`, not the environment. `CHANNELS` is only an
override/ordering escape hatch: when set it wins outright (`none`/empty = no
channels), and `runtime.start()` logs which source resolved the channel list.
Order matters — the cron scheduler uses the first enabled platform as its
default notification target.

Toggling a plugin off also stops advertising its **skills** to spawned sessions
(`skills.skill_plugin_dirs` → `plugins.enabled_skill_dirs`), which is what a
user toggling it off expects.

### Plugins tab

`/api/plugins` (GET/POST/PATCH/DELETE) plus `POST /api/daemon/restart`, surfaced
by `web/src/PluginsView.tsx`. Every mutation is a `plugins.yaml` edit that only
takes effect when receivers are rebuilt, i.e. at startup — so each response
carries `restart_required: true` and the UI shows a banner until a restart. This
is deliberate: hot-swapping live receivers is a much bigger change than
pretending to.

`POST /api/daemon/restart` must not kill its own caller, since the web server
runs *inside* the daemon. `daemon.spawn_detached_restart()` dispatches to
`daemon_windows` (schedule `os._exit(0)` a few seconds out and let yuki-watcher
relaunch), `daemon_macos` (`setsid` + `launchctl kickstart -k`), or
`daemon_linux` (delayed `systemctl --user restart --no-block`), returns 202 at
once with the daemon's `boot_id`, and the frontend polls `/api/daemon` until it
reports a different one. Neither a reply nor an error alone means anything: the
old daemon answers for a few seconds, and while it's down `tailscale serve`
returns 502 instead of failing the request.

### Self-update

The daemon assumes it runs from a git checkout. `GET /api/update` fetches
origin and lists the commits on the branch's upstream (`@{upstream}`) that the
checkout lacks; `POST /api/update` fetches, `git rebase`s onto it, rebuilds the
frontend if `web/` changed, and restarts via `spawn_detached_restart()` — or
returns `restarting: false` when already current. Tracked local changes, a
detached HEAD, no upstream, a conflicting rebase (always aborted) or a failed
frontend build are a 409 with a readable `detail`. `updater.py` serializes git
behind one lock, since every open browser tab polls. The GET also reports `running`,
the in-flight agent runs by kind (`chat`, `task`, `automation`), counted by
`run_agent`'s `kind=` argument in `harness/process.py` — an update restarts
the daemon and would kill them.

The frontend (`UpdatePrompt.tsx`, mounted in `App`) polls every 5 minutes and,
unless an agent run is in flight, pops a modal listing the new commits with **Update & restart**, **Cancel**
(back at the next poll) and **Snooze 1 hour** (`localStorage`). The Plugins tab has an **Update** button beside
Restart daemon. Either reloads the page once the daemon is back.

### Archived conversations

The chat menu's **Archive** sets `conversations.archived_at`. An archived
conversation keeps its rows but never appears in the web UI again: it's dropped
from `/api/conversations`, and `GET /api/conversations/{id}` and its
`/messages` 404. There's no unarchive and no UI to browse them — query the DB
directly (`SELECT … FROM conversations WHERE archived_at IS NOT NULL`).

### Conversation search

The search box at the top of the chat sidebar calls `GET /api/search?q=`,
which is `ConversationStore.search()`: keyword search over web messages
through `messages_fts`, a regular FTS5 table of each message's text, keyed by
`messages.rowid`. The store's own writes keep it in step from Python (no
triggers), so a raw SQL write isn't indexed. `search_indexes` records each
index's `SEARCH_INDEX_VERSION`; `_init_search_index` rebuilds an index only
when that differs, in one `BEGIN IMMEDIATE` transaction, so CLI processes that
open the store don't redo it. Bump the version whenever what an index holds
changes (columns, tokenizer, `cjk_split`). Chinese, Japanese and Korean have no
spaces, so `cjk_split` surrounds every CJK character with U+200B, which the
tokenizer treats as a separator: each character is a token. Only the index
holds the split text; `messages` is never touched. Each whitespace-separated
query term is split the same way and becomes a quoted prefix phrase, ANDed
together (`fts_query`), so 认证 matches inside 认证方式 like a substring and FTS
syntax in the box is just text. Snippets are `cjk_unsplit` back. You get one hit per
conversation (its best message), and archived conversations are excluded.
The snippet wraps matched terms in `\x02…\x03` (`SNIPPET_MARK`), which the
sidebar renders as `<mark>`. Clicking a hit opens `#chat/<id>` and scrolls to
and flashes that message. Tasks have the same search over title, description
and non-system comments (`TaskStore.search`, `tasks_fts` + `task_comments_fts`,
`GET /api/tasks/search`), one hit per task; a match in the task itself
ranks above any comment match, title over description. The Tasks sidebar has
the same search box: a hit opens the task and scrolls to the matching comment.

`yuki-conductor search [--conversations|--tasks] TERMS` (`cli_search.py`)
runs both over HTTP and prints Markdown hits, each with a link into the web
UI (`#chat/<id>`, `#tasks/<id>`); `conversation show <id>` prints a
conversation's messages, and `task show` a link too. Links are built on
`WEB_PUBLIC_URL` (`.env`, e.g. the `tailscale serve` URL; defaults to
`http://127.0.0.1:<WEB_PORT>`). The bundled `yuki-conductor-search` skill
documents it for agents. Semantic search is
planned behind the same `search()`: see
`docs/plans/2026-10-19-conversation-semantic-search.md`.

### Web tabs

The tab bar holds four: Chat, Tasks, Automations, and a `⋯` overflow.
Projects, Sessions, Status, and Plugins live behind the overflow
(`OVERFLOW_TABS` in `App.tsx`), which renders a list view and shows as active
whenever one of its children is current. Deep links (`#projects`, `#plugins`,
`#tasks/<id>`, …) remain valid.


## Skill Discovery

yuki-conductor spawns headless `claude -p` runs whose working directory often
points at some other project. The goal is that such a run sees the same skills an
interactive session in that directory would.

**A headless `claude -p` run receives no available-skills listing, in any scope.**
Verified empirically: even with `--setting-sources user,project,local` *and*
`--plugin-dir`, the model reports no skills — plugin, user-scope, and
project-scope alike. But *resolution* works in all three: a skill invoked by name
via the `Skill` tool runs fine. The gap is purely advertisement, so synthesizing
the listing into `--append-system-prompt` is the only mechanism available.

`skills.resolve_skills(cwd, plugin_dirs)` therefore builds that listing itself,
reading `name`/`description` frontmatter from every `*/SKILL.md` it finds and
tagging each with a **tier**. A skill declares its tier by where it lives:

| Tier | Source | Scope |
| --- | --- | --- |
| `YUKI` | injected plugin dirs (`--plugin-dir`) | every session, every cwd |
| `PROJECT` | `<cwd>/.claude/skills` | only when that project is the cwd |
| `ALWAYS` | `~/.claude/skills`, named in `workspace/skills.yaml` | every session, emphasized |
| `USER` | rest of `~/.claude/skills` | every session, plain listing |

Names are deduplicated across tiers in that priority order, and each tier renders
under its own heading in the prompt. **No skill name is hardcoded anywhere in
`skills.py`** — routing emphasis belongs in each skill's own `description`.

Plugin dirs (tier `YUKI`) come from:

- the bundled `plugins/yuki-conductor` plugin — `yuki-conductor-cron` only, since
  everything here is advertised globally;
- any dirs contributed by installed packages through the
  `yuki_conductor.skill_plugins` entry-point group (each entry point is a
  zero-arg callable returning a plugin dir path). This lets an installed chat
  plugin ship its own skill without this public repo naming it.

Because `--plugin-dir` is session-scoped, these are invisible when the user runs
Claude Code directly — nothing is written to `~/.claude/skills`.

The repo's own `.claude/skills/` holds `yuki-conductor-restart`, which only makes
sense while working *in* this repo, so it is project-scoped rather than bundled.

`ClaudeCodeHarness` passes `--setting-sources user,project,local`; without it a
headless run loads no settings at all and the last three tiers aren't even
resolvable.

All of this is **Claude Code only**. pi lists skills natively, so it gets
`skills.core_prompt()` instead: the attachment contract, the conversation id and
the workspace prompt, without the synthesized listing. Codex lists its own
skills (`~/.codex/skills`) but has no `--plugin-dir` and ignores
`.claude/skills`, so `skills.codex_prompt()` is `core_prompt()` plus the
`YUKI` and `PROJECT` tiers, each named with its `SKILL.md` path for the model
to read. It reaches Codex as `-c developer_instructions=…`, since `codex exec`
has no system-prompt flag.

Finally, if `~/.yuki-conductor/workspace/system-prompt.md` exists, its contents
are appended verbatim. That file is outside the repo, so it's where private
*usage guidance* belongs — internal CLI invocation details, gotchas, examples.
Registration of private user-scope skills belongs in `workspace/skills.yaml`
instead. **Never name private capabilities in the repo-side prompt.** Absent or
empty file is a no-op.

The spawned run also gets `YUKI_CONDUCTOR_PROJECT` in its env so injected skills
can locate and invoke the yuki-conductor CLI.

## Cron Scheduler

The daemon supports scheduled tasks via `~/.yuki-conductor/workspace/cron.yaml`. Each task specifies a cron expression, a description, a Claude prompt, and optionally `chat_app` (`slack` or an installed chat plugin's name) to control where the notification goes. When the cron fires, the routed platform opens a new thread and runs Claude Code with the prompt, posting the result. The thread is session-tracked, so follow-up replies in that thread continue the conversation.

A task may also set `paused: true`, which keeps the definition and its run history
but excludes it from the scheduler entirely (`_build_task_state` filters it out, so
it holds no croniter state at all). Manual "Run now" still fires a paused task —
pausing suspends the *schedule*, not the task. The Automations tab toggles it via
`PATCH /api/automations/{name}`; resuming deletes the key rather than writing
`paused: false`.

Routing rule when `chat_app:` is omitted: pick the first enabled platform, with `slack` preferred. If `chat_app:` names a platform that isn't enabled, the task is skipped with a warning rather than misrouted.

Required env var (only when the Slack channel is enabled): `SLACK_CRON_CHANNEL` — the Slack channel ID to post cron results to.

**Slack slash commands are `/<prefix>-…`**, where the prefix is
`SLACK_COMMAND_PREFIX` (default `yuki`, validated at startup). Each machine
runs its own Slack app; command names are workspace-wide, so a second machine
in the same workspace sets its own prefix (e.g. `skirk`). `slack_app.create_app`
registers `/<prefix>-harness`, `-model`, `-title`, `-usage` and the
`/<prefix>-<skill>` catch-all, and never claims another prefix's commands.
Channel settings (model, harness) are keyed by channel, not command name.

### Automations tab

The web UI surfaces cron tasks as **Automations** (`web/src/AutomationsView.tsx`,
`/api/automations`). The sidebar lists tasks by `display_name` → `description` →
truncated prompt; the detail pane shows a link back to `origin_conversation`, a
foldable prompt with the cron string plus a plain-English cadence, and the run
history with a "Run now" button.

`cron_config.py` owns everything about the file that isn't scheduling:

- `load_tasks()` — the single parser. `cron_scheduler` uses it too; there is no
  second copy of the YAML schema.
- `describe_schedule()` — cron expression → prose. Pure string work, no deps.
- `set_task_field()` — **line-based**, not a PyYAML round trip. Rewriting the
  document would reflow every `prompt: >` block and drop comments, so renames
  find the task's `name:` line and patch one key inside its block. Only fields
  in `EDITABLE_FIELDS` may be written; everything else is read-only from the UI.

Run history lives in the `cron_runs` table of the same workspace DB, via
`CronRunStore`. A row is inserted as `running` when a task fires and updated on
completion, so an in-flight run is visible; the last 30 runs per task are kept
and older rows are pruned on insert. History is keyed by `name`, so renaming a
task's `name` (as opposed to its `display_name`) orphans its history.

Deleting a task from `cron.yaml` leaves that history behind, so
`/api/automations` unions the YAML tasks with `CronRunStore.task_names()` and
emits a synthetic `active: false` entry for every name that only has history.
Those sort after every live task regardless of recency, render dimmed and
labelled "Removed from cron.yaml", and their detail pane drops Rename / Pause /
Run now. `GET /api/automations/{name}` serves them too, and only 404s once a
name has neither a definition nor a run.

Manual runs go through `cron_scheduler.trigger_task()`, which reuses the exact
scheduled path — same prompt prefix, same notification routing — and tags the
row `trigger='manual'`. It notifies via the platforms `start_cron_scheduler()`
registered, so it only works in the daemon process.

Tests that exercise a cron run must not touch the real workspace DB; the autouse
`isolated_cron_run_store` fixture in `tests/conftest.py` rebinds the store.


## Taskboard

A queue of agent tasks the daemon works through on its own: the Tasks tab
(`web/src/tasks/`), `/api/tasks`, and `src/yuki_conductor/taskboard/`. The
PRD and implementation plan are `docs/plans/2026-10-18-taskboard*.md`.

- **States**: To Do → In Progress → Needs Input / Ready for Review → Done
  (`task_store.STATUSES`). `taskboard/transitions.py` is the only code that
  changes a status; it enforces the allowed moves, writes compare-and-set
  (a move that loses a race raises `InvalidTransition`), leaves a system
  comment, and calls listeners. Side effects subscribe as listeners in
  `start_taskboard()`: web broadcast, harvester wake-up, notifications,
  worktree cleanup. Tests clear the listener list (`isolated_task_store`).
- **Harvester** (`harvester.py`): picks To Do tasks in `sort_order`, per
  harness up to `TASKBOARD_CONCURRENCY` (`.env`, e.g. `claude-code:10,pi:1`;
  defaults 10 / 1 / 1 for anything else), unless paused (`taskboard_state`
  table, survives restarts; pausing never stops a running turn). On startup
  it moves every In Progress task back to the top of To Do; its session is
  kept, so the next pickup resumes it.
- **A turn** (`runner.py`): a worktree task runs in
  `<repo>/.claude/worktrees/task-<id>` on `task/<id>-<slug>` from the
  remote's default branch (`worktree.py`, one git writer per repo); others
  run in the project dir. One task is one session: a turn resumes it with
  the undelivered comments, or — if `Harness.session_exists` says it's gone —
  starts fresh with the whole timeline (`prompts.py`). The reply becomes an
  agent comment. Where the task goes next, first match wins: new user
  comment during the turn → To Do; cancelled → Needs Input; failed → Needs
  Input; agent ran `task ask` → Needs Input; agent ran `task done` → Done; else
  Ready for Review.
- **Comments** requeue: a user comment on Needs Input / Ready for Review sends
  the task to the end of To Do; on In Progress it waits for the turn's end;
  Done never reopens on a comment; only Reopen does. Comments are stored before the status is read, and the
  runner re-checks after its own move, so one racing the end of a turn is
  never stranded. `delivered` marks what the agent has been sent.
- **Reopen** (`POST /api/tasks/{id}/reopen`, `actions.reopen`, the Reopen
  button) is the only way out of Done: it runs `cleanup.run` first (so the
  old worktree is gone), clears `worktree_path`, and sends the task to the
  end of To Do with its session kept. The next turn gets a new worktree and
  resumes the session with a "you were reopened" preface
  (`prompts.was_reopened`), or starts fresh with the full timeline if the
  session is gone. A merged/closed PR is dropped (noted on the timeline) along
  with the branch, so the PR watcher can't re-close the task and the worktree
  starts from the default branch; a still-open PR keeps its branch, which
  `ensure_worktree` checks out from `origin/<branch>`.
- **Agents** get a "Taskboard" system-prompt section and `YUKI_TASK_ID`
  (`run_agent(task_id=...)`), and the bundled `yuki-conductor-task` skill
  documents `yuki-conductor task show|list|comment|ask|done|pr|create`
  (`cli_task.py`, over HTTP like `send`).
- **PRs** (`pr_watcher.py`): nothing polls on a timer. Each time the Tasks
  tab loads (page load, refresh, switching to the tab) it calls
  `POST /api/taskboard/pr-refresh`, which starts one background check
  (`pr_watcher.refresh`; skipped while one is already running). The ↻ beside
  a task's PR calls `POST /api/tasks/{id}/pr-refresh` (`actions.refresh_pr`),
  which checks just that PR and waits: the full check in Ready for Review,
  only `pr_state` otherwise. Each Ready for
  Review task's PR is read over the GitHub REST API via `gh api` (not
  `gh pr view`: its GraphQL authors can't tell bots apart). Merged or closed →
  Done; new comments → imported as `github` comments (once each, by URL) and
  the task goes to To Do; the page picks the changes up as task broadcasts.
  Comments carrying `skills.GITHUB_AGENT_MARKER` — the footer agents must
  append to everything they post — are ignored, since the agent may post as
  the user's own account. Each check also records the PR's `pr_state` (OPEN /
  DRAFT / MERGED / CLOSED; a newly registered PR starts OPEN, and Done cleanup
  refreshes it), which the Tasks tab draws as GitHub's octicon, coloured by
  state, in the list and the detail header.
- **Done** cleanup (`cleanup.py`, background): removes the worktree, the
  local `task/` branch and the remote branch. It keeps a local branch whose
  commits no remote has, and the remote branch of a PR that's still open
  (deleting it would close the PR); what it kept is noted on the timeline,
  failures go to `cleanup_error` with a retry button.
- **Notifications** (`notify.py`): only moves flagged `notify` (into Needs
  Input or Ready for Review, including failed runs) and comments posted with
  `notify` (an agent's `yuki-conductor send`) — a web push targeting
  `#tasks/<id>`. Never Slack: Slack only carries replies to Slack-started
  conversations, cron results and the daemon restart message.
- **Live steps** go through the same `ConnectionManager` as chat, keyed
  `task:<id>`; Chat ignores those keys. `live.py` is the only link from the
  board to the web layer.
- Done tasks older than 30 days are hidden from the list unless asked for
  (`?old=true`, `task list --old`, the "Show done over 30 days ago" toggle).
- **Archive** (`POST /api/tasks/{id}/archive`, `actions.archive`) sets
  `tasks.archived_at`, like an archived conversation: the row stays, but the
  task drops out of every list, `list_status` (so neither the harvester nor
  the PR watcher sees it), its parent's follow-ups, and `actions.get` 404s
  it. In Progress must be cancelled first; To Do is cancelled on the way.
  The worktree and branches are cleaned up (`cleanup.run`) *before* the
  archive, and a failed cleanup fails the archive with `cleanup_error` set
  ("Retry archive" in the UI). The final write is compare-and-set on the
  status, so a task requeued during cleanup isn't archived. No unarchive.
- **Pasted files**: pasting into the chat composer, the task form or a task
  comment uploads the clipboard's files (`web/src/paste.ts`). In tasks, a
  pasted image also gets an inline `![name](/api/files/<id>)` marker so
  Preview and the timeline show it; `prompts.py` rewrites those URLs to the
  file's path on disk before the agent sees the text.

## Key Commands

- `uv run pytest` — run all tests
- `uv run ruff check .` — run linter
- `uv run ruff check --fix .` — run linter with auto-fix
- `uv run yuki-conductor --help` — show CLI help
- `uv run yuki-conductor simulate message "test"` — test without Slack
- `uv run yuki-conductor send -c <conv-id> "text"` — push a message (and `--attach` files) into a web or Slack conversation (see Outbound Messages)
- `uv run yuki-conductor task show|list|comment|ask|done|pr|create` — the taskboard from a shell (see Taskboard)
- `uv run yuki-conductor search [--conversations|--tasks] TERMS` / `conversation show <id>` — find and read past work (see Conversation search)
- `uv run yuki-conductor web rebuild` — rebuild frontend + auto-reload connected browsers
- `cd web && pnpm dev` — start frontend dev server (proxies /api to port 2333)
- `cd web && pnpm build` — build frontend for production (output: web/dist/)

## Daemon Management

The `daemon` subcommand (`install`/`uninstall`/`restart`/`status`/`log`) is
dispatched by platform in `daemon.py`:

- macOS (`daemon_macos.py`) — a per-user LaunchAgent (`launchctl` + plist).
  Shared helpers (uv discovery, web build, log tailing) live in
  `daemon_common.py`.
- Linux (`daemon_linux.py`) — a systemd user service,
  `~/.config/systemd/user/yuki-conductor.service`, driven by `systemctl --user`.
  It has `Restart=always`; run `loginctl enable-linger` for it to start at boot
  rather than at login. `spawn_detached_restart()` can't use the macOS `setsid`
  trick — `KillMode=control-group` kills every process in the unit's cgroup,
  detached or not — so it queues `systemctl --user restart --no-block` on a
  timer thread instead; the job then belongs to the systemd manager and
  survives the kill.
- Windows (`daemon_windows.py`) — **unsupported**; every action prints a message
  and exits 1. yuki-watcher (`C:\Git\yuki-watcher`, a WinForms tray app) owns
  the daemon there: it runs `uv run --project <repo> yuki-conductor run` and
  relaunches it shortly after it exits. Don't reintroduce a Task Scheduler task
  or an in-repo supervisor script — two supervisors race and produce duplicate
  daemons fighting over port 2333.

Because yuki-watcher restarts the process on exit, **restarting on Windows means
exiting**: `spawn_detached_restart()` just schedules `os._exit(0)` a few seconds
out so the HTTP response gets away first. That's what the web UI's Restart
daemon button and the project-scoped `yuki-conductor-restart` skill both use.

## Outbound Messages

Every message to the user, whatever produced it, ends up as one
`OutgoingMessage(text, attachments)` handed to the owning platform's `send()`.
There are three producers:

| Producer | How the model speaks | Parsed by |
| --- | --- | --- |
| Reply to a turn | final response text | `handle_incoming_message` → `split_response` |
| Cron result | final response text + `<notify>`/`<silence>` | `cron_scheduler._execute` → `split_response` |
| Push (mid-turn) | `yuki-conductor send [-c id] [--attach PATH] "text"` | `/api/push` |

Files in a final response are `<attachment>/abs/path</attachment>` tags (bare
names resolve under `workspace/attachments/`). `split_response` strips them and
returns the paths as attachments. The same tag carries user uploads *into* the
prompt (`_build_prompt`). `skills._ATTACHMENTS` documents the tag for the
model, because nothing else tells it the tag exists; both `system_prompt()` and
`core_prompt()` include it, so it works on every harness.

`/api/push` looks up the platform that owns the conversation: a
`ConversationStore` row means web, and otherwise the `SessionStore`
`session_type` names a receiver's platform (Slack). Omitting `-c` opens a new
web conversation — unless the run is a task turn (`YUKI_TASK_ID`), where the
CLI posts an agent comment on that task instead and never calls `/api/push`.
That comment carries `notify: true`, so `actions.add_comment` pings the user
through `notify.on_comment` (web push), as a push would; a plain
`task comment` stays silent. The message is sent with `pushed=True`; `WebPlatform` uses
that to tag the WebSocket payload (drives the notification sound) and mark the
conversation unread. `web_server.PUSH_MARKER` is appended to the text itself so
a push still reads differently from a reply after a reload. Attachment paths
must be absolute, since the daemon reads them from the same disk as the run.

For the run to address its thread it must know the conversation id, so
`skills.system_prompt(conversation_key=...)` (and `core_prompt`) renders a "Your
conversation" section and `run_agent(push_conversation_id=...)` threads it through. Turns
pass their own key when the platform is in `messaging.PUSHABLE_PLATFORMS`.

Cron tasks get the same treatment via `WebPlatform.reserve_thread()`, which
allocates the conversation *before* the run so a long task can post progress
into the same thread its final result lands in. `discard_thread()` cleans up the
empty reservation when the run ends in `<silence>` and posted nothing. Slack has
no reservation, so a cron run on Slack can't push until its thread exists.

The `yuki-conductor-send` skill (bundled plugin) documents `send` for the model.

### Browser notifications

Every assistant message `WebPlatform` delivers — a reply, a cron result
(`start_thread` or `send`), a push — and every taskboard task that needs the
user also goes out as a Web Push notification
(`web_push.py`) to each browser subscribed via the 🔔 toggle in the chat
sidebar. Each push names a target, `{kind: "conversation" | "task", id}`
(`web_push.conversation_target` / `task_target`). A notification names the
conversation or task and what happened ("Responded to a conversation",
"Moved to Needs Input", "Agent posted a comment"), never the content. The service worker
`web/public/sw.js` shows it, unless a focused window is already on that
target; a click focuses the app and opens `#chat/<id>` or `#tasks/<id>`. Subscriptions live in the `push_subscriptions` table and are
dropped when the push service answers 404/410. The VAPID key is generated on
first use at `~/.yuki-conductor/.secrets/vapid-private.pem`; deleting it
invalidates every subscription. Push needs a secure context, so it only works
through HTTPS (`tailscale serve`) or localhost. Slack threads aren't covered —
Slack notifies on its own. Tests are isolated by the autouse
`isolated_push_subscriptions` fixture.

## Frontend Deployment Gotchas

The production daemon serves the **built** frontend from `web/dist/`, which is
**never in git**: each machine builds its own, so it always matches that
checkout. Don't commit a build (not even `index.html`) — its hashed asset names
change on every build, so it would conflict across PRs and go stale on merge.

What builds it:
- **Daemon startup** — `runtime.start()` builds when `config.web_frontend_stale()`
  finds `web/dist/index.html` missing or older than any frontend source (fresh
  clone, a pull, Windows' yuki-watcher, the Restart daemon button).
- `uv run yuki-conductor web rebuild` — rebuilds and, if the daemon is running,
  reloads every connected browser. Use it after frontend-only changes; no
  restart needed.
- `yuki-conductor daemon restart` (macOS/Linux) and self-update (when the
  update touched `web/`).

**After backend changes** (Python code), you must restart the daemon — the running process keeps old code in memory.

A build in a git worktree only lands in that worktree's `web/dist/`; the daemon
serves the main checkout's.

## Pre-commit / Pre-PR Checks

Before making any commit or creating a PR, you MUST run both:

1. `uv run pytest` — all tests must pass
2. `uv run ruff check .` — linter must report no errors

Do not commit or open a PR if either fails.

**UI changes:** whenever a change touches what the web UI looks like, finish by
screenshotting the affected view (the `playwright-cli` skill against the daemon
at `http://127.0.0.1:2333`, desktop and a ~390px-wide mobile viewport) and send
the screenshots to the user with your reply. Remember the daemon serves the main
checkout's `web/dist/`, so build there (or point the browser at a `pnpm dev`
server in the worktree) before capturing.

## Code Style

Keep the code clean. **No backward compatibility is required.** This project has a single user and no external consumers, so:

- Don't support old formats of `.env`, `cron.yaml`, the SQLite schema, CLI flags, or any other config. When a format changes, update the file in place and delete the old code path.
- Don't add migration shims, deprecated-field fallbacks, or "if old format then…" branches. Just change the format and the code together.
- Don't keep removed functions/flags as aliases or re-exports. Delete them.
- Don't write `# kept for backwards compatibility` comments — there is no such requirement.
- When renaming or restructuring, update all call sites and config files in the same change. Don't leave a transition period.

## Architecture

- **Session tracking**: SQLite database at `~/.yuki-conductor/workspace/yuki-conductor.db` maps `thread_ts → (session_id, channel_id, harness)` and `<channel>:<harness> → model`
- **Web server**: FastAPI on 127.0.0.1:2333 (env: `WEB_HOST`, `WEB_PORT`), serves React frontend and `/api/sessions` + chat endpoints. Starts in a daemon thread alongside any enabled chat-app receivers.
- **Concurrency**: slack-bolt's default thread pool (10 threads); each handler blocks on `subprocess.run`
- **Agent invocation**: `harness.process.run_agent(harness, prompt, ...)` spawns the harness's CLI and reads its stdout line by line; the harness's parser turns each record into a `StreamEvent` (tool calls, text, thinking, tool results) delivered live via `on_event`, and its final state into the returned `RunResult`. See Harnesses.
- **Progress surfaces**: platforms opt into live steps by implementing `on_stream_event`. Web buffers steps in `ConnectionManager` (memory only — never persisted) and broadcasts `{"type": "step"}`; `GET /api/chat/steps` replays the buffer so a reconnecting browser resumes mid-run. Slack folds steps into the shimmering assistant status (coalesced to one `setStatus` call per couple of seconds) and posts nothing until the final reply. Platforms without the hook just get the final response.
- **Environment**: Must unset `CLAUDECODE` env var in subprocess to avoid nested session errors

## Harnesses

A turn runs on one **harness**, an agent CLI. `harness/__init__.py` knows these
(`KNOWN_HARNESSES`):

| Name | CLI | Notes |
| --- | --- | --- |
| `claude-code` | `claude -p --output-format stream-json --verbose …` | Windows shims; skill listing synthesized (see Skill Discovery) |
| `pi` | `pi --mode json --no-approve --model <provider/id> …` | `PI_BIN`, `PI_DEFAULT_MODEL` (`strata/qwen3.8-flash-next-iq2_xs`); providers live in `~/.pi/agent`, not here |
| `codex` | `codex exec [resume] --json --dangerously-bypass-approvals-and-sandbox …` | `CODEX_BIN`; provider and model live in `~/.codex/config.toml`, `CODEX_DEFAULT_MODEL` overrides the model. On Windows the npm `.cmd` shim is skipped for the vendored `codex.exe`, since cmd.exe mangles a multi-line argument |

**Which exist on a machine is the `HARNESSES` setting** in
`~/.yuki-conductor/.env`, e.g. `HARNESSES=claude-code,pi`. It's a
comma-separated list in preference order; the first entry is the default for a
new conversation. It defaults to `claude-code` alone, since pi and Codex need their own
install and config. Only enabled harnesses are registered (`HARNESSES` in
`harness/__init__.py`), so a disabled one is never offered, scheduled or run:

- the web picker lists only enabled harnesses whose CLI is on PATH;
- a cron task naming a disabled harness is recorded as an error run and not run,
  like a missing `chat_app`;
- an existing conversation bound to a disabled harness gets a "not enabled on
  this machine" reply rather than switching harness, since another harness
  couldn't resume its session anyway;
- a *new* conversation asking for a disabled harness falls back to the default.

- **A conversation keeps its harness for life**, since a session id only works
  with the harness that made it. Web stores it on the conversation row when the
  conversation is created; Slack stores it on the `sessions` row at the first
  write (`SessionStore.set` never changes it); cron's `harness:` key is recorded
  on the reserved thread up front.
- **How a turn's harness is chosen:** `conversation.resolve_harness` takes the
  platform's record first, then `IncomingMessage.harness`, then
  `default_harness()`.
- **How a new one is chosen:**
  - web: the toggle in the new-chat picker (`GET /api/harnesses`);
  - Slack: the channel's `/<prefix>-harness` pin (`HarnessStore`, per channel),
    else the default. A new thread's `sessions` row is pre-created with that
    harness; since the first write fixes it, writing the default there would
    lock every Slack thread to it;
  - cron: a task's `harness:`;
  - otherwise the default (first in `HARNESSES`).
- **Context occupation is measured on the turn's last model call.**
  `RunResult.context_tokens` is that call's prompt-side tokens — Claude Code:
  `input + cache_creation + cache_read` of the last *top-level* assistant record
  (a subagent's own small context is ignored); pi: `input + cacheRead +
  cacheWrite` of the last `message_end`; Codex: `input_tokens` (cache
  included) of the last call. The result record's own `usage` sums
  every call of the turn, so it is never the window size. `context_window` is
  the window that call ran against: Claude Code reports it per model in
  `modelUsage`, pi never reports it at all, so pi's is read from pi's own
  catalogs — `<agent-dir>/models.json` (or `PI_CODING_AGENT_DIR`) over the
  cached `models-store.json`. Codex's stream has only the turn total, so both
  come from the last `token_count` event in the session's rollout file
  (`$CODEX_HOME/sessions/…/rollout-…-<id>.jsonl`), read after the run. Both are stored per assistant message in
  `message_usage` and reach the chat through the message list: a meter in the
  thread header plus a label on each reply.
- **Slack's `/<prefix>-model` pins a model for the channel's current harness**
  (its `/<prefix>-harness` pin, else the default), from that harness's
  `model_aliases()` — `/yuki-model sonnet` on a Claude channel,
  `/yuki-model astra` on a Codex one. `ModelStore` keys pins
  `<channel>:<harness>`, so switching harness never carries a model id to a
  CLI that doesn't know it, and each harness keeps its own pin. A thread uses
  the pin for the harness *it* runs on; pi offers no picks.
- **The web picker's model is per conversation, for every harness.** A
  conversation's `model` column (`conversations.model`, `PATCH
  /api/conversations/{id}`) is what `IncomingMessage.model` carries on every
  turn; `None` means the harness's own default. `GET /api/harnesses` adds each
  harness's `default_model()` and `models()` (`ModelOption(id, label)`) for the
  picker to render — empty for a harness with nothing to choose (pi). Each
  harness keeps its own small, hand-maintained `MODEL_ALIASES` table (named
  pick → literal model id passed to the CLI), exposed as `model_aliases()` and
  shared by the web picker and Slack's `/<prefix>-model`:
  - **Claude Code**: `sonnet` / `opus`, bare (no `[1m]` suffix) — the
    Anthropic API re-resolves a bare alias to whatever snapshot is current on
    every single request, which a live check on 2026-10-28 confirmed also
    carries the 1M context window already (`modelUsage.contextWindow` was
    1,000,000 for a fresh run, a resume, and even with no `--model` flag at
    all). `CLAUDE_DEFAULT_MODEL`'s own `[1m]` suffix is untouched — it
    predates this check and still works — but nothing about the suffix is
    required for the picker's aliases.
  - **Codex**: `astra` / `sol`, mapped to today's literal GPT slug
    (`gpt-6-astra` / `gpt-6.1-sol`). Unlike Claude's bare aliases, a GPT slug
    *is* the resolved version already — OpenAI has no live "latest Sol" for
    anything to resolve per request — so there is nothing to ask the CLI or
    the provider for at runtime; the table is bumped by hand when a new
    generation ships. (`codex debug models` can print Codex's own bundled
    catalog, but that's what Codex knows how to drive, not what a provider
    actually serves, so it isn't used here.)
  - **pi** offers no named picks, only its own configured default.
- **Slack shows the harness** two ways: the shimmering status is tagged
  `(<label>) is thinking...`, and every thread's root message (user-started
  or cron's `start_thread`) gets a reaction from `HARNESS_EMOJI` in
  `slack_platform.py` (`:large_orange_diamond:` Claude Code, `:pie:` pi,
  `:large_blue_diamond:` Codex).
  Both are best-effort; a new harness without an emoji just gets no reaction.
- **Slack's `/<prefix>-<skill>` catch-all** forwards Claude's `/<skill>` syntax, so it
  always runs on Claude Code.
- **pi exits 0 even when the model call failed.** `PiStreamParser` reads the
  outcome from the last assistant message's `stopReason`.
- **Codex exits 1 on a failed turn with nothing useful on stderr.** The reason
  is in the stream (`turn.failed`); `StreamParser.failure()` hands it to
  `run_agent`, which prefers it over stderr. Codex also reads an open stdin as
  more prompt, so `run_agent` spawns every harness with stdin closed.
- **pi and the GPU:** pi usually runs on Strata, which shares the GPU with
  ComfyUI. That sharing is handled entirely outside this repo, by
  vhotseat (`~/Projects/vhotseat`). yuki-conductor never checks or switches the
  GPU; when Strata refuses with its "before_load command failed" 503, the parser
  just turns that into a readable "GPU busy" reply.
- Adding a harness means a module in `harness/` with a `launch()` and a
  stateful parser, plus fixtures recorded from real runs. See
  `docs/plans/2026-10-12-multi-harness.md`.

## Testing

- `test_store.py` — unit tests for SQLite store (roundtrip, concurrent access, separate tables)
- `test_harness_claude_code.py` — Claude Code through `run_agent` with a mocked subprocess (flag construction, errors, timeouts, cancel)
- `test_harness_pi.py` — pi command line, plus its parser against real recorded runs in `tests/fixtures/harness/pi/` (system prompt scrubbed)
- `test_harness_codex.py` — Codex command line, parser against real runs in `tests/fixtures/harness/codex/`, rollout-file context, `run_agent` end to end
- `test_cli_simulate.py` — integration tests via simulate commands
- `test_task_store.py`, `test_taskboard_*.py`, `test_tasks_api.py`, `test_cli_task.py` — the taskboard: store, state machine, harvester, runner (fake `run_agent`), worktrees and cleanup against a temp repo with a bare origin, PR watcher with `gh` mocked, API, CLI end to end through the API. The autouse `no_stray_harvester` fixture fails any test that leaves a real board loop running.
