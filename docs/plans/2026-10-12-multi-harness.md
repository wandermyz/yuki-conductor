# Multi-Harness Support

> Status: **pi implemented (2026-10-13); dsh and OpenCode not started.**
> Phases 1–3 are done for pi, plus most of phase 6: a harness toggle in the
> new-chat picker, a badge, and Slack's per-channel `/yuki-harness`. Phase 7's
> docs are done too. Not yet done: per-harness models (`/yuki-model` is still
> Claude-only, and its pin is passed to Claude Code turns only), harness on
> the Status and Automations tabs, and the `CLAUDE_TIMEOUT` → `AGENT_TIMEOUT`
> rename. Written 2026-10-12; updated 2026-10-13 when pi was installed and
> GPU sharing moved out of yuki-conductor (see "GPU sharing is not
> yuki-conductor's job").

## Goal

Every spawned agent run today is Claude Code (`claude -p`). Add support for more
agent harnesses while keeping live intermediate progress (the web step list,
the Slack shimmering status):

- **Pi**: `pi --mode json`, <https://github.com/earendil-works/pi>. This is
  the recommended harness for **Strata**, the local Qwen3.8-Flash-Next server on
  `127.0.0.1:2335`, and the first new harness to build.
- **OpenCode**: `opencode run`
- **DeepSeek Harness (`dsh`)**: <https://github.com/deepseek-ai/deepseek-harness>

## Decisions

| Question | Decision |
| --- | --- |
| What is "DeepSeek Harness"? | The `dsh` agent harness from deepseek-ai, not "DeepSeek models inside another harness". |
| Granularity | **Per conversation.** Chosen when the conversation starts and never switched halfway, because session ids only work with the harness that created them. |
| Run mode | **One process per turn**, the same shape as today. Long-lived server modes are a later optimization. |
| Skills | **Deferred.** The skill listing and plugin-dir injection stay Claude-only for now (see "Out of scope"). |
| Tokens / cost | Best effort. `None` is fine when a harness doesn't report them. |
| Models | **Per harness.** Each harness has its own alias table and default. |
| Windows | Leave alone. Only Claude Code keeps the `.ps1` / Git Bash launch shims. The new harnesses are Unix-only. |
| GPU awareness | **None.** yuki-conductor never checks or switches the GPU. A run that can't get it fails fast (~15 s) with a friendly "GPU busy" reply. |

## Where Claude Code is wired in today

| Coupling | Location |
| --- | --- |
| Process start, flags, Windows shims, cancel registry, idle watchdog, stderr drain | `claude_runner.py` |
| stream-json → `StreamEvent` | `stream_events.py` (`parse_stream_line`) |
| Session resume (`-r`), final `result` record → `ClaudeResult` | `claude_runner._build_result` |
| Skill injection (`--plugin-dir`, `--setting-sources`, `--append-system-prompt-file`) | `claude_runner.run_claude` + `skills.py` |
| Model aliases and default | `store.MODEL_ALIASES`, `store.DEFAULT_MODEL_ARG`, `config.CLAUDE_DEFAULT_MODEL` |
| Callers of `run_claude` | `messaging/conversation.py`, `cron_scheduler.py`, `slack_app.py` (the `/yuki-<skill>` catch-all, which maps to Claude's `/<skill>`), `cli.py` (simulate) |
| Cancel | `web_server.py` → `claude_runner.cancel_process` |

Callers only ever see `ClaudeResult` and the generic
`StreamEvent(kind, label, detail)`. Web and Slack never read Claude's raw
stream-json output, so **the progress surfaces don't need to change.** Each
harness only has to produce the same `StreamEvent` kinds.

## Harness comparison (per-turn mode)

**Pi 1.2.0 is installed and verified** against Strata (2026-10-12/13): its
column below was confirmed with real headless runs. `opencode` and `dsh` are
**not installed**, so their columns come from docs and source only (the dsh repo
was read at commit `d7432673`, 2026-10-09). Rows marked ⚠ must be confirmed
against a recorded real run before writing the parser.

| | Claude Code | Pi | OpenCode | dsh |
| --- | --- | --- | --- | --- |
| One prompt | `claude -p … "prompt"` | `pi --mode json … "prompt"` | `opencode run --format json "prompt"` | `dsh --profile headless --json "prompt"` (prompt via arg, or stdin when omitted / `-`) |
| Output | NDJSON `system` / `assistant` / `user` / `result` | strict JSONL (split on LF only), see below | "raw JSON events": ⚠ schema undocumented | NDJSON, see below |
| Resume | `-r <id>` | `--session-id <id>`: opens the id, or **creates it if absent** | `-s <sessionID>` | `--session-id <id>` (same cwd required; can't be a subagent or fork) |
| New session id | `result.session_id` | chosen by us up front (or the first line `{"type":"session","id"}`, a UUID) | ⚠ unknown, check a recorded run | first line `{"type":"session","sessionId","cwd"}`; format `session-<uuid>` |
| Model | `--model opus[1m]` | `--model <provider/id>` + `--thinking off…max`; custom providers in `<agentDir>/models.json` | `-m provider/model` (+ `--variant` for reasoning effort) | ⚠ no headless flag found; set via a `--patch` overlay (`agent-default-model {provider, model}`), default `deepseek-official` / `deepseek-flash` |
| Skip permissions | `--dangerously-skip-permissions` | nothing needed: **pi never asks for approval**. Pass `--approve` / `--no-approve` explicitly for project-trust files | `--auto` | `DSH_PERMISSION_MODE=danger-full-access` ⚠ (its approval policy `never` *rejects* requests; confirm that tools don't ask in full-access mode) |
| Extra system prompt | `--append-system-prompt-file` | `--append-system-prompt <text\|path>` (repeatable) | ⚠ no flag; use an instructions file or prefix the prompt | `personaSuffix` of the `system-prompt` plugin, via `--patch file.yml` |
| Skills | `--plugin-dir` + listing we synthesize | native `SKILL.md`, listed in the system prompt even headless; `--skill <path>` (repeatable) adds dirs. Does **not** read `~/.claude/skills` on its own | ⚠ | native `SKILL.md` via `customSkillDirs` |
| Working dir | process cwd | process cwd (sessions grouped per cwd) | process cwd, or `--dir` | process cwd |
| Tokens / cost | both | both: `usage {input, output, cacheRead, cacheWrite, cost.total}` on each assistant `message_end` | ⚠ unknown | tokens in `status` `step_end` (`usage`), no cost |
| Text granularity | per message (`--include-partial-messages` exists, unused) | **token deltas** (`text_delta`, `thinking_delta`, `toolcall_delta`) | ⚠ unknown | per committed step (headless has no token-by-token output) |
| Auth / env | `ANTHROPIC_*` | `models.json` `apiKey` (any dummy value for Strata); `PI_CODING_AGENT_DIR` picks the whole config dir | provider config | `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DSH_HOME` (default `~/.dsh`) |
| Exit code | 0 on success | 0 even when the model errors in JSON mode: read `stopReason` instead | ⚠ | 0 only when the final `turn/end` completed, else 1 |

### Pi `--mode json` events

Sources: `packages/coding-agent/docs/json.md`, `rpc.md`, `message-types.md`,
and `src/modes/print-mode.ts`.

```
{"type":"session", version:3, id, timestamp, cwd, parentSession?}     # first line
{"type":"agent_start"} / {"type":"turn_start"}
{"type":"message_start", message}
{"type":"message_update", usage, assistantMessageEvent:{type, ...}}
    type: text_start | text_delta{contentIndex, delta} | text_end{contentIndex, content}
          thinking_start | thinking_delta | thinking_end
          toolcall_start{id, toolName} | toolcall_delta{delta} | toolcall_end{toolCall}
          error{reason, error}
{"type":"message_end", message}          # authoritative; assistant: usage, stopReason, errorMessage?
{"type":"tool_execution_start", toolCallId, toolName, args}
{"type":"tool_execution_update", toolCallId, partialResult}
{"type":"tool_execution_end", toolCallId, toolName, result:{content, details}, isError, durationMs}
{"type":"turn_end", message, toolResults}
{"type":"agent_end", messages, willRetry}
{"type":"agent_settled", aborted}        # the real "done"
{"type":"auto_retry_start"|"auto_retry_end"|"compaction_start"|"compaction_end", ...}
```

`stopReason` is one of `stop | length | toolUse | error | aborted | deferred`.

Mapping onto `StreamEvent`:

- `session` → `init`.
- Assistant `message_end` content blocks → `text`, `thinking` and `tool_use`.
  Using `message_end` instead of the deltas keeps the same per-message
  granularity as Claude.
- `tool_execution_end` → `tool_result`, using `isError`.
- `compaction_start` / `auto_retry_start` → `init`-style status labels
  ("Compacting…", "Retrying…").
- Final text: the last assistant `message_end` with `stopReason == "stop"`.
- Error: `stopReason` is `error` or `length`. Report `errorMessage`.
- Tokens and cost: summed over the assistant `message_end.usage` records.

The deltas are what a future `text_delta` kind would use. Pi is the only
harness here that streams token by token headlessly.

### Pi setup (done, machine config)

Pi 1.2.0 is installed in `~/.local` (`npm install -g --ignore-scripts
--min-release-age=0 --prefix ~/.local @earendil-works/pi-coding-agent@1.2.0`,
the same flags as pi's own installer). Its config lives in its agent dir,
`~/.pi/agent/`:

- `models.json` defines provider `strata`:
  - endpoint `http://127.0.0.1:2335/v1`, OpenAI-compatible, dummy key;
  - model `qwen3.8-flash-next-iq2_xs`, 131,072-token context, 32,768 max output;
  - a `thinkingLevelMap` from pi's levels down to Strata's
    `none|low|medium|high`.
- `settings.json` makes it the default model, at thinking `medium`.

yuki-conductor does **not** write or depend on these files: they're personal
machine config, like LiteLLM's. `PiHarness` passes `--model` explicitly, and
`available()` only checks that `pi` is on PATH.

Behaviour verified in real headless runs (2026-10-12):

- thinking, tool calls, tool results and the final text all stream;
- `--session-id` creates-or-resumes, and prints a "creating a new session"
  warning on stderr when it creates one;
- `usage.cacheRead` shows Strata's prefix cache reused across turns;
- on a 503, pi auto-retries 3× (2/4/8 s), then ends with
  `stopReason: "error"`, and **still exits 0**;
- skills in `~/.pi/agent/skills/` are listed in `<available_skills>`, headless
  included.

Bash tool behaviour, verified 2026-10-13 with a fake model server (no Strata,
no GPU):

- **Output streams live.** While a command runs, `--mode json` emits a
  `tool_execution_update` per output chunk, throttled to at most one per 100 ms.
  Its `partialResult.content[0].text` holds the **cumulative** output so far,
  not a delta (6 ticks 2 s apart arrived as 6 updates 2 s apart).
- **No default timeout.** The model may pass an optional `timeout` (seconds);
  otherwise a command runs until it exits. A 150 s silent `sleep` completed.
- **A silent command emits nothing until it ends.** A long render whose script
  prints nothing produces no events, so yuki-conductor's idle watchdog would see
  silence. The default `CLAUDE_TIMEOUT` is 1800 s, so this only matters for
  renders over 30 min.

Sessions: we pass `--session-id <our id>` on **every** turn, so the id is
known before the first run and there is no first-line parsing race. Pi creates
the session if it's missing, and resumes it otherwise. Resuming also restores
the session's model and thinking level, which suits a model locked per
conversation. The session files live in pi's own store (`~/.pi/agent/sessions/`),
not in our workspace.

### Other pi surfaces (not used now)

- `pi --mode rpc`: JSONL commands on stdin (`prompt`, `abort`, `steer`,
  `follow_up`, `get_state`, `set_model`, `compact`, …) and the same events
  on stdout. It's the warm-process option, and `abort` gives a graceful cancel
  instead of a kill.
- **Extensions** (TypeScript, loaded with `-e <path>` or from
  `<agentDir>/extensions/`) can hook `before_agent_start` (to change the system
  prompt), `tool_call` and `message_end`, and can `registerTool`. A future
  `yuki-conductor` pi extension could register `send` and attachment tools
  natively instead of relying on bash plus the prompt.

### dsh `--json` event lines

Source: `packages/bundle/headless/src/json-stream.ts`.

```
{"type":"session", sessionId, cwd}
{"type":"status", phase:"turn_start"|"step_start"|"step_end"|"turn_end", turn, step?, usage?, reason?}
{"type":"text", text}
{"type":"thinking", text}
{"type":"tool_call", callId, tool, input}
{"type":"tool_result", callId, status:"completed"|"error", result}
{"type":"final", text}
{"type":"error", message}            # replaces `final` for failures outside a turn
```

Strings are capped at 8 KiB and lines at 32 KiB, with `truncated:true` when
cut. This maps almost one-to-one onto `StreamEvent`:

- `session` → `init`
- `text`, `thinking`, `tool_call` → `tool_use`, `tool_result` → same kinds
- `final` → `RunResult.text`
- `step_end.usage` summed → tokens

### Other dsh surfaces (not used now)

- `dsh --profile sdk`: stdio JSON-RPC 2.0. Methods `initialize`,
  `session/prompt`, `session/wait`, notifications `session.event` /
  `session.status`. There is no cancel method: kill the process instead. A
  Python client ships in the repo under `python/sdk`.
- `dsh --profile acp`: ACP protocol, with `session/cancel` and
  `session/request_permission`.
- `dsh --profile web`: an internal typed RPC on :3080. Avoid it.
- dsh **does** support `SKILL.md` skills, from `.dsh/skills`, `.agents/skills`,
  `$DSH_HOME/skills`, `~/.agents/skills`, and `customSkillDirs`. It reads
  `AGENTS.md` and `CLAUDE.md` as instruction files. That matters for the
  deferred skills work.

### Claude Code "serve"?

There is no HTTP server mode (checked against v2.1.296). The warm-process
equivalent is
`claude -p --input-format stream-json --output-format stream-json`: one
long-lived process, user messages written to stdin. That's what the Agent SDK
uses. Not needed while we run one process per turn.

## GPU sharing is not yuki-conductor's job

Strata and ComfyUI share one 16 GB GPU. That is handled entirely by
`gpu-workload` (wandermyz/wandering-vibe, `gpu-workload/`). Each side takes
the GPU for itself:

| Wants the GPU | Who makes room |
| --- | --- |
| Strata (pi, curl, anything) | Strata's `before_load` hook: frees an idle ComfyUI, refuses while ComfyUI has jobs |
| ComfyUI (scripts, its web page) | the ComfyUI guard node: lets a Strata answer finish, then unloads Strata |

So `PiHarness` never checks or switches the GPU. It has exactly two related
duties, and neither needs any knowledge of the GPU:

1. **Read errors from `stopReason`, not the exit code** (pi exits 0 on failure).
2. **Turn a "GPU busy" failure into a readable reply.**
   - When ComfyUI is rendering, Strata answers 503 with a `before_load command
     failed` message. Pi retries for ~15 s and gives up.
   - The adapter recognises that message in the final `errorMessage` and replies
     "Strata is unavailable right now: the GPU is busy with ComfyUI jobs. Try
     again in a few minutes."
   - **Decision (2026-10-13): fail fast.** Pi's default retry settings stay as
     they are; we don't stretch them to wait out a render.
   - Other errors are passed through as-is.

The pi-side batching hint (`comfyui-batching` skill) also lives in
gpu-workload, not here.

## Architecture

```
src/yuki_conductor/harness/
  __init__.py     — HARNESSES registry, get_harness(name), run_agent()
  base.py         — Harness / StreamParser protocols, RunRequest, Launch, RunResult, StreamEvent
  process.py      — Popen + idle watchdog + stderr drain + cancel registry + kill tree
  claude_code.py  — ClaudeCodeHarness: today's flags, Windows shims, stream-json parser, model aliases
  pi.py           — PiHarness
  opencode.py     — OpenCodeHarness
  dsh.py          — DshHarness
```

`claude_runner.py` and `stream_events.py` are deleted. Their contents move into
`process.py`, `base.py` and `claude_code.py`. No re-exports, per house style.

### Interfaces

```python
@dataclass
class RunRequest:
    prompt: str
    session_id: str | None
    model: str              # already resolved to the harness-native id
    cwd: str
    system_prompt: str      # harness-neutral text; the harness decides how to deliver it
    env: dict[str, str]     # base env (CLAUDECODE stripped, YUKI_CONDUCTOR_PROJECT set)

@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str]
    stdin: str | None = None
    temp_files: list[str] = field(default_factory=list)   # removed after the run

@dataclass
class RunResult:
    text: str
    session_id: str | None
    harness: str
    is_error: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None

class StreamParser(Protocol):
    def feed(self, line: str) -> list[StreamEvent]: ...
    def finish(self, stdout: str, stderr: str, returncode: int,
               fallback_session_id: str | None) -> RunResult: ...

class Harness(Protocol):
    name: str                    # "claude-code" | "pi" | "opencode" | "dsh"
    label: str                   # UI display name
    default_model: str           # alias key into `models`
    models: dict[str, str]       # alias -> native model id
    def launch(self, req: RunRequest) -> Launch: ...
    def new_parser(self) -> StreamParser: ...   # stateful, one per run
    def available(self) -> bool: ...            # binary on PATH; UI greys out otherwise
```

The parser is stateful so an adapter can hold the session id from an early
line, sum usage across steps, or put together tool-call fragments. The Claude
parser can stay effectively stateless.

### `run_agent`

```python
def run_agent(prompt, *, harness: str, session_id=None, model=None, cwd=None,
              conversation_key=None, push_conversation_id=None,
              on_event=None, timeout=None) -> RunResult
```

1. Look up the harness by name. Resolve the model: the alias goes through
   `h.models`, and None becomes `h.default_model`.
2. Build the system prompt (see the next section) and the base env.
3. `launch = h.launch(req)`, then `process.run(launch, cwd, timeout,
   conversation_key, on_line=parser.feed → on_event)`.
4. Handle the cancel and timeout cases generically, the same way as today:
   "(stopped by user)" and the idle-timeout message.
5. Otherwise return `parser.finish(...)`, and clean up `launch.temp_files` in
   `finally`.

`process.py` keeps the `register_process` / `cancel_process` / `was_cancelled`
API keyed by `conversation_key`, so `web_server`'s cancel endpoint only changes
its import.

`StreamEvent` keeps its current kinds (`init | text | thinking | tool_use |
tool_result`) and only changes its home module. A future `text_delta` kind for
token streaming can be added later. Nothing needs it yet.

### System prompt split

`skills.system_prompt()` currently returns one blob. Split it into:

- **`core_prompt(conversation_key)`**: the `_PROMPT_HEADER` content that every
  harness needs. That's the `<attachment>` tag docs, the "Your conversation"
  section for `yuki-conductor send`, and the workspace `system-prompt.md`.
  Without it, file replies and pushes break on new harnesses.
- **`skills_prompt(plugin_dirs, cwd)`**: the tiered skill listing. Only
  `ClaudeCodeHarness` uses it for now.

Each harness delivers the text its own way:

- Claude Code: `--append-system-prompt-file`.
- Pi: `--append-system-prompt <tempfile>` (it takes text or a file path). Pi
  already lists skills natively, so this gets `core_prompt` only, never
  `skills_prompt`.
- dsh: a temp `--patch` yml setting `personaSuffix`, which also carries the
  model.
- OpenCode: whatever the recorded run and docs show works. If nothing does,
  prefix the user prompt.

### Persistence

No migrations, per house style. Change the schemas in place:

- `sessions` (Slack, `store.SessionStore`) and `conversations`
  (`conversation_store.ConversationStore`) get a `harness TEXT NOT NULL DEFAULT
  'claude-code'` column. It's written on the first turn and **never updated
  afterwards**.
- `platform.get_session_id` → `get_session(conversation_key) -> (session_id,
  harness) | None`, and `set_session_id` gains `harness`.
- `ModelStore`'s key becomes `(scope, harness)`. `/yuki-model` lists the
  current harness's aliases. `MODEL_ALIASES` and `DEFAULT_MODEL_ARG` move into
  `claude_code.py` (its `models` and `default_model`), and
  `CLAUDE_DEFAULT_MODEL` stays a Claude-only config.

### Choosing the harness

Resolved once, in `handle_incoming_message` / `cron_scheduler._execute`, highest
first:

1. The **stored harness** when resuming an existing conversation. This always
   wins.
2. The explicit choice for a new conversation:
   - web: a picker in new-chat, sent as `harness` in the chat POST body;
   - cron: a `harness:` key per task in `cron.yaml`, added to
     `cron_config.EDITABLE_FIELDS`;
   - Slack: the channel default set by `/yuki-harness`, which applies to new
     threads only.
3. Project default: an optional `harness` on `ProjectStore` rows. Nice to have
   and can slip.
4. The first harness in `HARNESSES` (`.env`, default `claude-code`), which also decides which harnesses exist on the machine at all.

`IncomingMessage` gets `harness: str | None` next to `model`.

### Config

Add to `config.py`:

| Setting | Default |
| --- | --- |
| `HARNESSES` | `claude-code` (comma-separated, first = default; `claude-code,pi` on the Strata machine) |
| `PI_BIN` | `pi` |
| `PI_DEFAULT_MODEL` | `strata/qwen3.8-flash-next-iq2_xs` |
| `OPENCODE_BIN` | `opencode` |
| `OPENCODE_DEFAULT_MODEL` | — |
| `DSH_BIN` | `dsh` |
| `DSH_DEFAULT_MODEL` | — |

`CLAUDE_TIMEOUT` becomes `AGENT_TIMEOUT` and applies to every harness. Rename
all call sites and `.env` in the same change. `DEEPSEEK_API_KEY` / `DSH_HOME`
are read by dsh itself from the env and go in `~/.yuki-conductor/.env`.

### UI

- Web chat: a harness picker on new conversations only, then a harness badge
  in the conversation header and the sessions list. The model picker filters to
  the active harness.
- Status tab: each harness with `available()` and its version.
- Automations: show `harness` in the detail pane; editable like
  `display_name`.
- Slack: `/yuki-harness [claude-code|pi|opencode|dsh]`. It must be registered
  before the `/yuki-.+` catch-all in `slack_app.py`, the same as `/yuki-model`.
  The catch-all itself forwards `/<skill>` slash syntax, so it stays pinned to
  `claude-code`.

A plugin injection point for harnesses (`harnesses`) is **not** part of this
plan. A hard-coded registry of four is enough. Revisit if a fifth harness shows
up.

## Implementation phases

Each phase ends with `uv run pytest` and `uv run ruff check .` green.

1. **Refactor, no behaviour change.**
   - Create `harness/` and move the code out of `claude_runner.py` and
     `stream_events.py` into `process.py`, `base.py` and `claude_code.py`.
   - Rename `run_claude` → `run_agent(harness="claude-code")` and
     `ClaudeResult` → `RunResult`, updating every caller.
   - Move the existing tests (`test_claude_runner.py` → `test_harness_process.py`
     + `test_harness_claude_code.py`).
2. **Storage and selection.**
   - Add the `harness` columns and the `get_session` / `set_session_id`
     signature change.
   - Implement the selection order, `HARNESSES`, per-harness models, and
     the `core_prompt` / `skills_prompt` split.
3. **Pi adapter, targeting Strata.** This comes first among the new harnesses
   because it's the Strata recommendation and its JSON schema is fully
   documented. Install and config are already done (see "Pi setup").
   - Record `--mode json` fixtures into `tests/fixtures/harness/pi/*.jsonl`.
     Strata wakes on demand, so no manual start is needed. Record:
     - a plain turn;
     - a tool-using turn;
     - a resume with `--session-id`;
     - a **GPU-busy failure** (queue a ComfyUI job, then run pi);
     - a cancel.
   - Write `PiHarness`, its parser and tests, including the "GPU busy" reply
     mapping.
   - Map `tool_execution_update` to a progress step. The parser keeps the last
     `partialResult` text per `toolCallId` and shows its last line, so a render
     printing progress appears live in web and Slack.
4. **dsh adapter.**
   - Install `dsh`, set `DEEPSEEK_API_KEY`, and record real `--json` runs into
     `tests/fixtures/harness/dsh/*.jsonl`. Record a plain turn, a tool-using
     turn, a resume, an error and a cancel.
   - Confirm the ⚠ rows: model override via `--patch`, `danger-full-access`
     behaviour, and `personaSuffix` delivery.
   - Write the parser and fixture tests.
5. **OpenCode adapter.**
   - Install `opencode` and record `--format json` fixtures in the same way.
   - Write down the event schema in this doc, then write the parser and tests.
6. **UI.** Web harness picker and badge, model picker filtered by harness,
   `/yuki-harness`, cron `harness:`, harness list on the Status tab.
   Rebuild with `uv run yuki-conductor web rebuild`.
7. **Docs.** Update the `CLAUDE.md` Project Structure, Architecture (Claude
   invocation line) and Skill Discovery notes ("Claude Code only").

## Testing

- `process.py`: one generic test set using a fake child process (a Python
  script emitting lines and sleeping) covers the watchdog, cancel,
  non-zero exit and OSError on launch for every harness.
- Each adapter:
  - `launch()` argv/env snapshot tests for new vs resume and model override;
  - fixture-driven parser tests (recorded `.jsonl` → expected `StreamEvent`s +
    `RunResult`).
- Pi specifically: the recorded GPU-busy run yields `is_error=True` and the
  friendly reply, even though pi exited 0.
- Selection tests:
  - a resumed conversation ignores a changed default;
  - a cron `harness:` key is respected;
  - an unavailable harness gives a clear error reply, not a crash.

## Out of scope / later

- **Skills for non-Claude harnesses.** Options when we get to it:
  - list the skills in the prompt only;
  - point dsh's `customSkillDirs` at our plugin dirs and `~/.claude/skills`
    (dsh reads `SKILL.md` natively);
  - for pi, pass `--skill <dir>` for each plugin dir's `skills/` and for
    `~/.claude/skills`, plus the project's `.claude/skills`. Pi lists them
    itself, so this is the cheapest of the three and could even ship with
    phase 3. Watch prompt size on a local model;
  - an OpenCode equivalent.

  The `yuki-conductor send` / `<attachment>` contract already works with any
  harness that can run shell commands once `core_prompt` is delivered.
- **Warm processes:** `pi --mode rpc`, `opencode serve` + `run --attach`,
  `dsh --profile sdk`, and Claude's stdin stream-json mode.
- **Token-level text streaming** (`text_delta`): pi's `text_delta` events,
  Claude `--include-partial-messages`, plus whatever OpenCode emits. dsh
  headless can't do it.
- **A yuki-conductor pi extension** that registers `send` / attachment tools
  natively.
- Windows support for the new harnesses.
- A `harnesses` plugin injection point.

## Open risks

- **Strata latency.**
  - Strata serves **one request at a time**, so parallel conversations and
    cron runs queue behind each other.
  - A cold wake costs ~30 s, and a GPU handover to and from ComfyUI also loses
    its prefix cache. After that, the whole conversation is re-read at about a
    minute per 30K tokens. Keep pi's prompt lean, which is one reason skills
    stay deferred.
- **"GPU busy" depends on the error text.** The adapter recognises the busy
  case from Strata's `before_load command failed` message. If Strata rewords
  it, the reply falls back to the raw error: still correct, just less friendly.
  The recorded fixture pins it.
- **Silent long tool calls.** pi emits no events while a command prints
  nothing, so a render over 30 min with a quiet script would trip the idle
  watchdog. The `comfyui-batching` skill asks for short output only, so it
  doesn't prevent this. If it bites, either add an occasional progress line to
  the skill's advice, or raise `AGENT_TIMEOUT` for pi.
- **Pi JSON-mode exit code is 0 on model errors.** The parser must derive
  `is_error` from `stopReason`, not from `returncode`.
- **Pi sessions are grouped by cwd.** The same constraint as dsh: a
  conversation's cwd must stay stable.

- **dsh is a developer preview** with "compatibility-breaking changes" promised.
  Pin a version, and keep the parser tolerant of unknown lines, the same way
  `parse_stream_line` already is.
- **dsh resume needs the same cwd.** A conversation's cwd must be stable, which
  is already true per project but worth asserting.
- **dsh permissions.** If `danger-full-access` still routes some tools through
  an approval ask that `never` rejects, headless runs will silently refuse
  actions. Fallback: the ACP profile with auto-answered
  `session/request_permission`, which means a different, non-NDJSON adapter.
- **OpenCode's JSON schema is undocumented.** The parser is built from recorded
  output, so re-record fixtures when bumping OpenCode.
