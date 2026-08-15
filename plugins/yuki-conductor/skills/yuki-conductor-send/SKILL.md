---
name: yuki-conductor-send
description: >
  Use to push a message to the user in the yuki-conductor web chat outside your
  normal reply — progress updates during a long task, an interim finding, an
  alert from a cron run, or a follow-up after the current turn ends. Also use
  when the user asks you to "message me", "ping me", "let me know when", or
  "send me an update" and no other chat surface is specified. Only reaches the
  web chat; this is not email, Slack, or Teams.
---

# Sending a message to the user (yuki-conductor web chat)

This session was spawned by **yuki-conductor**. Its final response is delivered
to the user automatically — you do not need this skill for that. Use it when you
want to say something *before* the turn ends, or when the run has no reply
surface at all (a cron task that decided to stay silent, a background agent).

## Command

```
yuki-conductor send "your message text"
```

By default this opens a **new** conversation in the web chat. To post into the
conversation you are already part of, pass its id:

```
yuki-conductor send -c <conversation-id> "your message text"
```

Flags:

- `-c` / `--conversation` — the web conversation id to post into. If your system
  prompt has a "Your conversation" section, use the id named there; that is the
  thread the user is watching. Without it, you start a new conversation the user
  has to notice separately.
- `--title` — title for a newly created conversation. Ignored with `-c`.
- Pass `-` as the text to read the message from stdin, which avoids shell
  quoting problems for long or multi-line messages:

  ```
  yuki-conductor send -c <conversation-id> - <<'EOF'
  Multi-line
  message body
  EOF
  ```

The text is rendered as markdown in the web UI.

## Requirements and behaviour

- The message goes to the daemon's HTTP API on `localhost:2333` (or `WEB_PORT`).
  It is persisted as an assistant message and pushed live over WebSocket, so a
  browser that is open sees it immediately and one that reconnects still finds
  it in scrollback. The conversation is marked unread.
- The web UI appends its own "pushed message" marker to the text so the user can
  tell a proactive note from a reply. Don't write your own — you'd get two.
- The daemon must be running. If it isn't, the command exits non-zero with
  "Daemon not reachable" — report that plainly rather than retrying in a loop.
- If `yuki-conductor` is not on PATH, invoke it from the project directory named
  by the `YUKI_CONDUCTOR_PROJECT` environment variable:
  `uv run --directory "$YUKI_CONDUCTOR_PROJECT" yuki-conductor send ...`

## When not to use it

- Don't use it to deliver your final answer for a normal chat turn — that is
  already sent for you, and doing both makes the user read it twice.
- Don't use it to reach email, Slack, or Teams. It only writes to the web chat.
- Don't send a stream of chatty updates. One message at a meaningful checkpoint
  (or on completion of something long) is what the user wants.
