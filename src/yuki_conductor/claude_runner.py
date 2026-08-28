"""Subprocess wrapper for the Claude Code CLI."""

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from yuki_conductor.config import CLAUDE_BIN, CLAUDE_TIMEOUT, CLAUDE_WORKING_DIR, project_dir
from yuki_conductor.skills import skill_plugin_dirs, system_prompt
from yuki_conductor.stream_events import StreamEvent, parse_stream_line

logger = logging.getLogger(__name__)

# How often the idle watchdog checks the last-output timestamp.
_WATCHDOG_POLL_SECONDS = 5.0

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


def _write_temp_prompt(text: str) -> str:
    """Write `text` to a temp file for `--append-system-prompt-file`."""
    fd, path = tempfile.mkstemp(prefix="yuki-sysprompt-", suffix=".md", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _cleanup_temp_prompt(path: str) -> None:
    """Remove a temp system-prompt file, ignoring an already-deleted one."""
    try:
        os.unlink(path)
    except OSError:
        logger.debug("Could not remove temp system prompt %s", path, exc_info=True)


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
    web_conversation_id: str | None = None,
    on_event: Callable[[StreamEvent], None] | None = None,
) -> ClaudeResult:
    """Run claude CLI and return the result.

    The CLI is invoked with `--output-format stream-json --verbose`, so stdout
    is NDJSON: one record per intermediate step, ending in a `result` record.
    Each step is normalized into a `StreamEvent` and handed to `on_event` as it
    arrives; only the final result is returned.

    Args:
        prompt: The prompt text to send.
        session_id: Optional session ID to resume.
        timeout: Idle timeout in seconds — the run is killed only after this
            long with no output on stdout or stderr, so a long but active run
            is never cut off. Defaults to CLAUDE_TIMEOUT.
        model: Optional model alias (e.g. "opus", "opus[1m]", "sonnet").
        cwd: Working directory for claude. Defaults to CLAUDE_WORKING_DIR.
        web_conversation_id: Web conversation this run belongs to, if any. Named
            in the system prompt so the run can push messages back into it via
            `yuki-conductor send`.
        on_event: Called from the reader thread for every intermediate step.
            Exceptions raised by the callback are logged and swallowed so a
            broken UI subscriber can't kill the run.
    """
    plugin_dirs = skill_plugin_dirs()
    effective_cwd = cwd or CLAUDE_WORKING_DIR
    # The synthesized skills listing runs to tens of KB in skill-heavy projects.
    # Passing it inline blows Windows' 32767-char command-line limit, so it goes
    # through a temp file instead; `prompt_file` is removed once the run ends.
    prompt_file = _write_temp_prompt(
        system_prompt(plugin_dirs, cwd=effective_cwd, conversation_key=web_conversation_id)
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
    if model:
        args.extend(["--model", model])
    if session_id:
        args.extend(["-r", session_id])
    args.append(prompt)

    if CLAUDE_BIN.endswith(".ps1"):
        cmd = ["powershell", "-Command", f"& '{CLAUDE_BIN}' {' '.join(_ps_quote(a) for a in args)}"]
    elif CLAUDE_BIN.endswith(".sh"):
        cmd = [_git_bash(), CLAUDE_BIN, *args]
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
            cwd=effective_cwd,
        )
    except OSError as exc:
        _cleanup_temp_prompt(prompt_file)
        # Windows reports an over-long command line as WinError 206, which arrives
        # as FileNotFoundError and would otherwise read as a missing CLI.
        if getattr(exc, "winerror", None) == 206:
            text = (
                "Claude could not be started: the command line exceeded the Windows "
                "32767-character limit. The synthesized skills listing for this "
                "project is likely very large."
            )
        elif isinstance(exc, FileNotFoundError):
            text = f"{CLAUDE_BIN} CLI not found. Ensure it is installed and on PATH (or set CLAUDE_BIN)."
        else:
            text = f"Failed to start {CLAUDE_BIN}: {exc}"
        return ClaudeResult(text=text, session_id=None, is_error=True)

    if conversation_key:
        register_process(conversation_key, proc)

    try:
        final, raw_stdout, stderr, timed_out = _consume_stream(
            proc, effective_timeout, on_event
        )
    finally:
        _cleanup_temp_prompt(prompt_file)
        if conversation_key:
            unregister_process(conversation_key)

    if timed_out:
        return ClaudeResult(
            text=(
                f"Claude produced no output for {effective_timeout}s and was stopped. "
                "Increase CLAUDE_TIMEOUT to wait longer."
            ),
            session_id=session_id,
            is_error=True,
        )

    if proc.returncode != 0:
        if conversation_key and was_cancelled(conversation_key):
            clear_cancelled(conversation_key)
            return ClaudeResult(
                text="(stopped by user)",
                session_id=session_id,
                is_error=False,
            )
        error_text = stderr.strip() or raw_stdout.strip() or f"claude exited with code {proc.returncode}"
        return ClaudeResult(text=error_text, session_id=session_id, is_error=True)

    return _build_result(final, raw_stdout, session_id)


def _consume_stream(
    proc: subprocess.Popen,
    timeout: int,
    on_event: Callable[[StreamEvent], None] | None,
) -> tuple[dict | None, str, str, bool]:
    """Read NDJSON from `proc` until it exits.

    stderr is drained on a helper thread so a chatty CLI can't deadlock on a
    full pipe while we're blocked reading stdout. Because that read blocks
    indefinitely on a hung CLI, a watchdog thread kills the process tree once
    `timeout` seconds pass *with no output at all*, which closes the pipe and
    unblocks us. The deadline is idle-based rather than wall-clock: a long run
    that keeps streaming steps is never cut off, so the timeout only fires on a
    genuinely silent process.

    Returns `(final_result_record, raw_stdout, stderr, timed_out)`.
    """
    stderr_chunks: list[str] = []
    last_activity = time.monotonic()
    activity_lock = threading.Lock()

    def touch() -> None:
        nonlocal last_activity
        with activity_lock:
            last_activity = time.monotonic()

    def drain_stderr() -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            stderr_chunks.append(line)
            touch()

    stderr_thread = threading.Thread(target=drain_stderr, name="claude-stderr", daemon=True)
    stderr_thread.start()

    timed_out = threading.Event()
    finished = threading.Event()

    def watch() -> None:
        # Poll rather than arm a one-shot timer: every line of output pushes the
        # deadline out, so the fire time isn't known in advance.
        poll = min(timeout, _WATCHDOG_POLL_SECONDS)
        while not finished.wait(poll):
            with activity_lock:
                idle = time.monotonic() - last_activity
            if idle >= timeout:
                timed_out.set()
                _kill_tree(proc)
                return

    watchdog = threading.Thread(target=watch, name="claude-watchdog", daemon=True)
    watchdog.start()

    final: dict | None = None
    raw_lines: list[str] = []
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                raw_lines.append(line)
                touch()
                events, result = parse_stream_line(line)
                if on_event is not None:
                    for event in events:
                        try:
                            on_event(event)
                        except Exception:
                            logger.debug("Stream event callback failed", exc_info=True)
                if result is not None:
                    final = result
        proc.wait()
    finally:
        finished.set()
        watchdog.join(timeout=5)

    stderr_thread.join(timeout=5)
    return final, "".join(raw_lines), "".join(stderr_chunks), timed_out.is_set()


def _build_result(
    final: dict | None, raw_stdout: str, fallback_session_id: str | None
) -> ClaudeResult:
    """Turn the terminal `result` record into a ClaudeResult."""
    if final is None:
        # No result record — the CLI printed something we don't understand.
        text = raw_stdout.strip()
        return ClaudeResult(text=text or "(empty response)", session_id=fallback_session_id)

    result_text = final.get("result") or ""
    new_session_id = final.get("session_id") or fallback_session_id

    usage = final.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = final.get("input_tokens") or usage.get("input_tokens")
    output_tokens = final.get("output_tokens") or usage.get("output_tokens")
    cost_usd = final.get("total_cost_usd") or final.get("cost_usd") or usage.get("cost_usd")

    return ClaudeResult(
        text=result_text or "(empty response)",
        session_id=new_session_id,
        is_error=bool(final.get("is_error")),
        input_tokens=int(input_tokens) if input_tokens is not None else None,
        output_tokens=int(output_tokens) if output_tokens is not None else None,
        cost_usd=float(cost_usd) if cost_usd is not None else None,
    )
