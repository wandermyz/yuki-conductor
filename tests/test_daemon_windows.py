"""Tests for the Windows Task Scheduler daemon adapter.

These mock ``subprocess.run`` so they run on any platform (including macOS CI).
"""

from unittest import mock

import pytest

from yuki_conductor import daemon_windows


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch, tmp_path):
    monkeypatch.setenv("USERDOMAIN", "TESTDOM")
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setattr(daemon_windows, "_pwsh", lambda: "C:\\pwsh.exe")


def test_current_user_from_env(monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "DOM")
    monkeypatch.setenv("USERNAME", "alice")
    assert daemon_windows._current_user() == "DOM\\alice"


def test_generate_task_xml_contains_key_fields():
    xml = daemon_windows._generate_task_xml()
    assert '<?xml version="1.0" encoding="UTF-16"?>' in xml
    assert "<LogonTrigger>" in xml
    assert "<UserId>TESTDOM\\tester</UserId>" in xml
    assert "C:\\pwsh.exe" in xml
    assert "yuki-conductor-daemon.ps1" in xml
    assert "<RestartOnFailure>" in xml
    assert "IgnoreNew" in xml


def test_install_creates_and_runs_task():
    with mock.patch.object(daemon_windows, "_schtasks") as sch:
        sch.return_value = mock.Mock(returncode=0, stdout="")
        daemon_windows._install()
    calls = [c.args[0] for c in sch.call_args_list]
    assert any(a[:2] == ["/Create", "/TN"] for a in calls)
    assert any(a[:2] == ["/Run", "/TN"] for a in calls)


def test_uninstall_when_not_installed(capsys):
    with mock.patch.object(daemon_windows, "_task_installed", return_value=False):
        daemon_windows._uninstall()
    assert "not installed" in capsys.readouterr().out.lower()


def test_uninstall_deletes_task():
    with (
        mock.patch.object(daemon_windows, "_task_installed", return_value=True),
        mock.patch.object(daemon_windows, "_kill_daemon_processes", return_value=0),
        mock.patch.object(daemon_windows, "_schtasks") as sch,
    ):
        sch.return_value = mock.Mock(returncode=0, stdout="")
        daemon_windows._uninstall()
    calls = [c.args[0] for c in sch.call_args_list]
    assert any(a[:2] == ["/Delete", "/TN"] for a in calls)


def test_status_running(capsys):
    out = "TaskName: \\YukiConductor\nStatus:                Running\n"
    with mock.patch.object(daemon_windows, "_schtasks") as sch:
        sch.return_value = mock.Mock(returncode=0, stdout=out)
        daemon_windows._status()
    assert "Running" in capsys.readouterr().out


def test_status_not_installed(capsys):
    with mock.patch.object(daemon_windows, "_schtasks") as sch:
        sch.return_value = mock.Mock(returncode=1, stdout="")
        daemon_windows._status()
    assert "Not installed" in capsys.readouterr().out


def test_restart_not_installed_exits():
    with mock.patch.object(daemon_windows, "_task_installed", return_value=False):
        with pytest.raises(SystemExit):
            daemon_windows._restart()


def _proc(pid, ppid, cmdline):
    return {"pid": pid, "ppid": ppid, "cmdline": cmdline}


def test_daemon_pids_finds_whole_tree_and_skips_self(monkeypatch):
    procs = [
        _proc(100, 1, 'pwsh -File "C:\\repo\\bin\\yuki-conductor-daemon.ps1"'),
        _proc(200, 100, "uv.exe run --project C:\\repo yuki-conductor run"),
        _proc(300, 200, '"C:\\repo\\.venv\\Scripts\\yuki-conductor.exe" run'),
        _proc(400, 1, "notepad.exe"),
        # the restart command itself, plus its uv parent — must be spared
        _proc(500, 1, "uv.exe run yuki-conductor daemon restart"),
        _proc(600, 500, "yuki-conductor.exe daemon restart"),
    ]
    monkeypatch.setattr(daemon_windows, "_list_processes", lambda: procs)
    monkeypatch.setattr(daemon_windows.os, "getpid", lambda: 600)
    assert sorted(daemon_windows._daemon_pids()) == [100, 200, 300]


def test_daemon_pids_spares_own_ancestors(monkeypatch):
    """A caller spawned *by* the daemon must not kill the daemon out from under itself."""
    procs = [
        _proc(100, 1, "uv.exe run --project C:\\repo yuki-conductor run"),
        _proc(200, 100, '"C:\\repo\\.venv\\Scripts\\yuki-conductor.exe" run'),
        _proc(300, 200, "claude.sh -p ..."),
    ]
    monkeypatch.setattr(daemon_windows, "_list_processes", lambda: procs)
    monkeypatch.setattr(daemon_windows.os, "getpid", lambda: 300)
    killable, ancestral = daemon_windows._classify_daemon_pids()
    assert killable == []
    assert sorted(ancestral) == [100, 200]


def test_kill_warns_when_running_inside_daemon(monkeypatch, capsys):
    monkeypatch.setattr(
        daemon_windows, "_classify_daemon_pids", lambda: ([], [100, 200])
    )
    with mock.patch.object(daemon_windows.subprocess, "run") as run:
        assert daemon_windows._kill_daemon_processes() == 0
    run.assert_not_called()
    assert "Refusing to kill" in capsys.readouterr().err


def test_daemon_pids_excludes_daemon_subcommands(monkeypatch):
    procs = [_proc(10, 1, "yuki-conductor.exe daemon status")]
    monkeypatch.setattr(daemon_windows, "_list_processes", lambda: procs)
    monkeypatch.setattr(daemon_windows.os, "getpid", lambda: 999)
    assert daemon_windows._daemon_pids() == []


def test_kill_daemon_processes_uses_tree_kill(monkeypatch):
    monkeypatch.setattr(
        daemon_windows, "_classify_daemon_pids", lambda: ([100, 200], [])
    )
    with mock.patch.object(daemon_windows.subprocess, "run") as run:
        killed = daemon_windows._kill_daemon_processes()
    assert killed == 2
    for call, pid in zip(run.call_args_list, ["100", "200"], strict=True):
        assert call.args[0] == ["taskkill", "/PID", pid, "/T", "/F"]


def test_restart_kills_tree_before_running(monkeypatch):
    order = []
    monkeypatch.setattr(daemon_windows, "_task_installed", lambda: True)
    monkeypatch.setattr(daemon_windows, "build_web_frontend", lambda: None)
    monkeypatch.setattr(
        daemon_windows,
        "_kill_daemon_processes",
        lambda: (order.append("kill"), 2)[1],
    )
    monkeypatch.setattr(
        daemon_windows, "_wait_for_log_release", lambda: order.append("wait") or True
    )
    with mock.patch.object(daemon_windows, "_schtasks") as sch:
        sch.side_effect = lambda args, **kw: (
            order.append(args[0]),
            mock.Mock(returncode=0, stdout=""),
        )[1]
        daemon_windows._restart()
    # Old tree must be dead and the log released before the new instance starts.
    assert order == ["/End", "kill", "wait", "/Run"]


def test_wait_for_log_release_returns_when_openable(monkeypatch, tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text("x")
    monkeypatch.setattr(daemon_windows, "LOG_FILE", log)
    assert daemon_windows._wait_for_log_release(timeout=1.0) is True


def test_wait_for_log_release_times_out(monkeypatch, tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text("x")
    monkeypatch.setattr(daemon_windows, "LOG_FILE", log)
    import builtins

    real_open = builtins.open

    def locked(path, *a, **kw):
        if str(path) == str(log):
            raise PermissionError(13, "locked")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", locked)
    assert daemon_windows._wait_for_log_release(timeout=0.1) is False
