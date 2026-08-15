---
name: yuki-conductor-restart
description: >
  Use when the user asks to restart, reload, or bounce the yuki-conductor daemon
  on Windows, or after backend Python changes are merged and need to take effect
  (e.g. "restart the daemon", "restart yuki-conductor", "pick up the new code",
  "clean up duplicate daemons"). Do NOT run `yuki-conductor daemon restart`
  directly from this session — it silently fails and leaves two daemons.
---

# Restarting the yuki-conductor daemon (Windows)

## Why the obvious command does not work here

This Claude Code session was spawned **by the daemon**, so the daemon is one of
your process ancestors. `daemon_windows.py::_kill_daemon_processes()` classifies
daemon PIDs and refuses to kill any that are in the caller's own ancestry:

```
Refusing to kill daemon process(es) [...] — this command is running inside them.
Restart from a shell outside the daemon.
```

That message goes to **stderr** and the command still exits 0. `schtasks /Run`
then fires anyway, so a *second* daemon starts, fails to bind port 2333, and the
**old** daemon keeps serving chat traffic with stale in-memory Python code. It
looks like a successful restart and is not. Never conclude a restart worked from
exit status alone.

So: run the restart **detached from your own process tree**.

## Procedure

1. Locate the bundled script. This skill is project-scoped, so it lives in the
   yuki-conductor checkout alongside this file:

   ```
   $YUKI_CONDUCTOR_PROJECT\.claude\skills\yuki-conductor-restart\restart-daemon.ps1
   ```

2. Launch it via WMI so it runs under the WMI provider host — **outside** your
   process tree, where the ancestry guard cannot trip and your own death does not
   abort it:

   ```powershell
   $script = "$env:YUKI_CONDUCTOR_PROJECT\.claude\skills\yuki-conductor-restart\restart-daemon.ps1"
   Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
       CommandLine = "pwsh -NoProfile -ExecutionPolicy Bypass -File `"$script`""
   }
   ```

   A `ReturnValue` of `0` means the process was created — it says nothing about
   the restart itself.

   Do not use `Start-Process`, `&`, or a background job: all of those stay in
   your tree and get killed mid-run.

3. Tell the user the restart is running detached and that this session will be
   torn down as part of it. The script sleeps 15 seconds first so your reply
   lands before the kill.

## What the script does

1. Sleeps 15s, then transcripts everything to `~/.yuki-conductor/restart-daemon.log`.
2. Dumps before-state (matching processes + port 2333 owner).
3. Touches `~/.yuki-conductor/.stop` — the sentinel telling the supervisor
   launcher this exit is intentional, so it does not relaunch the daemon.
4. `schtasks /End /TN YukiConductor`.
5. `taskkill /PID <n> /T /F` for every process whose `CommandLine` matches
   `*yuki-conductor*` (excluding itself). This is deliberately broader than the
   Python code's matching — it catches the `pwsh` supervisor and `uv` wrappers
   that accumulate as duplicates.
6. Removes the sentinel, then frees port 2333 via `Get-NetTCPConnection`.
7. `schtasks /Run /TN YukiConductor` and dumps after-state.

## Verifying (in a later turn or session)

Read `~/.yuki-conductor/restart-daemon.log` and check the `=== After ===`
section, or run:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*yuki-conductor*' } |
  Select-Object ProcessId, ParentProcessId, Name, CreationDate
Get-NetTCPConnection -LocalPort 2333 -State Listen | Select-Object OwningProcess
```

A healthy result is **exactly one** chain — `pwsh` (supervisor) → `uv` →
`yuki-conductor.exe` → `python` → `python` — with that last `python` owning port
2333, and all creation times within a second of each other. Two independent
chains, or differing creation times, means the old daemon survived: run the
script again.

## When NOT to use this

- **Frontend-only changes** need no restart: `uv run yuki-conductor web rebuild`
  rebuilds and hot-reloads connected browsers.
- **`cron.yaml` changes** need no restart — the daemon hot-reloads that file.
- From a **normal interactive shell** that the daemon did not spawn, plain
  `uv run yuki-conductor daemon restart` is fine and simpler.
