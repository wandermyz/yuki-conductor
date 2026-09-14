# Systematic Plugin Support

## Problem

Plugins exist today, but only by accident of Python packaging. A plugin is
"installed" by `uv pip install`-ing a package that happens to declare entry
points, and "enabled" by hand-editing `CHAT_APPS` in `~/.yuki-conductor/.env`.
Four things are wrong with that:

1. **No plugin identity.** A plugin isn't one thing — it's up to three unrelated
   entry points (`yuki_conductor.chat_plugins`, `.cli_plugins`,
   `.skill_plugins`) that only look like a unit because they share a
   `pyproject.toml`. Nothing in the daemon knows they belong together, so
   nothing can report "plugin X is installed and contributes a channel, a CLI,
   and a skill."
2. **`CHAT_APPS` names the wrong axis.** It conflates "which plugins are on"
   with "which chat surfaces exist". When we add a second injection point —
   automation triggers are the next one — there is nowhere to put it without a
   second parallel env var.
3. **No registry.** There's no list of what's installed. A package pruned by
   `uv sync` disappears silently and the daemon just fails to build a receiver
   at startup, deep in the log.
4. **No UI, and no way to apply changes.** Every plugin operation is a text
   edit plus a daemon restart, and restarting the daemon from a spawned session
   is itself a known trap (see `.claude/skills/yuki-conductor-restart`).

## Scope

This plan covers four changes, in dependency order:

1. A plugin manifest with **injection points**, of which `channels` is the only
   one implemented now. `CHAT_APPS` is renamed to reflect this.
2. A **plugin registry** — installed vs. enabled — with REST endpoints and a
   web UI to toggle plugins, plus an explicit **Restart daemon** button.
3. Tab budget: the web UI drops to **4 primary tabs**, with Sessions, Status,
   and the new Plugins view folded behind a **"…" (More)** tab.
4. **Local-path discovery**: a plugin is registered by pointing at a directory.

Non-goals for this pass: a plugin marketplace, remote/registry installs,
versioning or compatibility ranges, per-plugin sandboxing, and the additional
injection points themselves (triggers, tools, views). The manifest is shaped so
those can land later without another rename.

## 1. Injection points, and `CHAT_APPS` → `channels`

### Concept

A **plugin** is a named unit that contributes capability at one or more
**injection points**. An injection point is a well-known extension slot the
daemon owns:

| Injection point | Status | What a plugin contributes |
| --- | --- | --- |
| `channels` | **implemented now** | a `ChatAppReceiver` factory — a chat surface the daemon listens on |
| `triggers` | planned | a source of automation events that can fire a cron-like task |
| `skills` | already exists, folds in | a Claude Code plugin dir injected via `--plugin-dir` |
| `cli` | already exists, folds in | subcommands grafted onto `yuki-conductor` |

Only `channels` is enabled/disabled in this pass. `skills` and `cli` already
work through entry points and keep working; the manifest just makes them
*visible* in the registry so the UI can show what a plugin brings. `triggers` is
listed to fix the shape of the design, not to be built.

### Rename

`CHAT_APPS` becomes `CHANNELS`, and `config.chat_apps()` becomes
`config.channels()`. Per the repo's no-backwards-compatibility rule, `CHAT_APPS`
is deleted outright — no fallback read, no deprecation warning. The workspace
`.env` is updated in the same change.

But the env var stops being the source of truth for *enablement* (see §2). Its
new, narrower job: an optional **override/ordering** hint. Precedence:

- If `CHANNELS` is set, it wins — exactly today's semantics, and the escape
  hatch when the registry file is broken or the UI is unreachable.
- If unset, enabled channels come from the registry, in registry order.
- `CHANNELS=none` or empty still means "no channels; web + cron only".

Ordering still matters: the cron scheduler uses the first enabled platform as
its default notification target.

The term "chat app" is retired throughout — `_build_receiver`'s error message,
`runtime.start()`'s log line, the `docs/plans/chat-plugin-architecture.md`
vocabulary, and CLAUDE.md's "Chat Apps" section become "Channels". `slack_socket`
keeps its name as a channel.

### Built-in vs. plugin

Slack (`slack_socket`) is currently special-cased in `runtime._build_receiver`.
It stays in-tree, but it gets registered as a **built-in plugin** in the
registry rather than an `if name == ...` branch — same descriptor shape as an
external plugin, flagged `builtin: true` so it can't be uninstalled (only
disabled). That gives the registry at least one entry on a fresh install and
keeps one code path for receiver construction.

## 2. Plugin registry

### Storage

`~/.yuki-conductor/workspace/plugins.yaml` — same workspace-not-repo rule as
`cron.yaml`, since a plugin path can reveal private work. An example goes in
the repo as `plugins.example.yaml`.

```yaml
plugins:
  - name: slack
    builtin: true
    enabled: true

  - name: example-channel
    path: ~/Projects/yuki-conductor-example
    enabled: false
```

A record is deliberately thin: identity, where it came from, and whether it's
on. Everything else — what it contributes, whether it currently imports — is
*derived at load time* from the plugin's own manifest, never cached in this
file. A stale cache of capabilities is exactly the failure mode we're trying to
remove.

`plugin_config.py` mirrors `cron_config.py`'s division of labour:

- `load_plugins()` — the single parser, used by both runtime and web server.
- `set_plugin_field()` — line-based surgical edit (same reason as
  `cron_config.set_task_field`: a PyYAML round-trip reflows and drops comments).
  Only `enabled` is writable from the UI.
- `add_plugin(path)` / `remove_plugin(name)` — append/delete a record.

### Plugin manifest

Each plugin directory declares itself in `yuki-plugin.yaml` at its root:

```yaml
name: example-channel
description: One line, shown in the plugins list.
version: 0.1.0
injection_points:
  channels:
    - name: example
      factory: yuki_conductor_example:create_receiver
  skills:
    plugin_dir: ./claude-plugin
```

The `factory` string is `module:attr`, resolved with `importlib` — the same
contract entry points already use, just declared in a file we can read without
the package being installed. This matters for the registry: we can list and
describe a plugin, and report *why* it won't load, without importing it.

Entry points remain supported and are merged in, so an installed package that
declares `yuki_conductor.chat_plugins` keeps working with no manifest. Merge
rule: manifest-declared plugins take precedence on name collision, and a plugin
discovered *only* via entry points appears in the registry as
`source: entry_point` with `enabled` still governed by `plugins.yaml`.

### Load and status

`plugins.py` exposes:

```python
@dataclass(frozen=True)
class PluginDescriptor:
    name: str
    description: str
    version: str | None
    source: Literal["builtin", "path", "entry_point"]
    path: Path | None
    enabled: bool
    channels: list[str]          # channel names contributed
    skill_dirs: list[Path]
    cli_entry: str | None
    status: Literal["ok", "missing", "error"]
    error: str | None            # import traceback summary when status != ok
```

`discover_plugins()` returns these for every record in `plugins.yaml` plus
every entry-point plugin. It **never raises** — a plugin whose path is gone
comes back `status="missing"`, one that fails to import comes back
`status="error"` with the message, and both still render in the UI. That's the
fix for silent-disappearance.

`runtime.start()` then builds receivers from
`discover_plugins()` filtered to `enabled and status == "ok"` and intersected
with the `CHANNELS` override if set. A disabled or broken plugin is logged once
at startup, not fatal.

`skills.skill_plugin_dirs()` gains the manifest-declared `skills.plugin_dir`
entries from enabled plugins, alongside the bundled dir and the existing entry
points. A disabled plugin contributes no skills — so toggling a plugin off also
stops advertising its skills to spawned sessions, which is the behaviour a user
toggling it off expects.

### REST API

```
GET    /api/plugins             → PluginDescriptor[]
POST   /api/plugins             {path} → register a local dir, disabled by default
PATCH  /api/plugins/{name}      {enabled: bool}
DELETE /api/plugins/{name}      → drop from plugins.yaml (builtins rejected, 400)
POST   /api/daemon/restart      → detached restart, 202 Accepted
```

Every mutation is a `plugins.yaml` edit and takes effect **on next daemon
start** — receivers own sockets and background threads, and hot-swapping them is
a much larger change than this plan. So each mutating response carries
`restart_required: true`, and the UI shows a persistent banner until a restart
happens. Being explicit about this beats a half-working hot reload.

### Restart button

`POST /api/daemon/restart` must not kill its own caller — the web server runs
*inside* the daemon. It reuses the mechanism the `yuki-conductor-restart` skill
already relies on: spawn `restart-daemon.ps1` **detached** via WMI
`Win32_Process.Create` (macOS: `launchctl kickstart -k` in a detached child),
return 202 immediately, then let the frontend poll `GET /api/status` until the
socket comes back.

That script currently lives under `.claude/skills/yuki-conductor-restart/`.
It moves to `bin/restart-daemon.ps1`, with the skill invoking the moved copy —
one script, two callers, rather than a copy that drifts. The macOS path grows
the equivalent in `daemon_macos.py`.

The frontend restart flow: button → confirm → 202 → "Restarting…" spinner →
poll `/api/status` every 2s with a 90s ceiling → success, or "Restart may have
failed, check `daemon.log`". It must tolerate the WebSocket dropping mid-flight,
since that's the expected case.

## 3. Web UI: 4 tabs plus "More"

Today `App.tsx` renders five peer tabs: Chat, Projects, Automations, Sessions,
Status. Adding Plugins makes six, which is past what a phone tab bar holds.

New layout — four primary tabs, last one is an overflow:

```
[ Chat ]  [ Projects ]  [ Automations ]  [ ⋯ More ]
```

Sessions, Status, and Plugins move behind **More**, which renders a simple list
view of the three, each pushing its existing component as a detail pane. The
"…" tab shows as active whenever any of its children is the current tab.

Routing implications in `App.tsx`:

- `Tab` widens to include `"plugins"` and `"more"`.
- `parseHash()` keeps accepting `#sessions`, `#status`, `#plugins` directly —
  deep links stay stable, and the More tab just renders highlighted. The
  localStorage `yuki-tab` key keeps working unchanged.
- The tab-bar array becomes data-driven (`{id, label, icon}[]`) instead of five
  copy-pasted `<button>` blocks, since the overflow list needs the same
  metadata the bar does.

`PluginsView.tsx` follows `AutomationsView.tsx`'s sidebar+detail shape:

- **List**: plugin name, description, a status dot (ok / disabled / missing /
  error), and badges for injection points (`channel`, `skill`, `cli`). Broken
  plugins render dimmed with the reason inline, mirroring how removed
  automations render today.
- **Detail**: source path, version, the channels and skills it contributes, an
  enable/disable toggle, a Remove button (hidden for builtins), and the import
  error in full when there is one.
- **Header**: an "Add plugin" field taking a local path, and the **Restart
  daemon** button. The restart-required banner lives here too.

## 4. Discovery by local path

For this pass, registration is: user types a directory path, we validate and
record it.

`POST /api/plugins {path}` validates, in order:

1. Path exists and is a directory (after `~` expansion).
2. It contains `yuki-plugin.yaml`.
3. The manifest parses and has a `name` not already registered.
4. Its declared `factory` modules are importable — **warn, don't reject**. A
   plugin whose dependencies aren't installed yet is a real state worth
   recording, and it shows as `status="error"` until fixed.

A path-registered plugin still has to be importable at runtime, which means its
package must be on `sys.path`. Two supported ways, documented rather than
automated:

- `uv pip install -e <path>` into the daemon's venv (what a local dev plugin
  does today), or
- the manifest declares `python_path: ./src`, and `plugins.py` prepends that to
  `sys.path` before importing the factory.

The second is what makes "just point at a folder" genuinely work, so it ships
with this plan. It's scoped narrowly: only for enabled, path-registered
plugins, and only the dirs they declare.

Deliberately out of scope: installing dependencies, creating venvs, git-clone
from a URL, version resolution. If a plugin needs packages, the user installs
them; the registry reports the failure clearly until they do.

## Implementation phases

Each phase is independently mergeable and leaves the daemon working.

**Phase 1 — rename and manifest.**
`CHAT_APPS` → `CHANNELS`; `chat_apps()` → `channels()`; update `runtime.py`,
`cron_scheduler.py`, the workspace `.env`, CLAUDE.md, and
`docs/plans/chat-plugin-architecture.md` vocabulary. Add `plugins.py` with
`PluginDescriptor` and `discover_plugins()` reading entry points only. Slack
becomes a builtin descriptor; `_build_receiver`'s special case goes away.
Tests: `channels()` parsing, descriptor construction, Slack-as-builtin.

**Phase 2 — registry file.**
Add `plugin_config.py` + `plugins.yaml` + `plugins.example.yaml`. Wire
`discover_plugins()` to merge file records with entry points. `runtime.start()`
filters by enabled. Add manifest (`yuki-plugin.yaml`) parsing and `python_path`
injection. Tests: merge precedence, missing path → `status="missing"`,
bad import → `status="error"` and not fatal at startup.

**Phase 3 — API and restart.**
The five endpoints above. Move `restart-daemon.ps1` to `bin/` and add the
detached-spawn helper to `daemon_windows.py` / `daemon_macos.py`. Tests:
endpoint round-trips against a tmp workspace, builtin-delete rejection.
The restart endpoint itself is tested only for "spawns detached and returns
202" — actually restarting in CI isn't worth it.

**Phase 4 — frontend.**
Data-driven tab bar, More overflow, `PluginsView.tsx`, restart flow with
polling. Rebuild with `uv run yuki-conductor web rebuild`.

**Phase 5 — docs.**
CLAUDE.md gains a "Plugins" section replacing "Chat Apps"; the skill-discovery
section notes that disabled plugins contribute no skills.

## Risks

| Risk | Mitigation |
| --- | --- |
| Restart endpoint kills the daemon and nothing comes back | Detached spawn already proven by the restart skill; `.stop`-file guard keeps the supervisor from racing; frontend surfaces a timeout with a pointer to `daemon.log` |
| `python_path` injection lets a plugin shadow a stdlib or first-party module | Append rather than prepend relative to the existing `sys.path` head, and log every injected path at startup |
| A plugin's import runs arbitrary code at discovery time | Discovery reads manifests without importing; imports happen only for **enabled** plugins, at startup, which is the same trust level as `uv pip install` today |
| Registry and `CHANNELS` disagree and the user can't tell which won | `runtime.start()` logs the resolved channel list and its source on every boot; the Plugins view shows "overridden by CHANNELS" when the env var is set |
| Renaming `CHAT_APPS` breaks the live workspace `.env` | Single-user project, no compat shim wanted — the `.env` is edited in the same change as the code |
