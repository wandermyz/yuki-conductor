"""Normalization of `claude --output-format stream-json` lines into UI events.

The CLI emits one JSON object per line. Only a subset is useful for showing
progress, and the raw shape is far too verbose to push at a browser or a Slack
status line. `parse_stream_line` turns a raw line into zero or more
`StreamEvent`s — small, JSON-serializable records with a short human `label`.
"""

import json
from dataclasses import asdict, dataclass, field

# Longest tool-argument snippet kept in a label. Slack's assistant status and
# the web step list are both single lines, so anything longer just truncates.
SUMMARY_LIMIT = 120
TEXT_LIMIT = 400


@dataclass
class StreamEvent:
    """One progress step from a Claude run."""

    kind: str  # "init" | "text" | "thinking" | "tool_use" | "tool_result"
    label: str  # short single-line description, safe for a Slack status
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_summary(name: str, tool_input: dict) -> str:
    """Pick the one argument that best identifies what a tool call is doing."""
    if not isinstance(tool_input, dict):
        return ""
    for key in (
        "command", "file_path", "path", "pattern", "query", "url",
        "description", "prompt", "skill", "notebook_path",
    ):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(value, SUMMARY_LIMIT)
    for value in tool_input.values():
        if isinstance(value, str) and value.strip():
            return _truncate(value, SUMMARY_LIMIT)
    return ""


def _result_summary(content) -> str:
    """Flatten a tool_result content field (str, or list of blocks) to text."""
    if isinstance(content, str):
        return _truncate(content, SUMMARY_LIMIT)
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return _truncate(" ".join(parts), SUMMARY_LIMIT)
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
                        label=_truncate(text, SUMMARY_LIMIT),
                        detail={"text": _truncate(text, TEXT_LIMIT)},
                    )
                )
        elif btype == "thinking":
            text = (block.get("thinking") or "").strip()
            if text:
                events.append(
                    StreamEvent(
                        kind="thinking",
                        label=_truncate(text, SUMMARY_LIMIT),
                        detail={"text": _truncate(text, TEXT_LIMIT)},
                    )
                )
        elif btype == "tool_use":
            name = block.get("name") or "tool"
            summary = _tool_summary(name, block.get("input") or {})
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
