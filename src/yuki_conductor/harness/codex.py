"""Codex harness: `codex exec --json`, https://github.com/openai/codex.

Codex prints one JSON event per line. The events that matter here:

    thread.started {thread_id}             first line; the session id
    item.started / item.completed {item}   one item per step: agent_message,
                                           reasoning, command_execution,
                                           file_change, mcp_tool_call,
                                           web_search, todo_list, error
    turn.completed {usage}                 the turn finished; usage sums every
                                           model call of the turn
    turn.failed {error} / error {message}  the turn failed

A failed turn exits 1 with nothing useful on stderr, so the outcome is read
from the stream, as with pi. `turn.completed`'s usage is the turn's total, so
the context meter comes from the session's rollout file instead, whose last
`token_count` event carries the last call's usage and the model's window.
Shapes were checked against real runs; see tests/fixtures/harness/codex.
"""

import json
import logging
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

from yuki_conductor.config import CODEX_BIN, CODEX_DEFAULT_MODEL
from yuki_conductor.harness.base import (
    SUMMARY_LIMIT,
    Launch,
    ModelOption,
    RunRequest,
    RunResult,
    StreamEvent,
    prompt_tokens,
    tool_label,
    truncate,
)
from yuki_conductor.skills import codex_prompt, skill_plugin_dirs

logger = logging.getLogger(__name__)

# What `codex exec resume` says for an id it has no rollout for.
MISSING_SESSION_MARKER = "no rollout found for thread id"

# The prompt-side counts Codex reports for one model call. `input_tokens`
# already includes the cached part, unlike Claude Code's and pi's.
_CODEX_PROMPT_KEYS = ("input_tokens",)


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def resolve_bin(name: str) -> str:
    """The executable to spawn for `name`.

    On Windows npm installs `codex` as a `.cmd` shim, and cmd.exe mangles
    arguments with newlines or quotes in them, such as the system prompt. The
    real binary ships inside the npm package, so run that instead.
    """
    found = shutil.which(name)
    if not found or not found.lower().endswith(".cmd"):
        return found or name
    vendor = Path(found).parent / "node_modules" / "@openai" / "codex" / "node_modules"
    for exe in sorted(vendor.glob("@openai/codex-win32-*/vendor/*/bin/codex.exe")):
        return str(exe)
    return found


def model_catalog(bin_path: str) -> list[ModelOption]:
    """The models Codex itself lists, from `codex debug models`.

    Codex keeps a catalog of the models it knows how to drive; entries marked
    `hide` are internal and left out, as Codex's own picker does. It doesn't
    know which ones the configured provider actually serves.
    """
    try:
        proc = subprocess.run(
            [bin_path, "debug", "models"], capture_output=True, text=True,
            encoding="utf-8", stdin=subprocess.DEVNULL, timeout=15,
        )
        catalog = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        logger.warning("Couldn't read the Codex model catalog", exc_info=True)
        return []
    entries = catalog.get("models") if isinstance(catalog, dict) else None
    return [
        ModelOption(id=m["slug"], label=m.get("display_name") or m["slug"])
        for m in entries or []
        if isinstance(m, dict) and m.get("slug") and m.get("visibility") != "hide"
    ]


def configured_model() -> str | None:
    """The `model` in Codex's config.toml: what a turn gets with no `-m`."""
    try:
        with open(_codex_home() / "config.toml", "rb") as handle:
            model = tomllib.load(handle).get("model")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return model if isinstance(model, str) else None


def _toml_string(text: str) -> str:
    """`text` as a TOML basic string, for a `-c key=value` override."""
    # JSON string escapes are valid TOML basic-string escapes.
    return json.dumps(text, ensure_ascii=False)


def _item_events(item: dict, started: bool) -> list[StreamEvent]:
    itype = item.get("type")
    item_id = item.get("id") or ""
    if itype == "command_execution":
        command = (item.get("command") or "").strip()
        if started:
            return [StreamEvent(
                kind="tool_use",
                label=tool_label("Shell", command),
                detail={"tool_use_id": item_id, "name": "Shell", "summary": command},
            )]
        exit_code = item.get("exit_code")
        is_error = item.get("status") == "failed" or (
            isinstance(exit_code, int) and exit_code != 0
        )
        summary = truncate(item.get("aggregated_output") or "", SUMMARY_LIMIT)
        if is_error and not summary and exit_code is not None:
            summary = f"exit code {exit_code}"
        return [StreamEvent(
            kind="tool_result",
            label=("error: " + summary) if is_error else summary,
            detail={"tool_use_id": item_id, "is_error": is_error, "summary": summary},
        )]
    if started:
        return []
    if itype == "agent_message":
        text = (item.get("text") or "").strip()
        if not text:
            return []
        return [StreamEvent(kind="text", label=truncate(text, SUMMARY_LIMIT), detail={"text": text})]
    if itype == "reasoning":
        text = (item.get("text") or "").strip()
        if not text:
            return []
        return [StreamEvent(
            kind="thinking", label=truncate(text, SUMMARY_LIMIT), detail={"text": text}
        )]
    if itype == "file_change":
        paths = ", ".join(
            c.get("path", "") for c in item.get("changes") or [] if isinstance(c, dict)
        )
        return [StreamEvent(
            kind="tool_use",
            label=tool_label("Edit", paths),
            detail={"tool_use_id": item_id, "name": "Edit", "summary": paths},
        )]
    if itype == "mcp_tool_call":
        name = f"{item.get('server') or 'mcp'}.{item.get('tool') or 'tool'}"
        return [StreamEvent(
            kind="tool_use",
            label=name,
            detail={"tool_use_id": item_id, "name": name, "summary": ""},
        )]
    if itype == "web_search":
        query = item.get("query") or ""
        return [StreamEvent(
            kind="tool_use",
            label=tool_label("WebSearch", query),
            detail={"tool_use_id": item_id, "name": "WebSearch", "summary": query},
        )]
    if itype == "error":
        message = item.get("message") or ""
        return [StreamEvent(kind="status", label=truncate(message, SUMMARY_LIMIT), detail={})]
    return []


def _error_message(raw) -> str:
    """Codex wraps a provider's error body as a JSON string; dig out its message."""
    text = raw.get("message") if isinstance(raw, dict) else raw
    text = text if isinstance(text, str) else ""
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and isinstance(inner.get("message"), str):
            return inner["message"]
        if isinstance(body.get("message"), str):
            return body["message"]
    return text


def rollout_context(session_id: str | None) -> tuple[int | None, int | None]:
    """The last call's prompt tokens and the context window, from the rollout.

    `codex exec --json` reports only the turn's summed usage. The session's
    rollout file logs a `token_count` event after each call with that call's
    usage (`last_token_usage`) and the model's window.
    """
    path = _rollout_path(session_id) if session_id else None
    if path is None:
        return None, None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None
    for line in reversed(lines):
        if '"token_count"' not in line:
            continue
        try:
            info = (json.loads(line).get("payload") or {}).get("info")
        except (json.JSONDecodeError, AttributeError):
            continue
        if not isinstance(info, dict):
            continue
        last = info.get("last_token_usage")
        window = info.get("model_context_window")
        return (
            prompt_tokens(last if isinstance(last, dict) else {}, _CODEX_PROMPT_KEYS),
            window if isinstance(window, int) else None,
        )
    return None, None


def _rollout_path(session_id: str) -> Path | None:
    """Codex keeps a session as `sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl`."""
    root = _codex_home() / "sessions"
    if not root.is_dir():
        return None
    matches = sorted(root.rglob(f"rollout-*{session_id}.jsonl"))
    return matches[-1] if matches else None


class CodexStreamParser:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self._messages: list[str] = []
        self._usage: dict | None = None
        self._error: str | None = None

    def feed(self, line: str) -> list[StreamEvent]:
        line = line.strip()
        if not line:
            return []
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []

        etype = data.get("type")
        if etype == "thread.started":
            self.session_id = data.get("thread_id") or self.session_id
            return [StreamEvent(
                kind="init", label="Starting (Codex)…",
                detail={"session_id": self.session_id or ""},
            )]
        if etype == "turn.started":
            # A resumed turn's messages are the only ones that count.
            self._messages = []
            return []
        if etype in ("item.started", "item.completed"):
            item = data.get("item")
            if not isinstance(item, dict):
                return []
            if etype == "item.completed" and item.get("type") == "agent_message":
                text = (item.get("text") or "").strip()
                if text:
                    self._messages.append(text)
            return _item_events(item, started=etype == "item.started")
        if etype == "turn.completed":
            usage = data.get("usage")
            self._usage = usage if isinstance(usage, dict) else {}
            return []
        if etype == "turn.failed":
            self._error = _error_message(data.get("error")) or self._error
            return []
        if etype == "error":
            self._error = _error_message(data.get("message"))
            return []
        return []

    def failure(self) -> str | None:
        return self._error

    def finish(self, raw_stdout: str, fallback_session_id: str | None) -> RunResult:
        session_id = self.session_id or fallback_session_id
        usage = self._usage or {}
        tokens = {
            "input_tokens": usage.get("input_tokens") if self._usage is not None else None,
            "output_tokens": usage.get("output_tokens") if self._usage is not None else None,
        }
        if self._usage is None:
            text = self._error or raw_stdout.strip() or "Codex produced no response"
            return RunResult(text=text, session_id=session_id, is_error=True, **tokens)
        context_tokens, context_window = rollout_context(session_id)
        # Codex narrates as it goes ("I'll run …"), each its own message; the
        # last one is the answer.
        text = self._messages[-1] if self._messages else "(empty response)"
        return RunResult(
            text=text, session_id=session_id, **tokens,
            context_tokens=context_tokens, context_window=context_window,
        )


class CodexHarness:
    name = "codex"
    label = "Codex"
    bin = CODEX_BIN
    missing_session_marker = MISSING_SESSION_MARKER

    def launch(self, req: RunRequest) -> Launch:
        # Codex lists its own skills but can't see yuki-conductor's plugins, so
        # the prompt names those with their paths. It goes in as a config
        # override: `exec` has no system-prompt flag.
        instructions = codex_prompt(
            skill_plugin_dirs(), cwd=req.cwd,
            conversation_key=req.conversation_key, task_id=req.task_id,
        )
        argv = [resolve_bin(self.bin), "exec"]
        if req.session_id:
            argv.append("resume")
        argv.extend([
            "--json",
            # Not every cwd is a git repo, and nobody is there to trust one.
            "--skip-git-repo-check",
            # Headless: nobody can approve a command, and without this every
            # shell call is rejected while the model carries on regardless.
            "--dangerously-bypass-approvals-and-sandbox",
            "-c", f"developer_instructions={_toml_string(instructions)}",
        ])
        model = req.model or CODEX_DEFAULT_MODEL
        if model:
            argv.extend(["-m", model])
        # `--` so a prompt starting with "-" isn't read as a flag.
        argv.append("--")
        if req.session_id:
            argv.append(req.session_id)
        argv.append(req.prompt)
        return Launch(argv=argv, env=dict(os.environ))

    def new_parser(self) -> CodexStreamParser:
        return CodexStreamParser()

    def session_exists(self, session_id: str, cwd: str) -> bool:
        return _rollout_path(session_id) is not None

    def models(self) -> list[ModelOption]:
        options = model_catalog(resolve_bin(self.bin))
        # The configured model may be one the catalog doesn't list (a proxy's
        # own alias); offer it anyway, since it's what turns run on.
        default = self.default_model()
        if default and all(o.id != default for o in options):
            options.insert(0, ModelOption(id=default, label=default))
        return options

    def default_model(self) -> str | None:
        return CODEX_DEFAULT_MODEL or configured_model()
