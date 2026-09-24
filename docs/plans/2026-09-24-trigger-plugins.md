# Trigger Plugins

## Problem

`channels` was the first injection point; `triggers` is the second. A **trigger**
is anything that decides a Claude run should happen *without a human typing a
message*. Today exactly one thing can do that — the cron scheduler — and it is
not a plugin. It's a module wired directly into `runtime.start()`, with its own
YAML file, its own SQLite table, its own web API, and its own tab.

That's fine while cron is the only trigger. It stops being fine the moment
there's a second one, because every piece of cron's machinery is generic except
the part that decides *when* to fire:

| Concern | Cron-specific? |
| --- | --- |
| Deciding it's time to fire | **yes** — croniter over a schedule |
| Prefixing a prompt with notify/silence instructions | no |
| Reserving a destination thread before the run | no |
| Running Claude and parsing `<notify>` / `<silence>` | no |
| Recording a run row, updating it on completion | no |
| Routing the notification to a platform | no |
| Rendering run history in a UI tab | no |

Six of seven are generic. Adding an email trigger by copying
`cron_scheduler.py` would duplicate all six, and the duplicates would drift.

So this design pulls the generic six into a **trigger host** owned by
yuki-conductor, and reduces a trigger plugin to the one thing that is actually
its own: deciding when to fire, and with what prompt.

## Scope

1. A `triggers` injection point, with a `TriggerSource` protocol.
2. **Cron becomes a bundled trigger plugin** (`plugins/cron/`), the same way
   Slack became a bundled channel plugin. No behaviour change visible to the
   user; the Automations tab keeps working.
3. **An email trigger plugin** (`plugins/email/`), backed by a long-running
   subprocess that emits events on stdout. This is the second implementation
   that proves the abstraction, and the reason the protocol is shaped the way
   it is.
4. Generalizing `cron_runs` → `trigger_runs`, and the Automations tab from
   "cron tasks" to "triggers".

Non-goals: a trigger *authoring* UI beyond what Automations already does,
trigger chaining (one trigger firing another), conditional/filtered triggers
beyond what a plugin implements itself, and replay of historical events.

## 1. The `TriggerSource` protocol

A trigger plugin contributes one or more **trigger sources**. A source is a
long-lived object that the host starts, and which calls back into the host when
something should run.

```python
@dataclass(frozen=True)
class TriggerEvent:
    """One decision that a Claude run should happen."""

    source: str              # trigger source name, e.g. "cron" or "email"
    key: str                 # stable id within the source: task name, rule name
    prompt: str              # what to ask Claude
    title: str | None = None # human label for the thread / history row
    chat_app: str | None = None   # explicit platform, else default routing
    context: dict | None = None   # free-form, recorded in history for debugging
    dedupe_key: str | None = None # see "Delivery semantics"


class TriggerSource(Protocol):
    """A source of TriggerEvents. Contributed at the `triggers` injection point."""

    name: str

    def start(self, emit: Callable[[TriggerEvent], None]) -> None:
        """Begin producing events. Must not block — spawn threads as needed.

        `emit` is the host's callback. It is thread-safe, returns immediately,
        and never raises into the caller: a failed run is the host's problem to
        record, not the source's problem to handle.
        """

    def stop(self) -> None:
        """Stop producing events and release resources."""

    def status(self) -> dict:
        """Health, same shape as ChatAppReceiver.status()."""

    def list_triggers(self) -> list[TriggerDescriptor]:
        """Everything this source could fire, for the Automations tab."""

    def fire_now(self, key: str) -> bool:
        """Fire one trigger immediately, off-schedule. Backs 'Run now'."""
```

`emit` is a push callback rather than a generator or a queue the host polls,
because the two real implementations have opposite shapes: cron wakes on a timer
it owns, while email blocks on a subprocess pipe. A push callback is the only
form that suits both without one of them running an awkward adapter loop.

### Why `start(emit)` and not `poll() -> list[TriggerEvent]`

A polled interface would force the email source to buffer events between polls
and would put an arbitrary floor on latency. It would also make the
long-running-command case — where the process *is* the event loop — fight the
host's loop. Push has one real cost, addressed below: the host must be
re-entrant, since two sources can emit concurrently.

## 2. The trigger host

`trigger_host.py` owns everything currently generic inside `cron_scheduler.py`:

```python
def handle_trigger_event(
    event: TriggerEvent,
    platforms_by_name: dict[str, MessagingPlatform],
    trigger: str = "event",
) -> None:
    """Run Claude for one TriggerEvent and route the result. Never raises."""
```

The body is today's `_run_cron_task` + `_execute`, with `CronTask` replaced by
`TriggerEvent`:

1. Insert a `running` row in `trigger_runs`.
2. Pick a platform (`event.chat_app`, else enabled-channel order, else `web`).
3. `reserve_thread()` where supported, so the run can post interim messages.
4. Prefix the prompt with the notify/silence instructions and `run_claude`.
5. Parse `<notify>` / `<silence>`; update the run row; discard the reserved
   thread if the run stayed silent.
6. Send, and persist the session id so replies in that thread continue it.

This is a **move, not a rewrite**. The notify/silence contract, the thread
reservation dance, and the run-history semantics are all behaviour users
already depend on; changing them here would be a second change wearing the same
commit.

One genuine addition: the host must be safe to call concurrently from several
sources. Today only the cron loop calls it. The run store already serializes on
its own lock, and each call spawns its own thread, so this is mostly a matter of
not introducing shared mutable state in the host — worth stating because it is
easy to break later.

### Prompt prefix ownership

`_CRON_PROMPT_PREFIX` moves to the host and loses the word "cron":

> You are running as an automated trigger. After completing your work, decide
> whether the user needs to be notified…

A source may prepend its own context (the email source describes the message
that fired it), but the notify/silence contract belongs to the host, since the
host is what parses the answer.

## 3. Registry and manifest

`triggers` joins `channels` in `yuki-plugin.yaml`:

```yaml
name: cron
description: Fire tasks on a schedule, from workspace/cron.yaml.
injection_points:
  triggers:
    - name: cron
      factory: yuki_conductor.cron_trigger:create_source
```

Everything `plugins.py` already does applies unchanged: discovery reads the
manifest without importing, a disabled plugin contributes no sources, a broken
one reports `status="error"` and doesn't take startup down, and `python_path`
makes a path-registered plugin importable.

Additions to `plugins.py`:

- `PluginDescriptor.triggers: list[Trigger]` alongside `.channels`
- `enabled_trigger_sources()`, mirroring `enabled_channels()`
- `find_trigger(name)`, mirroring `find_channel(name)`

The factory signature matches channels: `create_source(session_store=...,
model_store=...)`.

### Startup

`runtime.start()` gains a phase after channels are up:

```python
sources = start_trigger_sources(descriptors, platforms_by_name)
```

Ordering matters. Sources must start *after* channels, or a trigger that fires
in the first second has nowhere to deliver. A source that fails to start is
logged and skipped — same rule as a broken channel, for the same reason: one bad
plugin must not cost you the daemon.

## 4. Cron as a bundled trigger plugin

`plugins/cron/yuki-plugin.yaml`, with the implementation staying in-tree at
`src/yuki_conductor/cron_trigger.py`. It keeps `cron.yaml`, `cron_config.py`,
and the croniter loop; it loses `_run_cron_task`, `_execute`, `_pick_platform`,
and `_CRON_PROMPT_PREFIX` to the host.

```python
class CronSource:
    name = "cron"

    def start(self, emit):
        # today's _scheduler_loop, except the firing branch becomes:
        #   emit(TriggerEvent(source="cron", key=task.name,
        #                     prompt=task.prompt, title=task.label,
        #                     chat_app=task.chat_app))
```

`trigger_task()` becomes `fire_now(key)`. The 30-second reload of `cron.yaml`
stays — it's why edits take effect without a restart.

What deliberately does **not** change: `cron.yaml`'s format, the Automations
tab's behaviour, and the `yuki-conductor-cron` skill. A user should not be able
to tell this refactor happened.

## 5. Email as a subprocess-backed trigger plugin

The second implementation, and the one that justifies the protocol's shape.

`plugins/email/` wraps a **long-running command** that watches a mailbox and
writes one JSON object per line to stdout. yuki-conductor does not implement IMAP
or OAuth; it supervises a process and reads a pipe.

```yaml
name: email
description: Fire a Claude run when mail matching a rule arrives.
injection_points:
  triggers:
    - name: email
      factory: yuki_email_trigger:create_source
```

Configured by `workspace/email-triggers.yaml`:

```yaml
# The watcher process. Must emit one JSON object per line on stdout.
command: ["workiq", "watch", "--folder", "Inbox", "--json"]

rules:
  - name: build-failures
    from_contains: "ci@example.com"
    subject_matches: "(?i)build failed"
    prompt: >
      A CI failure email arrived. Read it, find the failing test,
      and summarize the likely cause.
  - name: from-my-manager
    from_contains: "manager@example.com"
    prompt: "Summarize this email and draft a reply for my review."
```

Each stdout line is an email event:

```json
{"id": "AAMk...", "from": "ci@example.com", "subject": "Build failed",
 "received_at": "2026-09-24T10:31:00Z", "preview": "3 tests failed..."}
```

The source matches it against the rules and, on the first match, emits a
`TriggerEvent` whose prompt is the rule's prompt plus the email's fields, with
`dedupe_key` set to the message id.

### The supervision problem

A long-running child process is the part that will actually break in
production, so the design is mostly about its failure modes:

- **It exits.** Restart with exponential backoff (1s → 60s cap). Surface the
  state in `status()` so the Plugins tab shows `error` with the exit code
  rather than the plugin looking healthy while nothing arrives.
- **It hangs.** No output is indistinguishable from an idle mailbox, so the
  protocol requires a **heartbeat line** (`{"type": "heartbeat"}`) at least
  every N seconds. Miss two and the source kills and restarts the child. Without
  this, a wedged watcher is silently equivalent to no watcher — the same
  silent-disappearance failure `discover_plugins()` was built to prevent.
- **It floods.** A misconfigured rule matching every message could spawn
  unbounded concurrent Claude runs. The source enforces `max_concurrent_runs`
  (default 3) and `min_seconds_between_runs` per rule, dropping with a logged
  warning rather than queueing without bound.
- **It emits garbage.** A line that isn't valid JSON is logged and skipped. One
  bad line must not kill the reader thread.
- **It replays on restart.** Covered below.

### Delivery semantics

Cron is idempotent by nature: miss a firing and the next one comes along. Email
is not — a restarting watcher re-reading the last N messages would re-run every
rule, which for a "draft a reply" prompt is user-visible damage.

So `TriggerEvent.dedupe_key` is checked against `trigger_runs` before the host
runs anything: if a run already exists for `(source, key, dedupe_key)`, the
event is dropped and logged. The email source sets it to the message id. Cron
leaves it `None` and opts out.

This is **at-most-once delivery for a given key**, chosen over at-least-once
because a duplicate LLM run costs money and can send real email, while a missed
one is recoverable by the next poll. Stated explicitly because it's the kind of
default that is painful to discover later.

## 6. Storage and UI

### `cron_runs` → `trigger_runs`

```sql
CREATE TABLE trigger_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,              -- new: "cron" | "email" | ...
  trigger_key TEXT NOT NULL,         -- was task_name
  dedupe_key TEXT,                   -- new
  trigger TEXT NOT NULL DEFAULT 'event',
  status TEXT NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  response TEXT, error TEXT,
  notified INTEGER NOT NULL DEFAULT 0,
  session_id TEXT, conversation_id TEXT,
  context TEXT                       -- new: JSON, why this fired
);
CREATE INDEX idx_trigger_runs_key ON trigger_runs (source, trigger_key, started_at DESC);
CREATE UNIQUE INDEX idx_trigger_runs_dedupe
  ON trigger_runs (source, trigger_key, dedupe_key) WHERE dedupe_key IS NOT NULL;
```

Per the repo's no-backward-compatibility rule, `cron_runs` is renamed in place
and the old table dropped. Existing history is migrated with a single
`INSERT INTO trigger_runs SELECT 'cron', task_name, NULL, … FROM cron_runs` —
history is worth one throwaway statement, but not a compatibility shim.

The `context` column is what makes a non-cron trigger debuggable: for email it
holds the sender, subject, and matched rule, so "why did this run?" is
answerable from the UI months later.

### Automations tab

Stays the tab it is; the sidebar gains a source badge (`cron`, `email`) and
groups by source when more than one is present. `/api/automations` unions
`list_triggers()` across enabled sources instead of reading `cron.yaml`
directly. The synthetic `active: false` entry for deleted-but-has-history
triggers still applies, keyed on `(source, key)`.

The detail pane gains a "Fired by" block rendering `context` — for cron, the
schedule; for email, the message that matched.

## Implementation phases

**Phase 1 — host extraction.** Add `TriggerEvent` + `trigger_host.py`; move the
generic six out of `cron_scheduler.py`; rename the table. Cron keeps calling the
host directly, still not a plugin. Nothing user-visible changes. Tests: the
existing cron routing/notify tests now exercise the host.

**Phase 2 — the injection point.** `triggers` in the manifest,
`PluginDescriptor.triggers`, `enabled_trigger_sources()`, and the startup phase.
Cron moves to `plugins/cron/` and is discovered through the registry.

**Phase 3 — API and UI.** `/api/automations` reads from sources; source badges,
grouping, and the "Fired by" block.

**Phase 4 — the email plugin.** `plugins/email/`, the subprocess supervisor,
rule matching, dedupe, backpressure, and heartbeat handling. A fake watcher
script in `examples/` makes this testable without a mailbox.

**Phase 5 — docs.** CLAUDE.md gains a Triggers section; the plugin-authoring
docs cover both injection points.

## Risks

| Risk | Mitigation |
| --- | --- |
| A wedged watcher looks healthy while firing nothing | Mandatory heartbeat; two missed beats restarts the child and `status()` reports it |
| A bad rule floods Claude runs (cost, and real emails sent) | `max_concurrent_runs` + per-rule rate limit, dropping with a warning rather than unbounded queueing |
| Duplicate runs after a watcher restart | `dedupe_key` enforced by a partial unique index, at-most-once per key |
| Extracting the host silently changes cron behaviour | Phase 1 is a pure move with no behaviour change; existing tests must pass untouched |
| Two sources emitting concurrently corrupt shared state | Host holds no mutable state; each event runs on its own thread; the run store already locks |
| A trigger fires before channels are up | Sources start after channels in `runtime.start()` |
| The email watcher becomes a de-facto yuki-conductor API | The contract is one JSON line per message plus a heartbeat — deliberately too thin to grow into one |

## Open questions

1. **Should `fire_now` bypass dedupe?** "Run now" on an email trigger with an
   already-seen id currently does nothing, which will read as a broken button.
   Leaning yes: manual firing is an explicit human act and should always run.
2. **Does the email source own rule matching, or does the host?** Kept in the
   source here, since matching is inherently source-shaped (headers, folders).
   Revisit if a third trigger wants the same predicate language.
3. **Should a trigger be able to target a specific existing conversation**
   rather than always opening a thread? Useful for "reply in the thread where
   this was discussed", but it needs a conversation-lookup API that doesn't
   exist yet. Out of scope for now.
