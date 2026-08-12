"""Windows Task Scheduler daemon management.

The Windows equivalent of the macOS per-user LaunchAgent is a per-user
Scheduled Task triggered `OnLogon` with `RestartOnFailure`. It runs in the
user's session (so uv, claude, and the user's `~/.yuki-conductor/.env` are all
available) and does not require admin elevation.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from yuki_conductor.config import DATA_DIR, LOG_FILE
from yuki_conductor.daemon_common import build_web_frontend, print_logs, project_dir

TASK_NAME = "YukiConductor"

# How long to wait for the old daemon to release its grip on daemon.log before
# relaunching. The new process opens the log with a plain FileHandler, which
# fails outright on Windows while another process holds the handle.
LOG_RELEASE_TIMEOUT = 15.0


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


def _list_processes() -> list[dict]:
    """Return [{pid, ppid, cmdline}] for every process visible to this user."""
    ps = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [_pwsh(), "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    return [
        {
            "pid": p.get("ProcessId"),
            "ppid": p.get("ParentProcessId"),
            "cmdline": p.get("CommandLine") or "",
        }
        for p in raw
        if p.get("ProcessId")
    ]


# `yuki-conductor run`, however it was launched: directly, via the .exe
# shim, or as `uv run --project <repo> yuki-conductor run`.
_DAEMON_CMD_RE = re.compile(r"yuki-conductor(\.exe)?\"?\s+run(\s|$)", re.IGNORECASE)


def _self_ancestry(procs: list[dict]) -> set[int]:
    """PIDs of this process and all of its ancestors."""
    by_pid = {p["pid"]: p for p in procs}
    chain: set[int] = set()
    pid = os.getpid()
    while pid and pid not in chain:
        chain.add(pid)
        parent = by_pid.get(pid)
        pid = parent["ppid"] if parent else None
    return chain


def _classify_daemon_pids() -> tuple[list[int], list[int]]:
    """Split running daemon PIDs into (killable, in_own_ancestry).

    `daemon restart` is itself often run as `yuki-conductor daemon restart`
    under `uv run`; that chain never matches the daemon pattern, so it is not
    at risk. But a Claude session *spawned by* the daemon has the daemon as an
    ancestor — killing it there would take out the restart command mid-flight,
    before `schtasks /Run` ever fires. Those PIDs are reported separately.
    """
    procs = _list_processes()
    own = _self_ancestry(procs)
    launcher = _launcher_script().name.lower()
    killable, ancestral = [], []
    for proc in procs:
        cmd = proc["cmdline"]
        if launcher in cmd.lower() or _DAEMON_CMD_RE.search(cmd):
            (ancestral if proc["pid"] in own else killable).append(proc["pid"])
    return killable, ancestral


def _daemon_pids() -> list[int]:
    """PIDs of the running daemon that this process may safely kill."""
    return _classify_daemon_pids()[0]


def _kill_daemon_processes() -> int:
    """Force-kill the daemon process tree(s). Returns the number of PIDs killed.

    `schtasks /End` only stops the task's top-level pwsh; the `uv` →
    `yuki-conductor` → `python` descendants are re-parented and survive, keeping
    a lock on daemon.log and continuing to serve chat traffic with stale code.
    """
    pids, ancestral = _classify_daemon_pids()
    if ancestral:
        print(
            f"Refusing to kill daemon process(es) {ancestral} — this command is "
            "running inside them. Restart from a shell outside the daemon.",
            file=sys.stderr,
        )
    for pid in pids:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    return len(pids)


def _wait_for_log_release(timeout: float = LOG_RELEASE_TIMEOUT) -> bool:
    """Block until daemon.log can be opened for append, or `timeout` elapses."""
    if not LOG_FILE.exists():
        return True
    deadline = time.monotonic() + timeout
    while True:
        try:
            with open(LOG_FILE, "a", encoding="utf-8"):
                return True
        except PermissionError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)


def _stop_daemon() -> None:
    """End the scheduled task and kill any surviving daemon processes."""
    _schtasks(["/End", "/TN", TASK_NAME], check=False)
    killed = _kill_daemon_processes()
    if killed:
        print(f"Killed {killed} lingering daemon process(es)")


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
    _stop_daemon()
    _schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    print(f"Removed scheduled task {TASK_NAME}")


def _restart():
    if not _task_installed():
        print("Scheduled task not installed. Run 'daemon install' first.")
        sys.exit(1)
    build_web_frontend()
    _stop_daemon()
    if not _wait_for_log_release():
        print(
            f"Warning: {LOG_FILE} is still locked after {LOG_RELEASE_TIMEOUT:.0f}s; "
            "the restarted daemon may fail to start.",
            file=sys.stderr,
        )
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
