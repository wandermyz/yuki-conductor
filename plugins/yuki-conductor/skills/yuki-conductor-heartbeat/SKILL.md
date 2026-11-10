---
name: yuki-conductor-heartbeat
description: >
  Use to keep watching something from THIS conversation or task: wake its own
  session on an interval with all of its context, e.g. "monitor the permission
  grant every 60min", "keep an eye on the deploy", "check the PR until it's
  ready", "let me know when CI passes", or whenever you are about to wait on
  something external (an access request, a reviewer, a deploy, CI). Also to
  change, pause, check or stop that heartbeat. For a standalone recurring job
  (a daily briefing, a weekly report) use yuki-conductor-cron instead.
---

# Heartbeat: wake this conversation or task on an interval

This session was spawned by **yuki-conductor**. A heartbeat makes the daemon
resume *this* session again and again, with a prompt you choose, until the
thing you are watching is resolved. Each tick is a turn in the same session, so
you keep everything you already know: why you are waiting, what to do next.

The rule: **if the recurring check needs this conversation's context, use a
heartbeat. If it's a self-contained job, use cron** (`yuki-conductor-cron`).

## Commands

```
yuki-conductor heartbeat set [--every 270s] [--for 7d] "what each tick should check and do"
yuki-conductor heartbeat status
yuki-conductor heartbeat pause
yuki-conductor heartbeat resume
yuki-conductor heartbeat stop
```

The target defaults to the run you are in: your task in a taskboard turn
(`YUKI_TASK_ID`), otherwise your conversation (`YUKI_CONVERSATION_ID`). Pass
`-c <conversation-id>` or `--task <id>` only to target another one. A target has
at most one heartbeat; `set` again replaces it.

Write the prompt for your future self: what to check, how to tell it's done,
and what to do then. For example:

```
yuki-conductor heartbeat set --every 60m "Check whether my membership of <group> is active (az ad group member check …). Once it is, resume the migration from step 3 and stop the heartbeat."
```

## Choosing `--every`

- **The user named an interval** ("every 60min", "hourly", "every 2 minutes"):
  use exactly that.
- **Something is actively changing** (a PR under review, CI running): use the
  default, 270s. It keeps the prompt cache warm, so each tick is cheap.
- **Waiting on something slow** (a permission grant, a long deploy): 30m–60m.
- Avoid 5–30 minutes unless asked: every tick misses the cache, and there are
  enough ticks for that to add up. The CLI warns about it.
- The minimum is 60s.

`--for` is how long it lasts: 7 days at most, the default. If you still need to
watch after it expires, run `heartbeat set` again; that restarts the clock.

## On each tick

The tick's message starts with `[Heartbeat — fires every …]` and your prompt.
It is a scheduled check, not the user.

- **Nothing changed / nothing for the user:** do as little as needed, keep tool
  output small, and end with `<silence>`. Nothing is shown to the user.
- **The user should see this:** include `<notify>`. In a conversation the
  reply is posted and the user is notified. On a task it becomes a comment and
  the task moves to Ready for Review; if you need the user's decision, run
  `yuki-conductor task ask` as usual (Needs Input).
- **Resolved:** run `yuki-conductor heartbeat stop`, then reply (with
  `<notify>` if the user should hear about it).
- A failing tick is always shown to the user; three failures in a row pause the
  heartbeat.

A task's heartbeat only fires while the task is waiting (Needs Input or Ready
for Review), and stops when the task is Done or archived. A conversation's stops
when it is archived or deleted. The web UI only shows that a heartbeat is
active (a 🕒 on the conversation or task); the user changes or stops it by
asking you, so run these commands when they do. They can also stop it outside
this session, and you won't be told: `heartbeat status` shows whether it's
still there.

## Requirements

- The command talks to the daemon on `localhost:2333` (or `WEB_PORT`). If it
  reports "Daemon not reachable", say so rather than retrying in a loop.
- If `yuki-conductor` is not on PATH, run it from the project directory named
  by `YUKI_CONDUCTOR_PROJECT`:
  `uv run --directory "$YUKI_CONDUCTOR_PROJECT" yuki-conductor heartbeat ...`
