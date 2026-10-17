"""Claude Code harness: `claude -p --output-format stream-json`.

The CLI emits one JSON object per line. Only a subset is useful for showing
progress, and the raw shape is far too verbose to push at a browser or a Slack
status line. `parse_stream_line` turns a raw line into zero or more
`StreamEvent`s; the terminal `result` record becomes the `RunResult`.
"""

import json
import os
import shutil
import tempfile

from yuki_conductor.config import CLAUDE_BIN, CLAUDE_DEFAULT_MODEL
from yuki_conductor.harness.base import (
    SUMMARY_LIMIT,
    TEXT_LIMIT,
    Launch,
    RunRequest,
    RunResult,
    StreamEvent,
    tool_summary,
    truncate,
)
from yuki_conductor.skills import skill_plugin_dirs, system_prompt

# ---------------------------------------------------------------------------
# stream-json parsing
# ---------------------------------------------------------------------------


def _result_summary(content) -> str:
    """Flatten a tool_result content field (str, or list of blocks) to text."""
    if isinstance(content, str):
        return truncate(content, SUMMARY_LIMIT)
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return truncate(" ".join(parts), SUMMARY_LIMIT)
    return ""


def _assistant_events(message: dict) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                events.append(
                    StreamEvent(
                        kind="text",
                        label=truncate(text, SUMMARY_LIMIT),
                        detail={"text": truncate(text, TEXT_LIMIT)},
                    )
                )
        elif btype == "thinking":
            text = (block.get("thinking") or "").strip()
            if text:
                events.append(
                    StreamEvent(
                        kind="thinking",
                        label=truncate(text, SUMMARY_LIMIT),
                        detail={"text": truncate(text, TEXT_LIMIT)},
                    )
                )
        elif btype == "tool_use":
            name = block.get("name") or "tool"
            summary = tool_summary(block.get("input") or {})
            events.append(
                StreamEvent(
                    kind="tool_use",
                    label=f"{name}({summary})" if summary else name,
                    detail={
                        "tool_use_id": block.get("id") or "",
                        "name": name,
                        "summary": summary,
                    },
                )
            )
    return events


def _user_events(message: dict) -> list[StreamEvent]:
    """Tool results come back as `user` messages containing tool_result blocks."""
    events: list[StreamEvent] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        is_error = bool(block.get("is_error"))
        summary = _result_summary(block.get("content"))
        events.append(
            StreamEvent(
                kind="tool_result",
                label=("error: " + summary) if is_error else summary,
                detail={
                    "tool_use_id": block.get("tool_use_id") or "",
                    "is_error": is_error,
                    "summary": summary,
                },
            )
        )
    return events


def parse_stream_line(line: str) -> tuple[list[StreamEvent], dict | None]:
    """Parse one NDJSON line.

    Returns `(events, final)` where `final` is the raw `result` payload when
    this line is the terminal result record, else None. Unparseable or
    uninteresting lines yield `([], None)` rather than raising — a stray
    non-JSON line on stdout must not abort a run.
    """
    line = line.strip()
    if not line:
        return [], None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return [], None
    if not isinstance(data, dict):
        return [], None

    msg_type = data.get("type")
    if msg_type == "result":
        return [], data
    if msg_type == "system" and data.get("subtype") == "init":
        model = data.get("model") or ""
        return [
            StreamEvent(
                kind="init",
                label=f"Starting{f' ({model})' if model else ''}…",
                detail={"model": model, "session_id": data.get("session_id") or ""},
            )
        ], None
    if msg_type == "assistant":
        return _assistant_events(data.get("message") or {}), None
    if msg_type == "user":
        return _user_events(data.get("message") or {}), None
    return [], None


def build_result(
    final: dict | None, raw_stdout: str, fallback_session_id: str | None
) -> RunResult:
    """Turn the terminal `result` record into a RunResult."""
    if final is None:
        # No result record — the CLI printed something we don't understand.
        text = raw_stdout.strip()
        return RunResult(text=text or "(empty response)", session_id=fallback_session_id)

    result_text = final.get("result") or ""
    new_session_id = final.get("session_id") or fallback_session_id

    usage = final.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = final.get("input_tokens") or usage.get("input_tokens")
    output_tokens = final.get("output_tokens") or usage.get("output_tokens")
    cost_usd = final.get("total_cost_usd") or final.get("cost_usd") or usage.get("cost_usd")

    return RunResult(
        text=result_text or "(empty response)",
        session_id=new_session_id,
        is_error=bool(final.get("is_error")),
        input_tokens=int(input_tokens) if input_tokens is not None else None,
        output_tokens=int(output_tokens) if output_tokens is not None else None,
        cost_usd=float(cost_usd) if cost_usd is not None else None,
    )


class ClaudeStreamParser:
    def __init__(self) -> None:
        self._final: dict | None = None

    def feed(self, line: str) -> list[StreamEvent]:
        events, final = parse_stream_line(line)
        if final is not None:
            self._final = final
        return events

    def finish(self, raw_stdout: str, fallback_session_id: str | None) -> RunResult:
        return build_result(self._final, raw_stdout, fallback_session_id)


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------


def _ps_quote(arg: str) -> str:
    """Wrap an argument for PowerShell single-quoted context."""
    return "'" + arg.replace("'", "''") + "'"


def _git_bash() -> str:
    """Locate Git Bash.

    `shutil.which("bash")` is not enough: on Windows it often resolves to
    C:\\Windows\\System32\\bash.exe, the WSL launcher, which cannot execute a
    Windows-path script and fails with "No such file or directory".
    """
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "usr", "bin", "bash.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "bin", "bash.exe"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return "bash"


def write_temp_prompt(text: str) -> str:
    """Write `text` to a temp file for a `--append-system-prompt`-style flag."""
    fd, path = tempfile.mkstemp(prefix="yuki-sysprompt-", suffix=".md", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class ClaudeCodeHarness:
    name = "claude-code"
    label = "Claude Code"
    bin = CLAUDE_BIN

    def launch(self, req: RunRequest) -> Launch:
        plugin_dirs = skill_plugin_dirs()
        # The synthesized skills listing runs to tens of KB in skill-heavy
        # projects. Passing it inline blows Windows' 32767-char command-line
        # limit, so it goes through a temp file instead.
        prompt_file = write_temp_prompt(
            system_prompt(plugin_dirs, cwd=req.cwd, conversation_key=req.conversation_key)
        )
        args = [
            "-p",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
            # Without this, a headless run loads no settings at all, so neither
            # user-scope skills (~/.claude/skills) nor project-scope ones
            # (<cwd>/.claude/skills) are reachable.
            "--setting-sources", "user,project,local",
            "--append-system-prompt-file", prompt_file,
        ]
        for plugin_dir in plugin_dirs:
            args.extend(["--plugin-dir", plugin_dir])
        args.extend(["--model", req.model or CLAUDE_DEFAULT_MODEL])
        if req.session_id:
            args.extend(["-r", req.session_id])
        args.append(req.prompt)

        if self.bin.endswith(".ps1"):
            argv = ["powershell", "-Command", f"& '{self.bin}' {' '.join(_ps_quote(a) for a in args)}"]
        elif self.bin.endswith(".sh"):
            argv = [_git_bash(), self.bin, *args]
        else:
            argv = [self.bin, *args]

        # Unset CLAUDECODE to avoid nested session errors.
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        return Launch(argv=argv, env=env, temp_files=[prompt_file])

    def new_parser(self) -> ClaudeStreamParser:
        return ClaudeStreamParser()
