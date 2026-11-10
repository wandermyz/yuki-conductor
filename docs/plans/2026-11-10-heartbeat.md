# Heartbeat: recurring wake-ups for a conversation or task

## Problem

Cron is the only way to make an agent act without a human typing. A cron run
is a **fresh, isolated session** that belongs to no conversation and no task.
That suits a daily briefing. It fits poorly when the recurring work belongs
to something already in progress:

- **pr-babysitter** works around the isolation at every step. It appends a
  `pr-<id>-babysitter` block to the global `cron.yaml`. Because nothing ties
  one run to the next, it keeps its own `RUNBOOK.md` and a `state.json` with a
  `runLock` and dedupe sets (`handledCommentThreadIds`, `notifiedActionKeys`).
  When the PR closes it edits its own block out of `cron.yaml`. About half the
  skill is plumbing that exists only because the runs share no session.
- **Waiting on an external event**, such as an access request being approved,
  a deploy finishing or a reviewer replying. The agent that asked for the
  permission knows why it wanted it and what to do once it lands. A cron job
  would need all of that restated in a standalone prompt.

A **heartbeat** fills this gap. yuki-conductor wakes an *existing* session on
an interval, so the agent continues with all of its context. It stops when the
conversation or task it belongs to is finished.

## Prior art: Hermes Agent and OpenClaw

**Hermes Agent** (`website/docs/user-guide/features/heartbeat.md`,
`hermes_cli/heartbeat.py`) is the model this design follows:

- **Scope:** a session has at most one heartbeat. It is set with
  `/heartbeat every 10m <prompt>` and controlled with `status`, `pause`,
  `resume` and `clear`. The interval is at least 60s.
- **Context:** the tick is injected as a user-role message in the **main
  session**, so it shares that session's context and prompt cache. It never
  arrives in the middle of a turn.
- **Idle only:** a tick fires only when the session is idle and its input
  queue is empty, so user messages always come first. Missed ticks collapse
  into a single turn.
- **Silence:** a tick that ends with `NO_REPLY` / `[SILENT]` delivers nothing,
  and heartbeat turns show no typing or progress UI. Failures and approval
  prompts stay visible.
- **Delivery:** replies go to the surface that set the heartbeat.
- **Persistence:** stored per session, survives restarts, and is cleared when
  the session is reset.
- **The rule they give:** *"If the recurring prompt needs the conversation's
  context, use /heartbeat. If it's a self-contained job, use cron."*

| | Heartbeat | Cron |
|---|---|---|
| Runs in | this conversation, with full context | a fresh, isolated session each tick |
| How many | one per session | unlimited |
| Best for | "keep an eye on X in this thread" | standing jobs, reports, watchdogs |

**OpenClaw** took a different route. It had one heartbeat per *agent*, driven
by a `HEARTBEAT.md` checklist in the workspace, with `HEARTBEAT_OK` as the
silence token and active hours. On 2026-10-21 it retired heartbeat and folded
it into ordinary automation jobs. That shows a *global* heartbeat is really
just cron. Hermes's per-session version is the one that adds something new,
so this design follows Hermes.

## Design

### Concept

A heartbeat is a recurring instruction attached to **one target**: either a
web or Slack conversation, or a taskboard task. Each tick **resumes the
target's session** and gives it the heartbeat prompt. If the reply contains
`<notify>`, it is delivered like any other reply. Otherwise the tick stays
quiet. A target has at most one heartbeat.

| | Heartbeat | Cron |
|---|---|---|
| Lives on | a conversation or task (DB row) | `cron.yaml` (global) |
| Session | the target's own, resumed | fresh per run |
| Timing | approximate; interval counted from the *end* of the last tick; idle only | exact cron expression |
| Created by | the agent itself (CLI) or the user (UI or Slack) | the user, or the agent editing `cron.yaml` |
| Ends | explicit stop, 7-day expiry, or the target being done, archived or deleted | when someone removes it |
| Output | into the target's thread or timeline | a new thread for each `<notify>` |

The silence protocol matches cron's: nothing is delivered unless the reply
contains `<notify>`. Failures are always surfaced. Using the same vocabulary
means one rule for agents to learn.

### Data model

There is one new table. Both kinds of target share it, so one scheduler and
one CLI serve both.

```sql
CREATE TABLE heartbeats (
  target_kind     TEXT NOT NULL,      -- 'conversation' | 'task'
  target_id       TEXT NOT NULL,      -- conversation key (web id or Slack thread) / task id
  prompt          TEXT NOT NULL,
  interval_s      INTEGER NOT NULL,   -- default 270 (keeps the prompt cache warm), min 60
  next_due_at     TEXT NOT NULL,
  expires_at      TEXT NOT NULL,      -- created_at + --for; default and max 7 days
  paused_at       TEXT,
  created_by      TEXT NOT NULL,      -- 'agent' | 'user'
  created_at      TEXT NOT NULL,
  last_tick_at    TEXT,
  last_outcome    TEXT,               -- 'quiet' | 'notified' | 'error' | 'skipped-busy'
  consecutive_errors INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (target_kind, target_id)
);

CREATE TABLE heartbeat_ticks (        -- pruned to the last 50 per target
  id INTEGER PRIMARY KEY, target_kind TEXT, target_id TEXT,
  started_at TEXT, finished_at TEXT, outcome TEXT, response TEXT, error TEXT
);
```

`heartbeat_ticks` plays the same role for heartbeats that `cron_runs` plays
for cron. Quiet ticks are recorded there and **not** in `messages` or
`task_comments`, which keeps the chat and the task timeline readable after
100 quiet checks.

### Scheduler (`heartbeat.py`)

A single daemon thread started from `runtime.start()`, modelled on the
harvester's loop:

1. Every 15s, or as soon as `wake()` is called, select the heartbeats that are
   due, not paused and not expired. Expired rows are deleted, and a line is
   posted to the target ("Heartbeat expired after 7 days").

   **Expiry is a hard cap of 7 days** for conversations and tasks alike.
   `--for` may be shorter, never longer (the API rejects it). An agent that
   still needs to watch after that runs `heartbeat set` again, which restarts
   the clock. That is a deliberate choice it makes and that shows on the
   timeline, so a forgotten heartbeat can't run indefinitely.
2. **Busy check.** Skip a target while a turn is running on it: for a
   conversation, the `_active_processes` registry in `harness/process.py`;
   for a task, `status == in_progress`. The row stays due, so it fires as
   soon as the target is idle. Because missed ticks collapse into one, a long
   user turn costs at most one deferred tick. Nothing piles up.
3. Fire the tick on its own thread (see the per-kind sections below). When it
   finishes, set `next_due_at = finished_at + interval`. Measuring from the
   end of the tick means a slow tick can never overlap the next one.
4. **On startup**, any overdue row fires once, with 0–60s of jitter, so a
   restart doesn't wake every target at the same instant.
5. **Back-off.** After 3 errors in a row the heartbeat pauses itself and
   posts the error to the target, which triggers a notification through the
   normal path.

The taskboard pause switch also suspends task heartbeats.

This also fixes a gap the code survey turned up: today nothing stops two turns
running on the same conversation at once (`set_processing` only drives the
UI). The busy check needs a real per-conversation guard, so add
`conversation.try_begin(key)` / `end(key)` around `handle_incoming_message`.
A user message that arrives while a tick is running then **waits for the tick
to finish** rather than resuming the same session in parallel. That fixes an
existing race as well.

### Conversation ticks

The tick goes through `handle_incoming_message` with a synthetic
`IncomingMessage(kind="heartbeat")`, so harness, model, cwd and the
`skip_permissions` / `wait_for_mcp` settings are resolved exactly as for a
user turn. The prompt text is:

```
[Heartbeat — fires every 4m30s. Set by you at 14:02: "<reason/prompt>"]
<prompt>

If nothing changed or nothing needs the user, end with <silence>.
Include <notify> only if the user should see this reply.
When the thing you are watching is resolved, run
`yuki-conductor heartbeat stop` before replying.
```

How a heartbeat turn differs from a user turn:

- The prompt is **not** stored as a user message. A quiet tick stores nothing
  in `messages`. A `<notify>` tick stores only the assistant reply, tagged
  `heartbeat`, which the chat renders with a small ♡ marker. This is the
  reply-side counterpart of `PUSH_MARKER`.
- No live steps are broadcast and no Slack status shimmer is shown unless the
  reply notifies. Quiet ticks should be invisible.
- The session id still advances (`set_session_id`), so the next user message
  resumes a session that has seen the ticks.
- A notifying tick marks the conversation unread and sends a web push, as a
  push does. On Slack it posts into the thread.

### Task ticks

Tasks already have a status machine and a single pickup path, so task ticks
go through the **harvester** rather than the scheduler's own threads. A due
heartbeat on a task only marks it `heartbeat_due`. The harvester then claims
it like any other work, which keeps the per-harness concurrency limits and the
taskboard pause switch in force.

- **Eligible statuses:** Ready for Review and Needs Input, i.e. a task that is
  waiting on something external. A task in To Do or In Progress is already
  about to run or running, so its tick waits. A Done task has no heartbeat
  (see Lifecycle).
- **Claiming:** a new transition, `claim_heartbeat`, moves the task from
  Ready for Review or Needs Input to In Progress. It is **quiet**: no system
  comment and no notification. It records `heartbeat_return_status` so the
  task can go back where it came from.
- The turn uses `runner.run_turn` with `prompts.heartbeat(...)` instead of
  `follow_up`, and passes any undelivered comments along as usual.
- **Outcome.** The order of `resolve_outcome` is unchanged, with one addition
  at the end:
  - The agent ran `task ask`, ran `task done`, or a new user comment arrived:
    handled as today, with the normal notifications.
  - The reply contains `<notify>`: it becomes an agent comment, and the task
    moves with `notify` according to what the tick found:
    - **Needs Input** when the agent needs the user, e.g. a decision or an
      approval. It signals this with `task ask`, as on any turn.
    - **Ready for Review** otherwise: it acted or has a result to report,
      such as "rebased and CI is green" or "PR is ready to merge".

    The return status doesn't matter; the result decides. The heartbeat keeps
    running in both states.
  - Otherwise the tick was quiet. The task returns quietly to
    `heartbeat_return_status`, with no comment. The tick is recorded only in
    `heartbeat_ticks`.
- `recover()` on startup puts any In Progress task that came from a heartbeat
  back into its return status, instead of moving it to the top of To Do.

The PR watcher keeps its job: it imports GitHub comments when the Tasks tab
opens. A task heartbeat is how a PR task gets watched *without* the tab open,
which pr-babysitter currently does through cron.

### Lifecycle

| Event | Heartbeat |
|---|---|
| Task → Done, task archived, conversation archived/deleted | deleted |
| Task reopened | stays deleted; the agent sets a new one if needed |
| Conversation's harness disabled | paused, with a note |
| `expires_at` reached | deleted, with a note |
| 3 consecutive errors | paused, error surfaced |
| Daemon restart | kept; overdue ones fire once |

### Who sets it

**The agent**, through a new CLI and a bundled skill. This is the main path:
the agent knows when it is about to wait on something.

```
yuki-conductor heartbeat set [--every 270s|60m|1h] [--for 3d (max 7d)] "check whether ... ; if granted, continue X"
yuki-conductor heartbeat status | pause | resume | stop
```

**Choosing the interval.** It defaults to 270s. The user asks for a different
one in plain language, e.g. "monitor the permission grant every 60min" or
"check the deploy hourly". The agent turns that into `--every`. The bundled
skill tells the agent to:

- use the user's interval exactly when one is given;
- otherwise use 270s while something is actively changing, such as a PR under
  review or CI running;
- otherwise use 30–60m when waiting on something slow, such as a permission
  grant or a long deploy.

The CLI warns, but doesn't refuse, on intervals between 5 and 30 minutes,
which miss the cache on every tick without the low tick count to offset it
(see Costs and risks). The user can still change it at any point, from the
chat menu or by asking the agent.

The CLI works out its target the same way `send` and `task` do: from
`YUKI_TASK_ID` for a task turn, otherwise from the run's conversation id. That
needs a `YUKI_CONVERSATION_ID` env var next to the system-prompt line.
`-c/--task` override the target. It talks to the daemon over HTTP
(`/api/heartbeats/{kind}/{id}`: GET, PUT, PATCH, DELETE).

A bundled `yuki-conductor-heartbeat` skill teaches the agent when to use it.
The `yuki-conductor-cron` description gains a routing line: *"to keep watching
something in this conversation or task, use heartbeat, not cron."*

**The user** can manage it in three places:

- **Web chat:** a "Heartbeat…" item in the conversation menu (interval,
  prompt, stop) and a header chip reading `♡ 4m30s · next in 2m · last: quiet`.
  Clicking the chip opens the tick log.
- **Task detail:** the same chip, plus a field in the task form so a task can
  be created with a heartbeat ("open the PR, then watch it").
- **Slack:** `/<prefix>-heartbeat [60m] <prompt>` and
  `/<prefix>-heartbeat stop` in a thread.

### pr-babysitter, rewritten

pr-babysitter becomes a **task**: "Babysit PR !1234". In its first turn the
agent sets up the worktree, reads the PR and runs
`heartbeat set "triage new comments, rebase if base
moved, check CI; <notify> only when you acted or it's ready to merge"`.

Every later tick resumes that same session. The task worktree replaces the
custom one. The session's memory, plus the tick log, replaces `state.json`'s
dedupe sets. The harvester's one-turn-per-task rule replaces `runLock`. Once
the PR completes, the PR watcher, or the agent's own `task done`, moves the
task to Done, which deletes the heartbeat and cleans up the worktree. No
`cron.yaml` edits are needed. The email notifications can stay in the skill,
or be replaced by `<notify>` → web push.

**Permission-grant monitor:** in a chat, the agent files an access request,
runs `heartbeat set --every 60m "check whether <group> membership is active;
once it is, resume the migration and stop the heartbeat"`, and replies. Hours
later a tick sees the grant, carries on with the original work in the same
session, and notifies.

## Costs and risks

- **Context growth.** Every tick adds a turn to the session: about 320 a day at
  the 270s default. Mitigations:
  - tick prompts stay short;
  - quiet ticks are told to keep tool output small;
  - the 7-day expiry cap;
  - the harnesses' own auto-compaction.
- **Prompt cache.** Anthropic's prompt cache lives for 5 minutes and is
  refreshed on every hit. A cached read costs 0.1× input, while re-writing a
  cold cache costs 1.25×. Since the interval is measured from the end of the
  previous tick, the cost of re-reading the context *C* is roughly:

  | Interval | Cache | Ticks/hour | Context cost/hour |
  |---|---|---|---|
  | 4m (≤ 270s) | warm | 15 | 1.5 *C* |
  | 10m | cold | 6 | 7.5 *C* |
  | 30m | cold | 2 | 2.5 *C* |
  | 60m | cold | 1 | 1.25 *C* |

  Intervals between 5 and roughly 45 minutes are the worst of both: every
  tick is a cache miss, and there are enough ticks for that to add up. The
  tick log records `context_tokens` so the cost is visible.
- **A user typing just as a tick fires** waits for the tick to finish, up to
  one tick's duration. The ♡ "checking…" indicator in the chip explains the
  wait. The alternative is to cancel the tick when a user message arrives.
  It's cheap, since the tick restarts later, but it could interrupt a rebase
  halfway through. Waiting is the safer default.

## Scope and phasing

1. `heartbeats` and `heartbeat_ticks` tables, the scheduler,
   **conversation** ticks, the per-conversation turn guard, the CLI, the
   bundled skill, and the web chip and menu.
2. **Task** ticks: harvester integration, `claim_heartbeat`, the quiet return
   path, the task chip and the form field. Then port pr-babysitter.
3. Slack slash command.

**Non-goals:**

- more than one heartbeat per target;
- forked ticks (running quiet ticks on a throwaway `--fork-session` copy to
  keep the main session small). Revisit only if context growth hurts;
- active hours;
- a `HEARTBEAT.md` file (OpenClaw's design, which OpenClaw itself has dropped);
- heartbeats on cron runs. Cron threads are conversations, so a heartbeat
  *can* be attached to one after it notifies, and nothing extra is needed for
  that.

## Decisions

- **Expiry:** a hard cap of 7 days for every target. Renewing it means an
  explicit `heartbeat set` again.
- **A notifying task tick** moves the task to Needs Input (the agent needs the
  user) or Ready for Review (a result), whichever the tick found.
- **Stopped by the user:** the user can stop any heartbeat. The agent gets no
  note about it.
- **No forked ticks** in this design.
- **Default interval:** 270s, so the prompt cache stays warm. The user sets a
  different one in plain language ("every 60min") and the agent passes it as
  `--every`.
