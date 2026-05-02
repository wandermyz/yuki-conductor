"""Zellij session lifecycle management."""

import logging
import os
import re
import subprocess

from yuki_conductor.config import CLAUDE_WORKING_DIR

logger = logging.getLogger(__name__)


def create_session(name: str, worktree: str, working_dir: str | None = None,
                   resume: bool = False) -> None:
    """Create a detached Zellij session running Claude Code.

    If resume=True, uses `claude --continue --worktree` to resume the
    most recent conversation in that worktree.
    """
    cwd = working_dir or CLAUDE_WORKING_DIR
    env = os.environ.copy()
    env.update({
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "SHELL": "/bin/zsh",
        "LC_ALL": "en_US.UTF-8",
        "LANG": "en_US.UTF-8",
    })
    try:
        # Create detached session
        subprocess.run(
            ["zellij", "attach", name, "--create-background"],
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=10,
        )
        # Send the claude command into the session's terminal
        import time
        time.sleep(0.5)  # Give the session a moment to initialise
        if resume:
            claude_cmd = f"claude --continue --worktree {worktree}\n"
        else:
            claude_cmd = f"claude --worktree {worktree}\n"
        subprocess.run(
            ["zellij", "--session", name, "action", "write-chars", claude_cmd],
            capture_output=True,
            timeout=5,
        )
        logger.info("Created Zellij session %r (worktree=%r, resume=%s)", name, worktree, resume)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.error("Failed to create Zellij session %r: %s", name, e)
        raise


def list_sessions() -> list[str]:
    """Return names of active (non-exited) Zellij sessions."""
    try:
        result = subprocess.run(
            ["zellij", "list-sessions"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        alive = []
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            # EXITED sessions contain "EXITED" in the line
            if "EXITED" in line:
                continue
            # First word (possibly with ANSI codes) is the session name
            # Strip ANSI escape codes to get the name
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
            name = clean.split()[0] if clean.split() else ""
            if name:
                alive.append(name)
        return alive
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def is_session_alive(name: str) -> bool:
    """Check if a named Zellij session is running."""
    return name in list_sessions()


def kill_session(name: str) -> None:
    """Kill a Zellij session by name, then delete the exited entry."""
    try:
        subprocess.run(
            ["zellij", "kill-session", name],
            capture_output=True, timeout=5,
        )
        # Delete the exited session so it doesn't linger
        subprocess.run(
            ["zellij", "delete-session", name],
            capture_output=True, timeout=5,
        )
        logger.info("Killed Zellij session %r", name)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning("Failed to kill Zellij session %r", name)
