---
name: yuki-conductor-task
description: >
  Use when working on a yuki-conductor taskboard task (your system prompt has a
  "Taskboard" section): read your task and its comment timeline, post progress
  comments or quote-replies, ask the user for input when blocked, register the
  pull request you opened, mark the task done, or create follow-up tasks. Also use in any session
  when the user asks to "create a task", "add this to the taskboard", "queue
  this for later", or "have an agent work on this in the background".
---

# The yuki-conductor taskboard

The taskboard is a queue of agent tasks. yuki-conductor picks each one up as
its harness has capacity, runs an agent on it in the task's project (often in
its own git worktree), and moves it through **To Do → In Progress → Needs
Input / Ready for Review → Done**. The user follows along in the web UI's
Tasks tab and gets a notification when a task needs them.

Every command talks to the daemon's HTTP API, so the daemon must be running.
Inside a task run, `YUKI_TASK_ID` is set and every command defaults to your own
task, so you can leave out the id.

If `yuki-conductor` isn't on PATH, run it from the project directory named by
`YUKI_CONDUCTOR_PROJECT`:
`uv run --directory "$YUKI_CONDUCTOR_PROJECT" yuki-conductor task ...`

## Reading

```
yuki-conductor task show          # your task: status, body, every comment
yuki-conductor task show 12       # another task
yuki-conductor task list --status todo --project my-app
```

The timeline numbers every comment (`#42`), so you can quote-reply to one.

## Commenting

Your **final response is already posted** to the task as a comment, so don't
repeat it with this command. Use `comment` for a checkpoint worth keeping
mid-turn, or to attach a file:

```
yuki-conductor task comment "Schema migrated; starting on the API layer."
yuki-conductor task comment --quote 42 "> Should it be cached?

Yes, for 5 minutes, see the new CACHE_TTL setting."
yuki-conductor task comment --attach /abs/path/diagram.png "Current architecture"
yuki-conductor task comment - <<'EOF'
Long, multi-line Markdown. Mermaid diagrams in ```mermaid blocks render in the UI.
EOF
```

## Asking for input

When you're blocked, or a decision belongs to the user, ask, then **end your
turn**:

```
yuki-conductor task ask "Postgres or SQLite for the cache table? Postgres needs a migration."
```

Nobody answers mid-turn. The task moves to Needs Input and the user is
notified. Their reply starts your next turn in the same session, with the
reply as the prompt. Don't loop waiting, and don't guess on decisions the user
would want to make.

## Finishing without a pull request

When the task is complete and there's nothing for the user to review or
merge — a question answered, a report delivered, a change pushed that needs
no PR — close it yourself:

```
yuki-conductor task done
```

The task moves to Done when your turn ends, and its worktree and branches are
cleaned up then (a branch with commits no remote has is kept). Your final
response is still posted, so end with the summary as usual. Don't use it when
you opened a PR (merging that finishes the task), when you've asked a
question, or when the user would want to check the result first; a user
comment during the turn also overrides it.

## Registering a pull request

After opening a PR, register it:

```
yuki-conductor task pr https://github.com/owner/repo/pull/123
```

The task then goes to Ready for Review when your turn ends, and the board
watches the PR:
- New review comments come back to you as a new turn.
- A merge (or close) finishes the task and cleans up its worktree.

**Everything you post to GitHub must end with the agent footer.** That covers
the PR description, PR comments and review replies. The agent may post as the
user's own account, so this footer is the only way the board can tell your
comments from the user's:

```
_🤖 Posted by yuki-conductor agent (task #<id>)_
<!-- yuki-conductor:agent -->
```

## Creating a task

```
yuki-conductor task create --project my-app --title "Add rate limiting" --worktree \
  "Limit /api/login to 5 attempts per minute per IP. See #12 for context."
```

- `--project` must be a project registered in the web UI's Projects tab. Run
  `yuki-conductor task list` to see which projects tasks already use.
- `--worktree` gives the task its own branch and checkout. Use it for anything
  that changes code. Leave it off for read-only work like research or reports.
- `--harness` picks the agent CLI and `--model` picks a harness-native model id.
  Both inherit the current agent run's selection (conversation, cron, or task).
  Outside an agent run, the machine's defaults apply. Changing `--harness`
  clears the inherited model; pass `--model` to select one for that harness.
- The body is Markdown. Pass `-` to read it from stdin.

A task created from inside a task is linked to it as a follow-up. That link is
informational only: the new task runs on its own, and nothing waits for it.
