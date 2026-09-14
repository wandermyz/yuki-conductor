"""Daemon management dispatcher.

Routes `yuki-conductor daemon <action>` to the platform-specific adapter:
macOS uses a LaunchAgent (`daemon_macos`), Windows uses a Task Scheduler task
(`daemon_windows`).
"""

import sys


def handle_daemon(action: str) -> None:
    _impl().handle_daemon(action)


def spawn_detached_restart() -> None:
    """Restart the daemon from *inside* it, without killing the caller.

    ``handle_daemon("restart")`` kills every yuki-conductor process, which from
    the web server's own thread means killing itself mid-response. So the
    restart is handed to a process outside this one's tree — the same mechanism
    the `yuki-conductor-restart` skill relies on — and this call returns as soon
    as that process exists. It says nothing about whether the restart succeeds;
    the caller polls for the daemon coming back.
    """
    _impl().spawn_detached_restart()


def _impl():
    if sys.platform == "darwin":
        from yuki_conductor import daemon_macos

        return daemon_macos
    if sys.platform == "win32":
        from yuki_conductor import daemon_windows

        return daemon_windows
    raise RuntimeError(f"Unsupported platform for daemon management: {sys.platform}")
