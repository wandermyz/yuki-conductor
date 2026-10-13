"""Types shared by every agent harness.

A harness is one agent CLI (Claude Code, pi, …) that yuki-conductor can spawn
for a turn. Each adapter turns its CLI's stdout into the same `StreamEvent`s and
`RunResult`, so platforms never see a harness-specific format.
"""

from dataclasses import asdict, dataclass, field
from typing import Protocol

# Longest snippet kept in a label. Slack's assistant status and the web step
# list are both single lines, so anything longer just truncates.
SUMMARY_LIMIT = 120
TEXT_LIMIT = 400


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


@dataclass
class RunRequest:
    """Everything a harness needs to build one turn's command line."""

    prompt: str
    session_id: str | None
    model: str | None  # None: the harness's own default
    cwd: str
    conversation_key: str | None  # named in the system prompt for `yuki-conductor send`


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

    def launch(self, req: RunRequest) -> Launch: ...

    def new_parser(self) -> StreamParser: ...


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_summary(tool_input: dict) -> str:
    """Pick the one argument that best identifies what a tool call is doing."""
    if not isinstance(tool_input, dict):
        return ""
    for key in (
        "command", "file_path", "path", "pattern", "query", "url",
        "description", "prompt", "skill", "notebook_path",
    ):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return truncate(value, SUMMARY_LIMIT)
    for value in tool_input.values():
        if isinstance(value, str) and value.strip():
            return truncate(value, SUMMARY_LIMIT)
    return ""
