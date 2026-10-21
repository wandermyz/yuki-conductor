"""Types shared by every agent harness.

A harness is one agent CLI (Claude Code, pi, …) that yuki-conductor can spawn
for a turn. Each adapter turns its CLI's stdout into the same `StreamEvent`s and
`RunResult`, so platforms never see a harness-specific format.
"""

from dataclasses import asdict, dataclass, field
from typing import Protocol

# Longest snippet kept in a label. Slack's assistant status and the web step
# list are both single lines, so anything longer just truncates. A step's
# `detail` carries the full text of a reply or tool call for the web UI to
# expand; a tool result is only ever kept to this length.
SUMMARY_LIMIT = 120


@dataclass
class StreamEvent:
    """One progress step from an agent run."""

    kind: str  # "init" | "text" | "thinking" | "tool_use" | "tool_result" | "status"
    label: str  # short single-line description, safe for a Slack status
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunResult:
    text: str
    session_id: str | None
    is_error: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    # How full the model's context was on the last call of the turn, and how
    # big that window is. Both None when the harness reported neither.
    context_tokens: int | None = None
    context_window: int | None = None
    # The resumed session no longer exists (the harness said so). The caller
    # can retry with a fresh session that carries the history in the prompt.
    session_missing: bool = False
    # Stopped through `cancel_process` rather than finishing on its own.
    cancelled: bool = False


@dataclass
class RunRequest:
    """Everything a harness needs to build one turn's command line."""

    prompt: str
    session_id: str | None
    model: str | None  # None: the harness's own default
    cwd: str
    conversation_key: str | None  # named in the system prompt for `yuki-conductor send`
    task_id: int | None = None  # taskboard task this turn works on, if any


@dataclass
class Launch:
    """A built command, plus temp files to remove once the run ends."""

    argv: list[str]
    env: dict[str, str]
    temp_files: list[str] = field(default_factory=list)


class StreamParser(Protocol):
    """Stateful, one per run: fed stdout line by line, then asked for the result."""

    def feed(self, line: str) -> list[StreamEvent]: ...

    def finish(self, raw_stdout: str, fallback_session_id: str | None) -> RunResult: ...


class Harness(Protocol):
    name: str  # stored per conversation: "claude-code" | "pi"
    label: str  # shown in the UI
    bin: str  # executable, for error messages and availability checks
    # Text the CLI prints when asked to resume a session it doesn't have, or
    # None if it never fails that way (pi silently starts a new one).
    missing_session_marker: str | None

    def launch(self, req: RunRequest) -> Launch: ...

    def new_parser(self) -> StreamParser: ...

    def session_exists(self, session_id: str, cwd: str) -> bool:
        """Whether `session_id` can still be resumed from `cwd`."""
        ...


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def prompt_tokens(usage: dict, keys: tuple[str, ...]) -> int | None:
    """Sum the prompt-side token counts of one model call.

    That sum is what the call spent of its context window: fresh input plus
    everything read from or written to the prompt cache. Output tokens are not
    part of it. Returns None when `usage` reports none of `keys`.
    """
    total = 0
    seen = False
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int):
            total += value
            seen = True
    return total if seen else None


def tool_summary(tool_input: dict) -> str:
    """Pick the one argument that best identifies what a tool call is doing.

    Returned whole; callers truncate it for a label.
    """
    if not isinstance(tool_input, dict):
        return ""
    for key in (
        "command", "file_path", "path", "pattern", "query", "url",
        "description", "prompt", "skill", "notebook_path",
    ):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in tool_input.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def tool_label(name: str, summary: str) -> str:
    return f"{name}({truncate(summary, SUMMARY_LIMIT)})" if summary else name
