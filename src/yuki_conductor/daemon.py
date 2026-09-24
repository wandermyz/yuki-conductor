"""Daemon management dispatcher.

Routes `yuki-conductor daemon <action>` to the platform-specific adapter.
macOS manages its own daemon as a LaunchAgent (`daemon_macos`). Windows does
not: yuki-watcher supervises the process there, so `daemon_windows` reports the
subcommand as unsupported and implements restart as a plain exit.
"""

import sys


def handle_daemon(action: str) -> None:
    _impl().handle_daemon(action)


def spawn_detached_restart() -> None:
    """Restart the daemon from *inside* it, without killing the caller.

    On macOS the restart is handed to a process outside this one's tree, since
    bouncing the LaunchAgent inline would kill the web server mid-response. On
    Windows the process simply schedules its own exit and yuki-watcher relaunches
    it. Either way this returns as soon as the restart is committed; it says
    nothing about whether it succeeds, and the caller polls for the daemon
    coming back.
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
