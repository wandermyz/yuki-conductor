"""Subprocess wrapper for the Claude Code CLI."""

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass

from yuki_conductor.config import CLAUDE_BIN, CLAUDE_TIMEOUT, CLAUDE_WORKING_DIR, project_dir
from yuki_conductor.skills import SYSTEM_PROMPT, skill_plugin_dirs

SLACK_MESSAGE_LIMIT = 4000

# Registry of running Claude subprocesses, keyed by conversation_key.
_active_processes: dict[str, subprocess.Popen] = {}
_cancelled: set[str] = set()
_active_lock = threading.Lock()


def register_process(key: str, proc: subprocess.Popen) -> None:
    with _active_lock:
        _active_processes[key] = proc
        _cancelled.discard(key)


def unregister_process(key: str) -> None:
    with _active_lock:
        _active_processes.pop(key, None)


def was_cancelled(key: str) -> bool:
    with _active_lock:
        return key in _cancelled


def clear_cancelled(key: str) -> None:
    with _active_lock:
        _cancelled.discard(key)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a process and all its descendants (needed on Windows)."""
    import sys

    pid = proc.pid
    if sys.platform == "win32":
        # taskkill /T kills the whole process tree
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
            )
        except OSError:
            pass
    else:
        # On Unix, kill the process group if we have one, else just the process
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except OSError:
                pass


def cancel_process(key: str) -> bool:
    """Kill the running Claude process for *key*. Returns True if a process was found."""
    with _active_lock:
        proc = _active_processes.pop(key, None)
        if proc is not None:
            _cancelled.add(key)
    if proc is None:
        return False
    _kill_tree(proc)
    return True


def _ps_quote(arg: str) -> str:
    """Wrap an argument for PowerShell single-quoted context."""
    return "'" + arg.replace("'", "''") + "'"


@dataclass
class ClaudeResult:
    text: str
    session_id: str | None
    is_error: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


def run_claude(
    prompt: str,
    session_id: str | None = None,
    timeout: int | None = None,
    model: str | None = None,
    conversation_key: str | None = None,
    cwd: str | None = None,
) -> ClaudeResult:
    """Run claude CLI and return the result.

    Args:
        prompt: The prompt text to send.
        session_id: Optional session ID to resume.
        timeout: Timeout in seconds (defaults to CLAUDE_TIMEOUT).
        model: Optional model alias (e.g. "opus", "opus[1m]", "sonnet").
        cwd: Working directory for claude. Defaults to CLAUDE_WORKING_DIR.
    """
    args = [
        "-p",
        "--dangerously-skip-permissions",
        "--output-format", "json",
        "--append-system-prompt", SYSTEM_PROMPT,
    ]
    for plugin_dir in skill_plugin_dirs():
        args.extend(["--plugin-dir", plugin_dir])
    if model:
        args.extend(["--model", model])
    if session_id:
        args.extend(["-r", session_id])
    args.append(prompt)

    if CLAUDE_BIN.endswith(".ps1"):
        cmd = ["powershell", "-Command", f"& '{CLAUDE_BIN}' {' '.join(_ps_quote(a) for a in args)}"]
    elif CLAUDE_BIN.endswith(".sh"):
        git_bash = shutil.which("bash") or "bash"
        cmd = [git_bash, CLAUDE_BIN, *args]
    else:
        cmd = [CLAUDE_BIN, *args]

    # Unset CLAUDECODE to avoid nested session errors
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    # Let injected skills locate the yuki-conductor project (e.g. to run its CLI).
    env["YUKI_CONDUCTOR_PROJECT"] = str(project_dir())

    effective_timeout = timeout if timeout is not None else CLAUDE_TIMEOUT

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=cwd or CLAUDE_WORKING_DIR,
        )
    except FileNotFoundError:
        return ClaudeResult(
            text=f"{CLAUDE_BIN} CLI not found. Ensure it is installed and on PATH (or set CLAUDE_BIN).",
            session_id=None,
            is_error=True,
        )

    if conversation_key:
        register_process(conversation_key, proc)

    try:
        stdout, stderr = proc.communicate(timeout=effective_timeout)
    except subprocess.TimeoutExpired:
        return ClaudeResult(
            text="Claude timed out but is still running in the background. Increase CLAUDE_TIMEOUT to wait longer.",
            session_id=session_id,
            is_error=True,
        )
    finally:
        if conversation_key:
            unregister_process(conversation_key)

    if proc.returncode != 0:
        if conversation_key and was_cancelled(conversation_key):
            clear_cancelled(conversation_key)
            return ClaudeResult(
                text="(stopped by user)",
                session_id=session_id,
                is_error=False,
            )
        error_text = stderr.strip() or stdout.strip() or f"claude exited with code {proc.returncode}"
        return ClaudeResult(text=error_text, session_id=session_id, is_error=True)

    return _parse_output(stdout, session_id)


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

    # Extract token usage and cost if present
    input_tokens = data.get("input_tokens")
    output_tokens = data.get("output_tokens")
    cost_usd = data.get("cost_usd")
    # Some CLI versions nest usage under a "usage" key
    usage = data.get("usage")
    if usage and isinstance(usage, dict):
        input_tokens = input_tokens or usage.get("input_tokens")
        output_tokens = output_tokens or usage.get("output_tokens")
        cost_usd = cost_usd or usage.get("cost_usd")

    return ClaudeResult(
        text=result_text or "(empty response)",
        session_id=new_session_id,
        input_tokens=int(input_tokens) if input_tokens is not None else None,
        output_tokens=int(output_tokens) if output_tokens is not None else None,
        cost_usd=float(cost_usd) if cost_usd is not None else None,
    )
