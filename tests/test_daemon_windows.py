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
