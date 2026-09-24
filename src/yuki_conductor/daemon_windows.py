"""Windows daemon management — delegated to yuki-watcher.

There is no yuki-conductor-managed daemon on Windows. The
[yuki-watcher](C:/Git/yuki-watcher) WinForms app launches `yuki-conductor run`
and supervises it: if the process exits, yuki-watcher restarts it after a short
delay. That makes install/uninstall/status meaningless here, and makes "restart"
simply "exit" — the supervisor does the rest.
"""

import os
import sys
import threading

UNSUPPORTED_MESSAGE = (
    "`yuki-conductor daemon` is not supported on Windows. The daemon is "
    "launched and kept alive by yuki-watcher; use its Yuki Conductor tab to "
    "start, stop, and view the daemon, or the web UI's Restart daemon button."
)

# Give the HTTP response (and any in-flight reply) time to reach the client
# before the process disappears.
RESTART_DELAY_SECONDS = 3


def spawn_detached_restart(delay_seconds: int = RESTART_DELAY_SECONDS) -> None:
    """Restart by exiting; yuki-watcher relaunches the daemon.

    No detached helper process is needed: the supervisor is already watching
    this process. `os._exit` rather than `sys.exit` because this runs on a
    timer thread, where a raised SystemExit would only unwind that thread.
    """
    threading.Timer(delay_seconds, lambda: os._exit(0)).start()


def handle_daemon(action: str) -> None:
    print(UNSUPPORTED_MESSAGE, file=sys.stderr)
    sys.exit(1)
