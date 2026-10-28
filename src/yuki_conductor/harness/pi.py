"""pi harness: `pi --mode json`, https://github.com/earendil-works/pi.

pi prints one JSON event per line (strict JSONL: split on LF only). The events
that matter here:

    session                              first line; carries the session id
    message_end {message}                authoritative; assistant ones carry content
                                         blocks (text / thinking / toolCall), usage,
                                         stopReason and errorMessage
    tool_execution_update {partialResult} live tool output, cumulative, not a delta
    tool_execution_end {result, isError}
    auto_retry_start / compaction_start  status worth showing
    agent_settled                        the run is really over

pi exits 0 even when the model call failed, so the outcome comes from the last
assistant message's `stopReason`, never from the exit code. Shapes were checked
against real runs; see tests/fixtures/harness/pi.

Strata (the local model pi is used with) refuses to load while ComfyUI holds the
GPU and answers 503. pi retries three times (~15 s) and gives up; that error is
turned into a readable reply here. yuki-conductor never touches the GPU itself.
"""

import json
import os
import time
from pathlib import Path

from yuki_conductor.config import PI_BIN, PI_DEFAULT_MODEL
from yuki_conductor.harness.base import (
    SUMMARY_LIMIT,
    Launch,
    ModelOption,
    RunRequest,
    RunResult,
    StreamEvent,
    prompt_tokens,
    tool_label,
    tool_summary,
    truncate,
)
from yuki_conductor.harness.claude_code import write_temp_prompt
from yuki_conductor.skills import core_prompt

# Live tool output is cumulative and arrives up to every 100 ms. Each step the
# UI shows is one line of a short list, so only the latest line is reported, and
# only when it changed and at most this often.
PROGRESS_INTERVAL_S = 2.0

# What Strata says when vhotseat refuses to give it the GPU.
_GPU_BUSY_MARKER = "the before_load command failed"
GPU_BUSY_REPLY = (
    "Strata is unavailable right now: the GPU is busy with ComfyUI jobs. "
    "Try again in a few minutes."
)

# The prompt-side counts pi reports for one model call.
_PI_PROMPT_KEYS = ("input", "cacheRead", "cacheWrite")


def _agent_dir() -> Path:
    """pi's config directory, which is also where its model catalogs live."""
    return Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()


def _catalog_models(catalog, provider: str) -> list:
    """The model list of `provider` in one of pi's catalog files.

    `models.json` nests its providers under a `providers` key; the cached
    catalog `models-store.json` keys them at the top level.
    """
    if not isinstance(catalog, dict):
        return []
    entry = (catalog.get("providers") or {}).get(provider) or catalog.get(provider)
    models = entry.get("models") if isinstance(entry, dict) else entry
    return models if isinstance(models, list) else []


def model_context_window(provider: str | None, model: str | None) -> int | None:
    """The context window pi itself compacts against for this model.

    pi's JSON stream reports usage but never the window, so the limit is read
    from the same files pi reads: the cached provider catalog, with the user's
    `models.json` overrides on top of it.
    """
    if not provider or not model:
        return None
    window = None
    for name in ("models-store.json", "models.json"):
        try:
            catalog = json.loads((_agent_dir() / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for entry in _catalog_models(catalog, provider):
            if isinstance(entry, dict) and entry.get("id") == model:
                size = entry.get("contextWindow")
                if isinstance(size, int):
                    window = size
    return window


def _text_of(content) -> str:
    """Flatten a tool result's content (str, or list of text blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _last_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def _assistant_events(message: dict) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                events.append(StreamEvent(
                    kind="text",
                    label=truncate(text, SUMMARY_LIMIT),
                    detail={"text": text},
                ))
        elif btype == "thinking":
            text = (block.get("thinking") or "").strip()
            if text:
                events.append(StreamEvent(
                    kind="thinking",
                    label=truncate(text, SUMMARY_LIMIT),
                    detail={"text": text},
                ))
        elif btype == "toolCall":
            name = block.get("name") or "tool"
            summary = tool_summary(block.get("arguments") or {})
            events.append(StreamEvent(
                kind="tool_use",
                label=tool_label(name, summary),
                detail={"tool_use_id": block.get("id") or "", "name": name, "summary": summary},
            ))
    return events


def friendly_error(message: str) -> str:
    """Turn pi's raw errorMessage into what the user should read."""
    if _GPU_BUSY_MARKER in message:
        return GPU_BUSY_REPLY
    return message or "pi run failed with no error message"


class PiStreamParser:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self.session_id: str | None = None
        self._last_assistant: dict | None = None
        self._input_tokens = 0
        self._output_tokens = 0
        self._saw_usage = False
        # toolCallId -> (last reported line, when)
        self._progress: dict[str, tuple[str, float]] = {}

    def feed(self, line: str) -> list[StreamEvent]:
        line = line.rstrip("\r\n")
        if not line.strip():
            return []
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []

        etype = data.get("type")
        if etype == "session":
            self.session_id = data.get("id") or self.session_id
            return [StreamEvent(
                kind="init", label="Starting (pi)…",
                detail={"session_id": self.session_id or ""},
            )]
        if etype == "message_end":
            return self._message_end(data.get("message") or {})
        if etype == "tool_execution_update":
            return self._tool_progress(data)
        if etype == "tool_execution_end":
            call_id = data.get("toolCallId") or ""
            reported, _ = self._progress.pop(call_id, ("", 0.0))
            is_error = bool(data.get("isError"))
            output = _text_of((data.get("result") or {}).get("content"))
            # A short command's whole output is often its one progress line;
            # don't show the same line twice.
            if not is_error and reported and reported == _last_line(output) and len(
                [ln for ln in output.splitlines() if ln.strip()]
            ) == 1:
                return []
            summary = truncate(output, SUMMARY_LIMIT)
            return [StreamEvent(
                kind="tool_result",
                label=("error: " + summary) if is_error else summary,
                detail={"tool_use_id": call_id, "is_error": is_error, "summary": summary},
            )]
        if etype == "auto_retry_start":
            attempt, total = data.get("attempt"), data.get("maxAttempts")
            reason = friendly_error(data.get("errorMessage") or "")
            return [StreamEvent(
                kind="init",
                label=truncate(f"Retrying ({attempt}/{total}): {reason}", SUMMARY_LIMIT),
                detail={"attempt": attempt, "max_attempts": total},
            )]
        if etype == "compaction_start":
            return [StreamEvent(kind="init", label="Compacting the conversation…", detail={})]
        return []

    def _message_end(self, message: dict) -> list[StreamEvent]:
        if message.get("role") != "assistant":
            return []
        self._last_assistant = message
        usage = message.get("usage")
        if isinstance(usage, dict):
            self._saw_usage = True
            self._input_tokens += int(usage.get("input") or 0) + int(usage.get("cacheRead") or 0)
            self._output_tokens += int(usage.get("output") or 0)
            # usage.cost is ignored: pi prices tokens from models.json, which is
            # all zeros for a local model, so it would only ever read "$0.00".
        return _assistant_events(message)

    def _tool_progress(self, data: dict) -> list[StreamEvent]:
        call_id = data.get("toolCallId") or ""
        text = _text_of((data.get("partialResult") or {}).get("content"))
        line = _last_line(text)
        if not line:
            return []
        now = self._clock()
        last_line, last_at = self._progress.get(call_id, ("", float("-inf")))
        if line == last_line or now - last_at < PROGRESS_INTERVAL_S:
            return []
        self._progress[call_id] = (line, now)
        summary = truncate(line, SUMMARY_LIMIT)
        return [StreamEvent(
            kind="tool_result",
            label=summary,
            detail={"tool_use_id": call_id, "is_error": False, "summary": summary, "partial": True},
        )]

    def finish(self, raw_stdout: str, fallback_session_id: str | None) -> RunResult:
        session_id = self.session_id or fallback_session_id
        usage = {
            "input_tokens": self._input_tokens if self._saw_usage else None,
            "output_tokens": self._output_tokens if self._saw_usage else None,
        }
        msg = self._last_assistant
        if msg is None:
            text = raw_stdout.strip()
            return RunResult(
                text=text or "pi produced no response", session_id=session_id, is_error=True
            )
        stop = msg.get("stopReason")
        if stop in ("error", "aborted"):
            # A failed request's usage says nothing about the session's context
            # (a refused call reports 0), so leave the meter where it was.
            return RunResult(
                text=friendly_error(msg.get("errorMessage") or ""),
                session_id=session_id, is_error=True, **usage,
            )
        msg_usage = msg.get("usage")
        provider = msg.get("provider")
        context = {
            # How full the window was on the last request of this run, against
            # the window pi would have compacted at.
            "context_tokens": prompt_tokens(
                msg_usage if isinstance(msg_usage, dict) else {}, _PI_PROMPT_KEYS
            ),
            # A virtual model routes to a physical `responseModel`, which the
            # catalog may not list; the requested `model` is the fallback.
            "context_window": model_context_window(provider, msg.get("responseModel"))
            or model_context_window(provider, msg.get("model")),
        }
        text = "\n\n".join(
            b.get("text", "").strip()
            for b in msg.get("content") or []
            if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
        )
        if stop == "length":
            text = (text + "\n\n" if text else "") + "(reply cut off: the model hit its output limit)"
        # A virtual model routes to a physical `responseModel`: that one answered.
        model = msg.get("responseModel") or msg.get("model")
        return RunResult(
            text=text or "(empty response)", session_id=session_id, **usage, **context,
            model=model if isinstance(model, str) else None,
        )


    def failure(self) -> str | None:
        # pi exits 0 even when the model call failed; `finish` reads the error.
        return None


class PiHarness:
    name = "pi"
    label = "pi"
    bin = PI_BIN
    # `--session-id` creates a session it doesn't have, so a resume never
    # fails; `session_exists` is the only way to notice the history is gone.
    missing_session_marker = None

    def launch(self, req: RunRequest) -> Launch:
        # pi lists skills natively, so it gets only the harness-neutral part.
        prompt_file = write_temp_prompt(
            core_prompt(conversation_key=req.conversation_key, task_id=req.task_id)
        )
        argv = [
            self.bin,
            "--mode", "json",
            # Headless runs can't answer pi's project-trust prompt; ignore
            # project-local .pi/ files rather than wait on it.
            "--no-approve",
            "--model", req.model or PI_DEFAULT_MODEL,
            "--append-system-prompt", prompt_file,
        ]
        # pi creates a session with this id if it doesn't exist, so a new
        # conversation and a resumed one take the same flag.
        if req.session_id:
            argv.extend(["--session-id", req.session_id])
        # `--` so a prompt starting with "-" isn't read as a flag.
        argv.extend(["--", req.prompt])
        return Launch(argv=argv, env=dict(os.environ), temp_files=[prompt_file])

    def new_parser(self) -> PiStreamParser:
        return PiStreamParser()

    def session_exists(self, session_id: str, cwd: str) -> bool:
        """Look for a session file named with `session_id` in pi's store.

        pi groups sessions by cwd under `<agent-dir>/sessions/` (or
        `PI_CODING_AGENT_SESSION_DIR`) but doesn't document the file naming, so
        this searches the whole store. A miss only costs a fresh session that
        is handed the full history, never lost context.
        """
        override = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
        root = Path(override).expanduser() if override else _agent_dir() / "sessions"
        if not root.is_dir():
            return False
        return any(root.rglob(f"*{session_id}*"))

    def models(self) -> list[ModelOption]:
        # pi runs one local model; there is nothing to pick between.
        return []

    def default_model(self) -> str | None:
        return PI_DEFAULT_MODEL

    def default_label(self) -> str | None:
        return PI_DEFAULT_MODEL
