"""Daemon management dispatcher.

Routes `yuki-conductor daemon <action>` to the platform-specific adapter:
macOS uses a LaunchAgent (`daemon_macos`), Windows uses a Task Scheduler task
(`daemon_windows`).
"""

import sys


def handle_daemon(action: str) -> None:
    if sys.platform == "darwin":
        from yuki_conductor.daemon_macos import handle_daemon as impl
    elif sys.platform == "win32":
        from yuki_conductor.daemon_windows import handle_daemon as impl
    else:
        raise RuntimeError(f"Unsupported platform for daemon management: {sys.platform}")
    impl(action)
