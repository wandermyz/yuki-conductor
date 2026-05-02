"""Subprocess wrapper for the Claude Code CLI."""

import json
import os
import subprocess
from dataclasses import dataclass

from yuki_conductor.config import CLAUDE_TIMEOUT, CLAUDE_WORKING_DIR

SLACK_MESSAGE_LIMIT = 4000


@dataclass
class ClaudeResult:
    text: str
    session_id: str | None
    is_error: bool = False


def run_claude(
    prompt: str,
    session_id: str | None = None,
    timeout: int | None = None,
    model: str | None = None,
) -> ClaudeResult:
    """Run claude CLI and return the result.

    Args:
        prompt: The prompt text to send.
        session_id: Optional session ID to resume.
        timeout: Timeout in seconds (defaults to CLAUDE_TIMEOUT).
        model: Optional model alias (e.g. "sonnet", "opus", "haiku").
    """
    cmd = [
        "claude",
        "-p",
        "--dangerously-skip-permissions",
        "--output-format", "json",
    ]
    if model:
        cmd.extend(["--model", model])
    if session_id:
        cmd.extend(["-r", session_id])
    cmd.append(prompt)

    # Unset CLAUDECODE to avoid nested session errors
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    effective_timeout = timeout if timeout is not None else CLAUDE_TIMEOUT

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            env=env,
            cwd=CLAUDE_WORKING_DIR,
        )
    except subprocess.TimeoutExpired:
        return ClaudeResult(
            text="Claude timed out. Try a simpler prompt or increase CLAUDE_TIMEOUT.",
            session_id=session_id,
            is_error=True,
        )
    except FileNotFoundError:
        return ClaudeResult(
            text="claude CLI not found. Ensure it is installed and on PATH.",
            session_id=None,
            is_error=True,
        )

    if proc.returncode != 0:
        error_text = proc.stderr.strip() or proc.stdout.strip() or f"claude exited with code {proc.returncode}"
        return ClaudeResult(text=error_text, session_id=session_id, is_error=True)

    return _parse_output(proc.stdout, session_id)


def _parse_output(stdout: str, fallback_session_id: str | None) -> ClaudeResult:
    """Parse JSON output from claude CLI."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # Fall back to raw text if JSON parsing fails
        text = stdout.strip()
        if len(text) > SLACK_MESSAGE_LIMIT:
            text = text[: SLACK_MESSAGE_LIMIT - 50] + "\n\n... (truncated, response too long)"
        return ClaudeResult(text=text or "(empty response)", session_id=fallback_session_id)

    result_text = data.get("result", "")
    new_session_id = data.get("session_id") or fallback_session_id

    if len(result_text) > SLACK_MESSAGE_LIMIT:
        result_text = result_text[: SLACK_MESSAGE_LIMIT - 50] + "\n\n... (truncated, response too long)"

    return ClaudeResult(
        text=result_text or "(empty response)",
        session_id=new_session_id,
    )
