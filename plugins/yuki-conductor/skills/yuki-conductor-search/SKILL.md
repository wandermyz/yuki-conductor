---
name: yuki-conductor-search
description: >
  Use to search the user's past yuki-conductor web chat conversations and
  taskboard tasks by keyword, read a conversation's messages, or give the user a
  link that opens a conversation or task in the web UI. Use when the user asks
  "find the chat where we…", "which task was about…", "search my conversations /
  tasks for…", "what did we decide about… last week", "link me to that
  conversation", or otherwise refers to earlier work you have no context for.
---

# Searching conversations and tasks (yuki-conductor)

The same keyword search as the web UI's search box, plus task search, over the
daemon's HTTP API.

## Search

```
yuki-conductor search <terms...>              # conversations and tasks
yuki-conductor search --conversations <terms>  # only web chat conversations
yuki-conductor search --tasks <terms>          # only taskboard tasks
yuki-conductor search -n 25 <terms>            # hits per kind (default 10)
```

Every term is a **prefix match** and **all terms must match** (in the same
message, or the same task field / comment). Quotes and operators are plain text.
Fewer, distinctive terms find more: `search deploy timeout` beats a sentence.
Chinese, Japanese and Korean match like a substring, so `认证` finds `认证方式`.

Output is Markdown, best match first, one hit per conversation or task:

```
## Conversations (1)

- Deploy chat · `3f2a…`
  …the **runner** **timed** out…
  https://<web-ui>/#chat/3f2a…

## Tasks (1)

- #25 Runner timeouts · Done · comment #145
  …raise the **runner** limit…
  https://<web-ui>/#tasks/25
```

Matched terms are in `**bold**`. A task hit names the comment that matched, or
none when the title or description did. Archived conversations and tasks are
never returned.

## Read what you found

```
yuki-conductor conversation show <conversation-id>         # last 100 messages
yuki-conductor conversation show -n 300 <conversation-id>  # up to 500
yuki-conductor task show <task-id>                         # task + timeline
```

Both print a link to the item in the web UI.

## Jumping there

The URLs in the output open the conversation (`#chat/<id>`) or task
(`#tasks/<id>`) in the user's web UI. To send the user somewhere, put the link
in your reply. The base comes from `WEB_PUBLIC_URL` in
`~/.yuki-conductor/.env`; when that's unset the links point at
`http://127.0.0.1:<port>`, which only works on this machine.

## Requirements

- The daemon must be running; otherwise the command exits with "Daemon not
  reachable". Report that rather than retrying.
- If `yuki-conductor` is not on PATH, run it from `YUKI_CONDUCTOR_PROJECT`:
  `uv run --directory "$YUKI_CONDUCTOR_PROJECT" yuki-conductor search ...`
