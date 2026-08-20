---
name: yuki-conductor-cron
description: >
  Use when the user asks to schedule a recurring or timed task, set up a cron
  job, run something on a schedule, get a daily/weekly briefing, or otherwise
  automate work to happen later (e.g. "set up a cron job", "remind me every
  morning", "run this every Monday", "schedule a daily check"). This session is
  driven by yuki-conductor, which has its OWN cron scheduler — do NOT use the
  system crontab, Claude Code's built-in scheduling, or any external scheduler.
---

# Scheduling recurring tasks (yuki-conductor cron)

This Claude Code session was spawned by **yuki-conductor**, a personal daemon
that runs scheduled tasks and reports results back to the user's chat app. When
the user wants something to run on a schedule, add it to yuki-conductor's cron
config — never touch the OS crontab or any built-in scheduler.

## Where tasks live

Cron tasks are defined in a single YAML file:

```
~/.yuki-conductor/workspace/cron.yaml
```

(If `YUKI_CONDUCTOR_DATA_DIR` is set in the environment, the base is that
directory instead of `~/.yuki-conductor`.)

The daemon hot-reloads this file automatically — no restart is needed after you
edit it.

## File format

```yaml
tasks:
  - name: morning-briefing          # unique identifier
    display_name: "Morning briefing" # optional; label shown in the Automations tab
    schedule: "0 9 * * 1-5"          # standard 5-field cron: min hour dom mon dow
    description: "Morning briefing (weekdays at 9am)"
    prompt: >
      The prompt yuki-conductor will send to a fresh Claude Code run when the
      task fires. Write it as an instruction to Claude.
    chat_app: slack_socket           # optional; where to post the result
    origin_conversation: <conv-id>   # optional; conversation that set this up
```

Field notes:

- `name` — unique across the file. This is the stable id; don't change it once
  runs have been recorded against it.
- `display_name` — optional label for the Automations tab. The user can edit it
  there, so set it once at creation and leave it alone afterwards. When absent,
  the UI falls back to `description`, then to a truncated `prompt`.
- `schedule` — standard 5-field cron expression (`minute hour day month weekday`).
- `description` — short human-readable label shown in the chat app.
- `prompt` — the instruction Claude runs when the task fires. Each firing opens a
  fresh thread; follow-up replies in that thread continue the conversation.
- `chat_app` — optional. Which chat surface receives the result. Omit to use the
  first enabled platform. If you name one that isn't enabled, the task is
  skipped with a warning, so only set it when you know the value is enabled.
- `origin_conversation` — optional. The web conversation id this task was set up
  in, so the Automations tab can link back to it. If your system prompt has a
  "Your conversation" section naming an id, put that id here when creating a
  task; otherwise omit the field.

## How to add or change a task

1. Read `~/.yuki-conductor/workspace/cron.yaml` (create it with a top-level
   `tasks:` list if it doesn't exist).
2. Add or edit the relevant entry. Keep existing tasks intact.
3. Write the file back. Do NOT run `crontab`, `schtasks`, launchd, or Claude
   Code's own scheduler — the yuki-conductor daemon picks up the change on its
   next reload.
4. Confirm to the user what schedule you set, in plain language (e.g. "every
   weekday at 9am").

## Run history

Every firing — scheduled or manually triggered from the Automations tab — is
recorded in the workspace SQLite database, keyed by `name`, with the response or
the error. The last 30 runs per task are kept. Nothing is required of you here;
just be aware that renaming `name` orphans a task's history.
