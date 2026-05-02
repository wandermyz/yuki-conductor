"""macOS LaunchAgent daemon management."""

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from yuki_conductor.config import DATA_DIR, ERR_LOG_FILE, LOG_FILE, PLIST_LABEL, PLIST_PATH


def _find_uv() -> str:
    uv_path = shutil.which("uv")
    if not uv_path:
        raise RuntimeError("uv not found on PATH")
    return uv_path


def _project_dir() -> str:
    return str(Path(__file__).resolve().parent.parent.parent)


def _build_web_frontend() -> None:
    """Run `pnpm install --frozen-lockfile && pnpm build` in web/ so the served
    dist matches the current source. Failures are logged but don't abort the
    restart — the daemon will keep serving whatever dist already exists.
    """
    web_dir = Path(_project_dir()) / "web"
    if not (web_dir / "package.json").exists():
        return
    pnpm = shutil.which("pnpm")
    if not pnpm:
        print("pnpm not on PATH; skipping web build")
        return
    try:
        print("Installing web dependencies...")
        subprocess.run(
            [pnpm, "install", "--frozen-lockfile"],
            cwd=web_dir,
            check=True,
        )
        print("Building web frontend...")
        subprocess.run([pnpm, "build"], cwd=web_dir, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"Web frontend build failed: {exc}; previous dist will be served")


def _generate_plist() -> bytes:
    uv = _find_uv()
    project_dir = _project_dir()

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
        "ProgramArguments": [uv, "run", "--project", project_dir, "yuki-conductor", "run"],
        "KeepAlive": True,
        "RunAtLoad": True,
        "WorkingDirectory": project_dir,
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
        _build_web_frontend()
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
    print(f"Stdout: {LOG_FILE}")
    print(f"Stderr: {ERR_LOG_FILE}")
    if LOG_FILE.exists():
        print(f"\n--- Last 20 lines of {LOG_FILE.name} ---")
        lines = LOG_FILE.read_text().splitlines()
        for line in lines[-20:]:
            print(line)


def handle_daemon(action: str) -> None:
    actions = {
        "install": _install,
        "uninstall": _uninstall,
        "restart": _restart,
        "status": _status,
        "log": _log,
    }
    actions[action]()
