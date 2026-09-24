---
name: yuki-conductor-restart
description: >
  Use when the user asks to restart, reload, or bounce the yuki-conductor daemon
  on Windows, or after backend Python changes need to take effect (e.g. "restart
  the daemon", "restart yuki-conductor", "pick up the new code"). Do NOT run
  `yuki-conductor daemon restart` — that subcommand is unsupported on Windows.
---

# Restarting the yuki-conductor daemon (Windows)

## How the daemon is kept alive

yuki-conductor does **not** manage its own daemon on Windows — there is no
scheduled task and no supervisor script in this repo. [yuki-watcher](file:///C:/Git/yuki-watcher)
(a WinForms tray app) launches `uv run --project <repo> yuki-conductor run` and
relaunches it a few seconds after it exits.

So **restarting is just exiting.** `yuki-conductor daemon <action>` prints an
unsupported message and exits 1.

## Procedure

Preferred: `POST /api/daemon/restart` on the running daemon — the same thing the
web UI's **Restart daemon** button does. It schedules `os._exit(0)` a few seconds
later, so the response (and your reply) lands first, and yuki-watcher brings the
daemon back:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:2333/api/daemon/restart
```

Fallback, if the HTTP server is wedged — kill the daemon process and let
yuki-watcher notice. This session may be a descendant of the daemon, so it will
be torn down too; tell the user before you do it:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*yuki-conductor*run*' } |
  ForEach-Object { taskkill /PID $_.ProcessId /T /F }
```

## Verifying (in a later turn or session)

```powershell
Get-NetTCPConnection -LocalPort 2333 -State Listen | Select-Object OwningProcess
```

One listener whose creation time is after the restart means it worked. If
nothing is listening after ~30s, yuki-watcher is not running — the user has to
start it.

## When NOT to use this

- **Frontend-only changes** need no restart: `uv run yuki-conductor web rebuild`
  rebuilds and hot-reloads connected browsers.
- **`cron.yaml` changes** need no restart — the daemon hot-reloads that file.
