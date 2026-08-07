"""Windows Task Scheduler daemon management.

The Windows equivalent of the macOS per-user LaunchAgent is a per-user
Scheduled Task triggered `OnLogon` with `RestartOnFailure`. It runs in the
user's session (so uv, claude, and the user's `~/.yuki-conductor/.env` are all
available) and does not require admin elevation.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from yuki_conductor.config import DATA_DIR, LOG_FILE
from yuki_conductor.daemon_common import build_web_frontend, print_logs, project_dir

TASK_NAME = "YukiConductor"


def _current_user() -> str:
    """Return DOMAIN\\user for the current interactive user."""
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME")
    if domain and user:
        return f"{domain}\\{user}"
    result = subprocess.run(
        ["whoami"], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return result.stdout.strip()


def _pwsh() -> str:
    """Locate the PowerShell executable (prefer pwsh 7+, fall back to Windows PowerShell)."""
    import shutil

    for exe in ("pwsh", "powershell"):
        found = shutil.which(exe)
        if found:
            return found
    raise RuntimeError("Neither pwsh nor powershell found on PATH")


def _launcher_script() -> Path:
    return project_dir() / "bin" / "yuki-conductor-daemon.ps1"


def _generate_task_xml() -> str:
    user = _current_user()
    pwsh = _pwsh()
    script = _launcher_script()
    repo = str(project_dir())
    arguments = f'-NoProfile -WindowStyle Hidden -File "{script}"'
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>yuki-conductor chat-to-Claude daemon</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{user}</UserId>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{user}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RestartOnFailure>\n"
        "      <Interval>PT1M</Interval>\n"
        "      <Count>9999</Count>\n"
        "    </RestartOnFailure>\n"
        "    <Hidden>false</Hidden>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        "  <Actions>\n"
        "    <Exec>\n"
        f"      <Command>{pwsh}</Command>\n"
        f"      <Arguments>{arguments}</Arguments>\n"
        f"      <WorkingDirectory>{repo}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _schtasks(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _task_installed() -> bool:
    result = _schtasks(["/Query", "/TN", TASK_NAME], check=False)
    return result.returncode == 0


def _install():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    xml = _generate_task_xml()
    # schtasks /XML requires a UTF-16 encoded file matching the XML declaration.
    fd, xml_path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(xml_path).write_text(xml, encoding="utf-16")
        _schtasks(["/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"])
    finally:
        os.unlink(xml_path)
    _schtasks(["/Run", "/TN", TASK_NAME], check=False)
    print(f"Installed and started scheduled task {TASK_NAME}")
    print(f"Logs: {LOG_FILE}")


def _uninstall():
    if not _task_installed():
        print("Scheduled task not installed")
        return
    _schtasks(["/End", "/TN", TASK_NAME], check=False)
    _schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    print(f"Removed scheduled task {TASK_NAME}")


def _restart():
    if not _task_installed():
        print("Scheduled task not installed. Run 'daemon install' first.")
        sys.exit(1)
    build_web_frontend()
    _schtasks(["/End", "/TN", TASK_NAME], check=False)
    _schtasks(["/Run", "/TN", TASK_NAME])
    print(f"Restarted scheduled task {TASK_NAME}")


def _status():
    result = _schtasks(["/Query", "/TN", TASK_NAME, "/FO", "LIST"], check=False)
    if result.returncode != 0:
        print("Not installed")
        return
    for line in result.stdout.splitlines():
        if line.strip().startswith("Status:"):
            status = line.split(":", 1)[1].strip()
            print(f"Status: {status}")
            return
    print("Installed (status unknown)")


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
