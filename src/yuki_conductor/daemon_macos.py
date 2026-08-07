"""macOS LaunchAgent daemon management."""

import plistlib
import subprocess
import sys
from pathlib import Path

from yuki_conductor.config import DATA_DIR, ERR_LOG_FILE, LOG_FILE
from yuki_conductor.daemon_common import build_web_frontend, find_uv, print_logs, project_dir

PLIST_LABEL = "com.user.yuki-conductor"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def _generate_plist() -> bytes:
    uv = find_uv()
    proj_dir = str(project_dir())

    # Build PATH that includes common locations for claude binary
    home = Path.home()
    extra_paths = [
        str(home / ".local" / "bin"),
        str(home / ".cargo" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    path_value = ":".join(extra_paths)

    plist = {
        "Label": PLIST_LABEL,
        "ProgramArguments": [uv, "run", "--project", proj_dir, "yuki-conductor", "run"],
        "KeepAlive": True,
        "RunAtLoad": True,
        "WorkingDirectory": proj_dir,
        "EnvironmentVariables": {"PATH": path_value},
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(ERR_LOG_FILE),
    }
    return plistlib.dumps(plist)


def _install():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_bytes(_generate_plist())
    subprocess.run(["launchctl", "load", str(PLIST_PATH)], check=True)
    print(f"Installed and loaded {PLIST_LABEL}")
    print(f"Plist: {PLIST_PATH}")
    print(f"Logs:  {LOG_FILE}")


def _uninstall():
    if PLIST_PATH.exists():
        subprocess.run(["launchctl", "unload", str(PLIST_PATH)], check=False)
        PLIST_PATH.unlink()
        print(f"Unloaded and removed {PLIST_LABEL}")
    else:
        print("LaunchAgent not installed")


def _restart():
    if PLIST_PATH.exists():
        build_web_frontend()
        subprocess.run(["launchctl", "unload", str(PLIST_PATH)], check=False)
        subprocess.run(["launchctl", "load", str(PLIST_PATH)], check=True)
        print(f"Restarted {PLIST_LABEL}")
    else:
        print("LaunchAgent not installed. Run 'daemon install' first.")
        sys.exit(1)


def _status():
    result = subprocess.run(
        ["launchctl", "list"],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if PLIST_LABEL in line:
            print(f"Running: {line}")
            return
    print("Not running")


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
