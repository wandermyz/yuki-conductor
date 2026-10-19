# Taskboard — Implementation Plan

> Status: **implemented (2026-10-18)**, phases 1–6 on one branch. One
> deviation: instead of stub modules, `transitions.add_listener()` is how
> later phases hook status changes. The PR watcher uses `gh api` (REST)
> throughout rather than `gh pr view`. Not verified end to end: the GitHub
> PR loop (needs a real repo), and pi's session-file lookup (pi isn't
> installed on the dev machine). Written 2026-10-18. Implements the PRD in
> [2026-10-18-taskboard.md](2026-10-18-taskboard.md). Read that first: this
> document covers *how* to build it, not *what* it does.

## Overview

The work is split into six phases. Each phase is one PR that leaves `main`
working (tests + ruff green, daemon starts):

| Phase | PR | Ships |
| --- | --- | --- |
| 1 | Task store + state machine | tables, `transition()`, config. No runtime effect. |
| 2 | Runner + harvester | tasks actually run. Usable through the API from curl. |
| 3 | HTTP API + `task` CLI + skill | agents and scripts can drive the board |
| 4 | Notifications, PR watcher, cleanup | the PR loop closes itself |
| 5 | Frontend | Tasks tab, list, detail, form, Markdown + pele |
| 6 | Docs + end-to-end check | CLAUDE.md, real run against a scratch repo |

Phases 1–4 are backend only. Phase 5 can start once phase 3's API exists.

## Module Layout

```
src/yuki_conductor/
  task_store.py       NEW  TaskStore, TaskboardStateStore, Task/TaskComment dataclasses
  taskboard/          NEW
    __init__.py            start_taskboard(platforms) — called by runtime
    transitions.py         transition(): the one place status changes
    runner.py              run one turn: worktree, prompt, run_agent, resolve outcome
    harvester.py           pickup loop + concurrency accounting + restart recovery
    prompts.py             first-turn / follow-up / full-context prompt builders
    worktree.py            create / cleanup (git), default-branch lookup
    pr_watcher.py          5-minute gh poll
    notify.py              web push + Slack fan-out for task transitions
  config.py           +    TASKBOARD_CONCURRENCY parsing
  harness/base.py     +    RunRequest.task_id, RunResult.session_missing, Harness.session_exists
  harness/claude_code.py + task env + prompt section + session_exists + session_missing detection
  harness/pi.py       +    same
  skills.py           +    _taskboard_section(task_id) in system_prompt/core_prompt
  web_push.py         ~    notify() takes a target {kind, id} instead of conversation_id
  web_server.py       +    /api/tasks*, /api/taskboard/state
  cli.py              +    `task` subcommand group (via a new cli_task.py)
  runtime.py          +    start_taskboard() after start_cron_scheduler()
plugins/yuki-conductor/skills/yuki-conductor-task/SKILL.md   NEW
web/src/
  Markdown.tsx        NEW  react-markdown + remark-gfm + pele mermaid
  chat/shared.tsx     NEW  HarnessIcon, StepList, attachment chips (moved out of Chat.tsx)
  tasks/              NEW  TasksView, TaskList, TaskDetail, TaskForm, QuoteButton, api.ts
  App.tsx             ~    tabs
web/public/sw.js      ~    notification target
```

The `taskboard/` package keeps the large new surface out of `web_server.py`
and `cron_scheduler.py`. Neither should grow task logic. They only call in.

---

## Phase 1 — Store, state machine, config

### `task_store.py`

The style follows `store.py`: a `threading.Lock`, a connection per call, and
`CREATE TABLE IF NOT EXISTS` in `_init_table`. No migrations, because the
tables are new.

```sql
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  attachments TEXT NOT NULL DEFAULT '[]',     -- JSON list of absolute paths
  project TEXT NOT NULL,
  harness TEXT NOT NULL,
  use_worktree INTEGER NOT NULL,
  status TEXT NOT NULL,                       -- todo|in_progress|needs_input|ready_for_review|done
  sort_order REAL NOT NULL,                   -- REAL so "top" = min-1 and "end" = max+1 never collide
  session_id TEXT,
  worktree_path TEXT,
  branch TEXT,
  pr_url TEXT,
  pr_checked_at REAL,
  asked_input INTEGER NOT NULL DEFAULT 0,
  parent_task_id INTEGER REFERENCES tasks(id),
  cleanup_error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  started_at REAL,                            -- first pickup ever; non-null = no longer editable
  done_at REAL
);
CREATE TABLE IF NOT EXISTS task_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  author TEXT NOT NULL,                       -- user|agent|system|github
  body TEXT NOT NULL,
  quote_comment_id INTEGER,
  attachments TEXT NOT NULL DEFAULT '[]',
  delivered INTEGER NOT NULL DEFAULT 0,
  external_url TEXT,
  external_author TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS task_comments_task ON task_comments(task_id, id);
```

API, all returning dataclasses:

- `create(...) -> Task`: status `todo`, `sort_order = max+1`
- `get(id)`, `list(include_archived: bool)`. Archived means `done AND done_at < now - 30d`, computed in SQL.
- `update_fields(id, **fields)`: a whitelist of columns. Bumps `updated_at`.
- `claim(id) -> bool`: `UPDATE tasks SET status='in_progress', started_at=COALESCE(started_at, now) WHERE id=? AND status='todo'`. The harvester claims by rowcount, so a concurrent cancel can't race a pickup.
- `set_status(id, status, *, sort_order=None)`: low level. Only `transitions.py` calls it.
- `reorder(ids: list[int])`: rewrites `sort_order` for the given To Do ids, in order
- `move_to_end(id)` / `move_to_top(id)`
- `add_comment(...) -> TaskComment`, `comments(task_id)`, `undelivered(task_id)`, `mark_delivered(ids)`
- `children(task_id)`, `running_counts() -> dict[harness, int]`

`TaskboardStateStore(_SqliteKVStore)` on table `taskboard_state`, with key `paused`.

### `taskboard/transitions.py`

`transition(task_id, to, *, reason: str, notify: bool = False) -> Task` is the
**only** code that changes `status`. It:

1. updates the status (and `done_at` when the target is `done`; `sort_order = max+1` when the target is `todo`, unless the caller passes `top=True`)
2. adds a `system` comment `"<from> → <to>: <reason>"`
3. broadcasts `{"type": "task", "id": …}` via `ws_manager.broadcast_all`. This is injected as a callback so phase 1 has no web import.
4. calls `notify.task_transition(task)` when `notify` is set (a no-op stub until phase 4)
5. calls `harvester.wake()` when the target is `todo` (a no-op stub until phase 2)
6. queues `worktree.cleanup(task)` on a background thread when the target is `done` (stub until phase 4)

### Config

In `config.py`, `TASKBOARD_CONCURRENCY` is parsed into `dict[str, int]`:

```
TASKBOARD_CONCURRENCY=claude-code:10,pi:1
```

`taskboard_limit(harness) -> int`: an explicit value, else
`{"claude-code": 10, "pi": 1}`, else 1. Malformed entries raise at import with
the same style of message as `HARNESSES`.

### Tests (phase 1)

- `tests/test_task_store.py`: CRUD, claim race (two threads, one wins), reorder, top/end ordering, archive cutoff, comment delivery flags
- `tests/test_taskboard_transitions.py`: each transition writes a system comment and broadcasts; `done` sets `done_at`
- `tests/test_taskboard_config.py`: parsing, defaults, errors
- `conftest.py`: autouse `isolated_task_store`, the same pattern as `isolated_cron_run_store`

---

## Phase 2 — Runner and harvester

### Harness changes (`harness/base.py`, `claude_code.py`, `pi.py`)

- `RunRequest.task_id: int | None`. When it's set, `launch()`:
  - adds `YUKI_TASK_ID=<id>` to `Launch.env`
  - passes `task_id` to `system_prompt()` / `core_prompt()`
- `run_agent(..., task_id=None)` forwards it to `RunRequest`.
- `Harness.session_exists(session_id: str, cwd: str) -> bool`, checked **before** resuming:
  - Claude Code: `~/.claude/projects/<cwd with / and . → ->/<session_id>.jsonl` exists
  - pi: its session file exists under the agent dir's `sessions/` for that cwd. The exact path layout must be confirmed against a real run. This check matters more for pi than for Claude, because `pi --session-id` **silently creates** a missing session, so a resume would otherwise lose all context without any error.
- `RunResult.session_missing: bool = False`, set by the Claude parser when the result text is `No conversation found with session ID…`. This is the backstop for a race where the file vanishes between the check and the run.

Tests: extend `test_harness_claude_code.py` and `test_harness_pi.py` for the env
var, the prompt section, `session_exists`, and `session_missing`.

### `skills.py`

`_taskboard_section(task_id)` is appended by both `system_prompt` and
`core_prompt` when `task_id` is set. Its content:

- You are working on taskboard task #N, unattended. Nobody is watching this turn.
- Read the task with `yuki-conductor task show`. Use the `yuki-conductor-task` skill.
- Blocked or need a decision: `yuki-conductor task ask "…"`, then **end your turn**. Don't wait.
- Opened a PR: `yuki-conductor task pr <url>`.
- Your final response is posted to the task as a comment.
- Everything you post to GitHub (PR body, comments, review replies) must end with:
  `_🤖 Posted by yuki-conductor agent (task #N)_` and then `<!-- yuki-conductor:agent -->`.

Task runs pass **no** `conversation_key`, so the "Your conversation" section is
omitted. Test this in `test_skills.py`.

### `taskboard/prompts.py`

All three builders are pure functions over `Task` + `list[TaskComment]`, and
easy to unit test.

- `first_turn(task, comments)`: title, body, project/branch info, then every comment rendered as below
- `follow_up(comments)`: "New comments on your task since your last turn:" plus the undelivered comments
- `full_context(task, comments)`: `first_turn` plus a preface: "Your previous session for this task is gone; here is the full history."

How a comment is rendered:

```
--- comment #42 · user · 2026-10-18 14:03 ---
> quoted excerpt (already in the body as Markdown)
body…
<attachment>/abs/path</attachment>
```

Comments by `github` include the author and URL. `system` comments are
included in the full context but skipped in `follow_up`.

### `taskboard/worktree.py` (create half)

`ensure_worktree(task, project_path) -> (path, branch)`:

1. When `worktree_path` is set and exists, return it.
2. `git -C <repo> fetch origin`
3. Default branch from `git symbolic-ref --short refs/remotes/origin/HEAD` (strip `origin/`), falling back to `main`
4. Branch `task/<id>-<slug>`, where `slug` is the title lowercased, non-alphanumerics replaced by `-`, at most 40 chars
5. `git -C <repo> worktree add <repo>/.claude/worktrees/task-<id> -b <branch> origin/<default>`
6. When `git check-ignore -q .claude/worktrees` fails, append `.claude/worktrees/` to `<repo>/.git/info/exclude`
7. Store `worktree_path`, `branch`

On any `CalledProcessError` it raises `WorktreeError(stderr)`. The runner turns
that into Needs Input.

### `taskboard/runner.py`

`run_turn(task_id)` runs on a worker thread:

```
task = store.get(id)
path = ProjectStore().get_path(task.project)    # None → needs_input "project not registered"
cwd = ensure_worktree(...) if task.use_worktree else path
store.update(asked_input=0)
turn_started = now
harness = get_harness(task.harness)
comments = store.comments(id)
if task.session_id and harness.session_exists(task.session_id, cwd):
    prompt, session = follow_up(undelivered), task.session_id
elif task.session_id:                            # session gone
    prompt, session = full_context(task, comments), None
else:
    prompt, session = first_turn(task, comments), None
store.mark_delivered(ids of comments in prompt)
ws_manager.set_processing(f"task:{id}", …, True)
result = run_agent(harness, prompt, session_id=session, cwd=cwd,
                   conversation_key=f"task:{id}", task_id=id,
                   on_event=step sink → ws_manager.add_step(f"task:{id}", …))
if result.session_missing: retry once with full_context, session None
ws_manager.set_processing(..., False)
store.update(session_id=result.session_id)
text, attachments = split_response(result.text)
if text or attachments: add_comment(author="agent", …)
if not pr_url: scan text for github PR URL → store
resolve_outcome(...)
```

`resolve_outcome` applies the PRD's precedence order exactly, and is unit
tested on its own:

1. Any `user` comment with `created_at >= turn_started` and `delivered=0` → `todo` (reason "new comment during turn")
2. `was_cancelled(f"task:{id}")` → `needs_input` (reason "cancelled", **no** notify)
3. `result.is_error` → `needs_input` + system comment with the error, notify
4. `asked_input` → `needs_input`, notify
5. else → `ready_for_review`, notify

The whole body is wrapped in `try/except`. Any unexpected exception becomes
Needs Input with the traceback summary, so a task can never be stranded In
Progress while the daemon is still up.

### `taskboard/harvester.py`

```python
class Harvester:
    _wake = threading.Event()
    _running: dict[int, str]   # task_id -> harness, guarded by a lock

    def loop(self):
        while not stop:
            self._wake.wait(timeout=2); self._wake.clear()
            if state.paused: continue
            for task in store.list_status("todo"):           # sort_order asc
                if task.harness not in HARNESSES: continue   # flagged in API response
                if count(task.harness) >= taskboard_limit(task.harness): continue
                if store.claim(task.id):
                    transition-side effects for in_progress (system comment, broadcast)
                    self._running[task.id] = task.harness
                    Thread(target=self._run, args=(task.id,), name=f"task-{task.id}").start()
```

`_run` calls `runner.run_turn` and removes the task from `_running` in
`finally`, then calls `wake()`.

**Restart recovery** runs in `start_taskboard()` before the loop starts. For
every `in_progress` task: `transition(todo, top=True, reason="re-queued after
daemon restart")`. Its `session_id` is kept, so the next pickup resumes it.

`start_taskboard(platforms)` is called from `runtime.start()` right after
`start_cron_scheduler`. It stores the platforms for `notify.py`, runs
recovery, and starts the harvester and (phase 4) PR watcher threads.

### Tests (phase 2)

- `test_taskboard_prompts.py`: rendering, quotes, attachments, github author lines
- `test_taskboard_runner.py`: a fake harness (a `run_agent` monkeypatch) covers each outcome branch, session-gone → full context, `session_missing` retry, worktree failure → needs_input, PR URL scan
- `test_taskboard_harvester.py`: per-harness caps (e.g. limit 2, 5 tasks → 2 running), a busy harness doesn't block another, pause, disabled harness skipped, restart recovery puts the task on top
- `test_taskboard_worktree.py` (create half): a temp repo with a bare `origin`, branch naming, exclude written only when needed

---

## Phase 3 — HTTP API, CLI, skill

### Endpoints (`web_server.py`, thin wrappers over `taskboard`)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/tasks?archived=0\|1` | rows + `comment_count`, `last_activity_at`, `harness_enabled` |
| POST | `/api/tasks` | 400 when the project is unknown or the harness isn't enabled; `parent_task_id` optional |
| GET | `/api/tasks/{id}` | task + comments + `children` + `editable` |
| PATCH | `/api/tasks/{id}` | 409 unless `status=='todo' and started_at is None` |
| POST | `/api/tasks/{id}/comments` | body: `{body, quote_comment_id?, attachments[], author?}`. `author` is `agent` only when the CLI sends it. Applies the requeue rules (below). |
| POST | `/api/tasks/{id}/cancel` | In Progress: `cancel_process("task:<id>")` (runner resolves needs_input). To Do: `transition(needs_input)`. Otherwise 409. |
| POST | `/api/tasks/{id}/ask` | CLI only: adds the agent comment + sets `asked_input=1` |
| POST | `/api/tasks/{id}/pr` | CLI only: `{url}`, validated as a GitHub PR URL |
| POST | `/api/tasks/{id}/done` | from needs_input / ready_for_review |
| POST | `/api/tasks/{id}/cleanup` | retry cleanup (phase 4) |
| PUT | `/api/tasks/order` | `{ids: [...]}`, only To Do ids accepted |
| GET/PUT | `/api/taskboard/state` | `{paused, running: {h: n}, limits: {h: n}}`. PUT `{paused}` wakes the harvester on resume. |
| GET | `/api/tasks/{id}/steps` | `ws_manager.get_steps()["task:<id>"]` |

**Requeue rules** for user (and github) comments, in `taskboard.add_comment()`:

| Status | Effect |
| --- | --- |
| needs_input, ready_for_review | `transition(todo)` (end of queue) |
| in_progress | stored undelivered. The runner's precedence rule 1 handles it. |
| todo | stored. It's delivered on pickup. |
| done | stored only |

Agent comments never requeue.

Task attachments reuse `/api/uploads`. The frontend uploads first, then sends
the returned absolute paths, the same as chat.

### `cli_task.py` + `cli.py`

A `task` subparser group. A shared `_api(method, path, payload)` helper is
built on the same `urllib` code as `send`, factored out of `send` into
`cli_http.py` so both commands use it. `ID` resolution: positional → `$YUKI_TASK_ID` → error.

```
yuki-conductor task show [ID]
yuki-conductor task list [--status S] [--project P] [--archived]
yuki-conductor task comment [ID] [--quote CID] [--attach PATH]... TEXT|-
yuki-conductor task ask [ID] TEXT|-
yuki-conductor task pr [ID] URL
yuki-conductor task create --project P [--harness H] [--worktree] --title T BODY|-
```

`comment` sends `author=agent` when `$YUKI_TASK_ID` is set, else `user`.
`create` sets `parent_task_id=$YUKI_TASK_ID` when set. `show` prints Markdown:
a header table, the body, then the timeline.

### Skill

`plugins/yuki-conductor/skills/yuki-conductor-task/SKILL.md`. Its frontmatter
description is written so it triggers both inside task runs ("you are working
on a taskboard task", "ask the user", "register PR") and in chat ("create a
task", "queue this for later", "put it on the taskboard"). The body has one
example per command, the ask-then-end-turn rule, and the GitHub attribution
footer verbatim.

### Tests (phase 3)

- `test_tasks_api.py`: every endpoint, the edit 409, requeue rules for each status, cancel from each status, archived filter, order endpoint rejects non-todo ids
- `test_cli_task.py`: argument parsing, ID resolution from env, author selection, parent link (mock the HTTP helper, as `test_cli_send.py` does)

---

## Phase 4 — Notifications, PR watcher, cleanup

### `web_push.py` + `sw.js`

`notify(target: dict, title, text)`, where `target` is
`{"kind": "conversation"|"task", "id": …}`. All existing callers pass
`{"kind": "conversation", "id": conv_id}`. `sw.js` builds the URL from the
target (`#chat/<id>` or `#tasks/<id>`), uses it both for the "already focused
on it" suppression and for click-to-open, and posts
`{type: "open-target", target}` to an existing window. The app handles that
message by setting the hash. Update `test_web_push.py`.

### `taskboard/notify.py`

`task_transition(task, reason)`:

- web push: title `[Task #N · Needs Input]` / `[Task #N · Ready for Review]`, body = the excerpt of the latest agent comment (+ PR URL for review)
- Slack: when `"slack"` is in the platforms passed to `start_taskboard`, call `send_notification(text)` with the same text (no deep link; there is no public base URL setting today)
- Both are best effort, and failures are only logged.

### `taskboard/pr_watcher.py`

A thread that ticks every 300 s. For each `ready_for_review` task with a
`pr_url`:

1. `gh pr view <url> --json state,comments,reviews`
2. `gh api repos/<o>/<r>/pulls/<n>/comments --paginate` for inline review comments
3. Comment rules:
   - **new** means `createdAt > pr_checked_at` (the first check uses the time the task entered Ready for Review, read from the last system comment, or `updated_at`) and the body doesn't contain `<!-- yuki-conductor:agent -->`
   - reviews with an empty body are ignored
4. New comments are imported as `author="github"`, with `external_author`, `external_url` and an inline `path:line` prefix, then `transition(todo, reason="new PR comments")`
5. else when `state == "MERGED"`: `transition(done, reason="PR merged")`
6. else when `state == "CLOSED"`: `transition(done, reason="PR closed without merging")`
7. `pr_checked_at = now`

`gh` runs with `cwd` = the project path. Errors are logged, and the task is
left untouched and retried on the next tick.

### `taskboard/worktree.py` (cleanup half)

`cleanup(task)` runs on a background thread when a task enters `done`. It only
applies when `use_worktree` is set and `worktree_path` is non-null:

1. `git -C <repo> worktree remove --force <path>` (a missing path is fine) + `git worktree prune`
2. `git -C <repo> branch -D <branch>` (a missing branch is fine)
3. `git -C <repo> push origin --delete <branch>`, where "remote ref does not exist" counts as success
4. On success, clear `worktree_path` and `cleanup_error`. On failure, store `cleanup_error` and broadcast.

### Tests (phase 4)

- `test_taskboard_notify.py`: which transitions notify, Slack only when that platform is enabled, payload shape
- `test_taskboard_pr_watcher.py`: mocked `gh` output covers new comment → todo, marker comment ignored, merged → done, closed → done, gh failure → no change
- `test_taskboard_worktree.py` (cleanup half): a temp repo + bare origin. All three removals happen, and a missing remote branch still counts as success.

---

## Phase 5 — Frontend

### Shared pieces

- **`web/src/Markdown.tsx`**: `react-markdown` + `remark-gfm`. A `code`
  component override sends `language-mermaid` blocks to a `<Mermaid>`
  component, which calls `pele`'s `mount(el, text)` in a `useEffect`, with
  `try/catch` → raw `<pre>` + an error line. `pnpm add pele`. In `index.css`,
  `--pele-*` vars map to the app's existing colour vars, inside both theme
  blocks.
- **`web/src/chat/shared.tsx`**: move `HarnessIcon`, `StepList`, and the
  attachment chip/preview rendering out of `Chat.tsx` unchanged, and import
  them back. This is a pure move with no behaviour change.

### Tabs (`App.tsx`)

`ALL_TABS` gains `tasks`. The bar is Chat, Tasks, Automations, `⋯`.
`OVERFLOW_TABS = ["projects", "sessions", "status", "plugins"]`. Hash routing
accepts `#tasks/<id>`.

### `web/src/tasks/`

| File | Responsibility |
| --- | --- |
| `api.ts` | typed fetchers for every phase-3 endpoint |
| `TasksView.tsx` | list/detail split (desktop) or stacked screens (mobile, `matchMedia('(max-width: 768px)')`). Subscribes to `/ws/chat` for `task` and `step` events. |
| `TaskList.tsx` | filter pills with counts, rows, pause toggle + `running/limit` chips, "Show archived", New task button |
| `TaskDetail.tsx` | header + actions, body, timeline, live `StepList` while In Progress, composer |
| `TaskForm.tsx` | create/edit modal: title, Markdown body with Write/Preview, project select (`listProjects`), harness toggle (`/api/harnesses`), worktree checkbox (default on), attachments |
| `QuoteButton.tsx` | `selectionchange` listener scoped to the timeline. When a selection lies within one comment, a floating "Quote" button shows. Clicking it inserts `> ` lines into the composer and sets `quoteCommentId`. A long-press "Quote whole comment" is provided for mobile. |

- **Reordering:** `@dnd-kit/core` + `@dnd-kit/sortable` (pointer + touch sensors), with drag handles only on To Do rows. A drop calls `PUT /api/tasks/order`. A row menu also has "Move to top".
- **Layout:** `tasks.css`, mobile-first. Pills sit in a horizontally scrolling row. Tap targets are ≥44px. The composer sticks to the bottom on mobile.
- **Live steps:** subscribe to `step` WS events whose `conversation_id == "task:<id>"`, and replay `/api/tasks/{id}/steps` on mount. This is the same flow chat uses.

### Verification

Use the `playwright-cli` skill at 1280×800 and 375×812:

- create a task with a Mermaid body
- the diagram renders
- reorder To Do rows
- pause/resume
- the quote flow
- a running task's steps stream
- nothing scrolls horizontally on mobile

`pnpm build` and `pnpm lint` must be clean. Deploy with
`uv run yuki-conductor web rebuild`, plus a daemon restart for the backend
phases.

---

## Phase 6 — Docs and end-to-end

- CLAUDE.md:
  - add a **Taskboard** section: module map, state machine pointer to the PRD, `TASKBOARD_CONCURRENCY`, the `task` CLI, the PR marker, restart recovery
  - add the new files to Project Structure
  - update "Web tabs"
- `.env` example / docs mention `TASKBOARD_CONCURRENCY`.
- Manual end-to-end run on a scratch GitHub repo:
  1. Create a worktree task: "add a README line and open a PR".
  2. It goes In Progress, then Ready for Review, and a push arrives.
  3. Comment on the PR on GitHub. Within 5 min the task goes to To Do, then In Progress, and the agent replies.
  4. Merge. The task goes to Done, and the worktree, local branch and remote branch are gone.
  5. Separately, check ask → Needs Input → reply → resumes, check cancel, and restart the daemon mid-run to check recovery.
- Mark this plan's status as done.

---

## Risks

| Risk | Mitigation |
| --- | --- |
| pi silently creates a missing session, so a resume loses context with no error | `session_exists` pre-check (phase 2). Confirm pi's session file layout against a real run before writing it. |
| Claude Code's session file path encoding changes | Fall back on `session_missing` from the result text. Tests pin the current encoding. |
| Up to 10 concurrent Claude runs load the machine and the API quota | The limit is configurable. Pausing is instant for new pickups. |
| The agent forgets the GitHub marker, and its own PR comment re-queues the task | One harmless extra turn. The skill and system prompt both state the rule. If it recurs, also ignore comments posted while the task was In Progress. |
| Non-worktree tasks collide in a shared checkout | Accepted per the PRD (mostly read-only tasks). |
| `ws_manager` step buffer grows with many concurrent tasks | It already caps each buffer. Buffers are cleared at turn end. |
| Daemon restart mid-turn leaves a half-written worktree | Recovery re-queues the task. `ensure_worktree` reuses the existing path, and the agent sees the dirty tree. |
