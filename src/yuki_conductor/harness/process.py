"""Run one agent turn as a subprocess, whatever the harness.

Owns everything that doesn't depend on the CLI being driven: the cancel
registry, the idle watchdog, draining stderr, and turning launch failures,
timeouts and cancels into a `RunResult`. Each harness only builds the command
line and parses stdout.
"""

import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable

from yuki_conductor.config import CLAUDE_TIMEOUT, CLAUDE_WORKING_DIR, project_dir
from yuki_conductor.harness.base import (
    Harness,
    RunRequest,
    RunResult,
    StreamEvent,
    StreamParser,
)

logger = logging.getLogger(__name__)

# How often the idle watchdog checks the last-output timestamp.
_WATCHDOG_POLL_SECONDS = 5.0

# Registry of running agent subprocesses, keyed by conversation_key.
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
    """Kill the running agent process for *key*. Returns True if a process was found."""
    with _active_lock:
        proc = _active_processes.pop(key, None)
        if proc is not None:
            _cancelled.add(key)
    if proc is None:
        return False
    _kill_tree(proc)
    return True


def _cleanup_temp_files(paths: list[str]) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            logger.debug("Could not remove temp file %s", path, exc_info=True)


def run_agent(
    harness: Harness,
    prompt: str,
    session_id: str | None = None,
    timeout: int | None = None,
    model: str | None = None,
    conversation_key: str | None = None,
    cwd: str | None = None,
    push_conversation_id: str | None = None,
    on_event: Callable[[StreamEvent], None] | None = None,
) -> RunResult:
    """Run one turn of `harness` and return its result.

    The harness's CLI streams one record per intermediate step on stdout. Each
    is normalized into a `StreamEvent` and handed to `on_event` as it arrives;
    only the final result is returned.

    Args:
        prompt: The prompt text to send.
        session_id: Optional session ID to resume.
        timeout: Idle timeout in seconds — the run is killed only after this
            long with no output on stdout or stderr, so a long but active run
            is never cut off. Defaults to CLAUDE_TIMEOUT.
        model: Harness-native model id, or None for the harness's default.
        conversation_key: Registers the process so `cancel_process` can stop it.
        cwd: Working directory for the run. Defaults to CLAUDE_WORKING_DIR.
        push_conversation_id: Conversation this run belongs to, if `/api/push`
            can route to it. Named in the system prompt so the run can push
            messages back into it via `yuki-conductor send`.
        on_event: Called from the reader thread for every intermediate step.
            Exceptions raised by the callback are logged and swallowed so a
            broken UI subscriber can't kill the run.
    """
    effective_cwd = cwd or CLAUDE_WORKING_DIR
    launch = harness.launch(RunRequest(
        prompt=prompt,
        session_id=session_id,
        model=model,
        cwd=effective_cwd,
        conversation_key=push_conversation_id,
    ))
    # Let injected skills locate the yuki-conductor project (e.g. to run its CLI).
    env = {**launch.env, "YUKI_CONDUCTOR_PROJECT": str(project_dir())}
    effective_timeout = timeout if timeout is not None else CLAUDE_TIMEOUT

    try:
        proc = subprocess.Popen(
            launch.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=effective_cwd,
            # Own process group, so `_kill_tree`'s killpg hits the agent and its
            # children only. Inherited, the group is the daemon's: a cancel or
            # idle timeout would SIGTERM the daemon itself. POSIX only; ignored
            # on Windows, which kills the tree with taskkill.
            start_new_session=True,
        )
    except OSError as exc:
        _cleanup_temp_files(launch.temp_files)
        # Windows reports an over-long command line as WinError 206, which arrives
        # as FileNotFoundError and would otherwise read as a missing CLI.
        if getattr(exc, "winerror", None) == 206:
            text = (
                f"{harness.label} could not be started: the command line exceeded the "
                "Windows 32767-character limit. The synthesized skills listing for "
                "this project is likely very large."
            )
        elif isinstance(exc, FileNotFoundError):
            text = (
                f"{harness.bin} CLI not found. Ensure it is installed and on PATH "
                "(or set its *_BIN variable)."
            )
        else:
            text = f"Failed to start {harness.bin}: {exc}"
        return RunResult(text=text, session_id=None, is_error=True)

    if conversation_key:
        register_process(conversation_key, proc)

    parser = harness.new_parser()
    try:
        raw_stdout, stderr, timed_out = _consume_stream(
            proc, effective_timeout, parser, on_event
        )
    finally:
        _cleanup_temp_files(launch.temp_files)
        if conversation_key:
            unregister_process(conversation_key)

    if timed_out:
        return RunResult(
            text=(
                f"{harness.label} produced no output for {effective_timeout}s and was "
                "stopped. Increase CLAUDE_TIMEOUT to wait longer."
            ),
            session_id=session_id,
            is_error=True,
        )

    if proc.returncode != 0:
        if conversation_key and was_cancelled(conversation_key):
            clear_cancelled(conversation_key)
            return RunResult(
                text="(stopped by user)",
                session_id=session_id,
                is_error=False,
            )
        error_text = (
            stderr.strip() or raw_stdout.strip()
            or f"{harness.bin} exited with code {proc.returncode}"
        )
        return RunResult(text=error_text, session_id=session_id, is_error=True)

    return parser.finish(raw_stdout, session_id)


def _consume_stream(
    proc: subprocess.Popen,
    timeout: int,
    parser: StreamParser,
    on_event: Callable[[StreamEvent], None] | None,
) -> tuple[str, str, bool]:
    """Read stdout line by line into `parser` until `proc` exits.

    stderr is drained on a helper thread so a chatty CLI can't deadlock on a
    full pipe while we're blocked reading stdout. Because that read blocks
    indefinitely on a hung CLI, a watchdog thread kills the process tree once
    `timeout` seconds pass *with no output at all*, which closes the pipe and
    unblocks us. The deadline is idle-based rather than wall-clock: a long run
    that keeps streaming steps is never cut off, so the timeout only fires on a
    genuinely silent process.

    Returns `(raw_stdout, stderr, timed_out)`.
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

    stderr_thread = threading.Thread(target=drain_stderr, name="agent-stderr", daemon=True)
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

    watchdog = threading.Thread(target=watch, name="agent-watchdog", daemon=True)
    watchdog.start()

    raw_lines: list[str] = []
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                raw_lines.append(line)
                touch()
                events = parser.feed(line)
                if on_event is not None:
                    for event in events:
                        try:
                            on_event(event)
                        except Exception:
                            logger.debug("Stream event callback failed", exc_info=True)
        proc.wait()
    finally:
        finished.set()
        watchdog.join(timeout=5)

    stderr_thread.join(timeout=5)
    return "".join(raw_lines), "".join(stderr_chunks), timed_out.is_set()
