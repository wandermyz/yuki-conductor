"""Linux daemon management — a systemd user service.

The unit lives in `~/.config/systemd/user/` and is driven with
`systemctl --user`, so no root is needed. It starts with the user's systemd
manager, i.e. at login — or at boot if lingering is enabled
(`loginctl enable-linger`).
"""

import subprocess
import sys
import threading
from pathlib import Path

from yuki_conductor.config import DATA_DIR, ERR_LOG_FILE, LOG_FILE
from yuki_conductor.daemon_common import build_web_frontend, find_uv, print_logs, project_dir

UNIT_NAME = "yuki-conductor.service"
UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME

# Give the HTTP response (and any in-flight reply) time to reach the client
# before systemd stops the process.
RESTART_DELAY_SECONDS = 3


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], check=check)


def _generate_unit() -> str:
    uv = find_uv()
    proj_dir = project_dir()

    # Build PATH that includes common locations for claude binary
    home = Path.home()
    extra_paths = [
        str(home / ".local" / "bin"),
        str(home / ".cargo" / "bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    path_value = ":".join(extra_paths)

    return f"""\
[Unit]
Description=yuki-conductor agent conductor
After=network-online.target

[Service]
WorkingDirectory={proj_dir}
Environment=PATH={path_value}
ExecStart={uv} run --project {proj_dir} yuki-conductor run
Restart=always
RestartSec=3
StandardOutput=append:{LOG_FILE}
StandardError=append:{ERR_LOG_FILE}

[Install]
WantedBy=default.target
"""


def _install():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(_generate_unit())
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", UNIT_NAME)
    print(f"Installed and started {UNIT_NAME}")
    print(f"Unit:  {UNIT_PATH}")
    print(f"Logs:  {LOG_FILE}")
    print("To keep it running while logged out: loginctl enable-linger")


def _uninstall():
    if UNIT_PATH.exists():
        _systemctl("disable", "--now", UNIT_NAME, check=False)
        UNIT_PATH.unlink()
        _systemctl("daemon-reload", check=False)
        print(f"Stopped and removed {UNIT_NAME}")
    else:
        print("systemd user service not installed")


def _restart():
    if UNIT_PATH.exists():
        build_web_frontend()
        _systemctl("restart", UNIT_NAME)
        print(f"Restarted {UNIT_NAME}")
    else:
        print("systemd user service not installed. Run 'daemon install' first.")
        sys.exit(1)


def spawn_detached_restart(delay_seconds: int = RESTART_DELAY_SECONDS) -> None:
    """Ask systemd to restart the service a few seconds from now.

    Unlike macOS, detaching a helper with `setsid` would not help: the default
    `KillMode=control-group` kills every process in the unit's cgroup, helper
    included. Instead, a timer thread queues the job with `--no-block`, so the
    restart is owned by the systemd manager and completes even though it kills
    this process.
    """
    if not UNIT_PATH.exists():
        raise RuntimeError("systemd user service not installed. Run 'daemon install' first.")

    threading.Timer(
        delay_seconds,
        lambda: _systemctl("restart", "--no-block", UNIT_NAME, check=False),
    ).start()


def _status():
    result = subprocess.run(
        ["systemctl", "--user", "is-active", UNIT_NAME],
        capture_output=True,
        text=True,
    )
    state = result.stdout.strip()
    if state == "active":
        pid = subprocess.run(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", UNIT_NAME],
            capture_output=True,
            text=True,
        ).stdout.strip()
        print(f"Running: {UNIT_NAME} (pid {pid})")
    else:
        print(f"Not running ({state or 'unknown'})")


def _log():
    print_logs()


def handle_daemon(action: str) -> None:
    actions = {
        "install": _install,
        "uninstall": _uninstall,
        "restart": _restart,
        "status": _status,
        "log": _log,
    }
    actions[action]()
