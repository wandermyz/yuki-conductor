"""POSIX-only PTY bridge for the Zellij terminal WebSocket.

Imported only when `sys.platform != "win32"`. Pulls in `pty`, `fcntl`,
`termios`, and the process-group signalling that Windows can't provide.
"""

import asyncio
import concurrent.futures
import fcntl
import json
import logging
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from yuki_conductor.config import CLAUDE_WORKING_DIR

logger = logging.getLogger(__name__)

# Dedicated thread pool for PTY reads so they don't block the default executor
_pty_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=10, thread_name_prefix="pty"
)


def register_terminal_ws(api: FastAPI) -> None:
    """Register the /ws/terminal/{session_key} WebSocket route on `api`."""

    @api.websocket("/ws/terminal/{session_key:path}")
    async def terminal_websocket(websocket: WebSocket, session_key: str):
        """Bridge a Zellij session to a browser terminal via WebSocket + PTY."""
        await websocket.accept()

        if not session_key.startswith("zellij:"):
            await websocket.close(code=1008, reason="Not a Zellij session")
            return
        zellij_name = session_key[len("zellij:"):]

        # Wait for the initial resize message from the frontend so we can set
        # the PTY size before spawning zellij
        initial_msg = await websocket.receive()
        init_cols, init_rows = 80, 24
        if "text" in initial_msg:
            try:
                parsed = json.loads(initial_msg["text"])
                if parsed.get("type") == "resize":
                    init_cols = parsed.get("cols", 80)
                    init_rows = parsed.get("rows", 24)
            except (json.JSONDecodeError, ValueError):
                pass

        master_fd, slave_fd = pty.openpty()
        winsize = struct.pack("HHHH", init_rows, init_cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
        env = os.environ.copy()
        env.update({
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "SHELL": "/bin/zsh",
            "LC_ALL": "en_US.UTF-8",
            "LANG": "en_US.UTF-8",
        })
        try:
            proc = subprocess.Popen(
                ["zellij", "attach", zellij_name, "--create"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=CLAUDE_WORKING_DIR,
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError:
            os.close(master_fd)
            os.close(slave_fd)
            await websocket.close(code=1011, reason="zellij not found")
            return

        os.close(slave_fd)  # Only the master side is needed

        loop = asyncio.get_event_loop()
        stop_event = threading.Event()

        def _blocking_read():
            """Read from PTY with select() so we can be interrupted."""
            while not stop_event.is_set():
                ready, _, _ = select.select([master_fd], [], [], 0.5)
                if ready:
                    try:
                        data = os.read(master_fd, 4096)
                        if not data:
                            return None
                        return data
                    except OSError:
                        return None
            return None

        async def pty_reader():
            """Read from PTY and send to WebSocket."""
            try:
                while not stop_event.is_set():
                    data = await loop.run_in_executor(
                        _pty_executor, _blocking_read
                    )
                    if not data:
                        break
                    await websocket.send_bytes(data)
            except (OSError, WebSocketDisconnect):
                pass

        reader_task = asyncio.create_task(pty_reader())

        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                try:
                    if "bytes" in msg:
                        os.write(master_fd, msg["bytes"])
                    elif "text" in msg:
                        try:
                            parsed = json.loads(msg["text"])
                            if parsed.get("type") == "resize":
                                cols = parsed.get("cols", 80)
                                rows = parsed.get("rows", 24)
                                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                                try:
                                    os.killpg(os.getpgid(proc.pid), signal.SIGWINCH)
                                except (OSError, ProcessLookupError):
                                    pass
                                continue
                        except (json.JSONDecodeError, ValueError):
                            pass
                        os.write(master_fd, msg["text"].encode())
                except OSError:
                    logger.debug("PTY write failed, closing WebSocket", exc_info=True)
                    break
        except WebSocketDisconnect:
            pass
        finally:
            stop_event.set()
            reader_task.cancel()
            try:
                os.close(master_fd)
            except OSError:
                pass
            proc.terminate()
