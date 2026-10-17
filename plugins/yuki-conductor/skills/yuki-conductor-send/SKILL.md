---
name: yuki-conductor-send
description: >
  Use to push a message or file to the user in their yuki-conductor conversation
  (web chat or Slack thread) outside your normal reply — progress updates during
  a long task, an interim finding, an image or report produced mid-task, an
  alert from a cron run, or a follow-up after the current turn ends. Also use
  when the user asks you to "message me", "ping me", "send me the file", "let me
  know when", or "send me an update" and no other surface is specified. This is
  not email or Teams.
---

# Sending a message to the user (yuki-conductor)

This session was spawned by **yuki-conductor**. Its final response is delivered
to the user automatically — you do not need this skill for that. Use it when you
want to say something *before* the turn ends, or when the run has no reply
surface at all (a cron task that decided to stay silent, a background agent).

To attach a file to your *final* response, you don't need this skill either: put
`<attachment>/absolute/path</attachment>` in the response text.

## Command

```
yuki-conductor send "your message text"
```

By default this opens a **new** conversation in the web chat. To post into the
conversation you are already part of (web or Slack), pass its id:

```
yuki-conductor send -c <conversation-id> "your message text"
```

To send files, add `--attach` (repeatable). Text is optional when attaching:

```
yuki-conductor send -c <conversation-id> --attach /path/to/image.png "caption"
```

Flags:

- `-c` / `--conversation` — the conversation id to post into. If your system
  prompt has a "Your conversation" section, use the id named there; that is the
  thread the user is watching. Without it, you start a new conversation the user
  has to notice separately.
- `-a` / `--attach` — a file to attach. Delivered as a native attachment: inline
  image in the web chat, file upload in Slack.
- `--title` — title for a newly created conversation. Ignored with `-c`.
- Pass `-` as the text to read the message from stdin, which avoids shell
  quoting problems for long or multi-line messages:

  ```
  yuki-conductor send -c <conversation-id> - <<'EOF'
  Multi-line
  message body
  EOF
  ```

The text is rendered as markdown.

## Requirements and behaviour

- The message goes to the daemon's HTTP API on `localhost:2333` (or `WEB_PORT`).
  The daemon delivers it through the platform that owns the conversation, the
  same way a normal reply is delivered. In the web chat it is also marked unread.
- A "pushed message" marker is appended to the text so the user can tell a
  proactive note from a reply. Don't write your own — you'd get two.
- The daemon reads attached files from disk, so pass paths on this machine.
- The daemon must be running. If it isn't, the command exits non-zero with
  "Daemon not reachable" — report that plainly rather than retrying in a loop.
- If `yuki-conductor` is not on PATH, invoke it from the project directory named
  by the `YUKI_CONDUCTOR_PROJECT` environment variable:
  `uv run --directory "$YUKI_CONDUCTOR_PROJECT" yuki-conductor send ...`

## When not to use it

- Don't use it to deliver your final answer for a normal chat turn — that is
  already sent for you, and doing both makes the user read it twice.
- Don't use it to reach email or Teams, or a Slack channel you aren't already
  in a thread with. It only writes to yuki-conductor conversations.
- Don't send a stream of chatty updates. One message at a meaningful checkpoint
  (or on completion of something long) is what the user wants.
