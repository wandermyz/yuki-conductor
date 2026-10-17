"""Tests for the Linux (systemd user service) daemon adapter."""

import pytest

from yuki_conductor import daemon, daemon_linux


@pytest.fixture
def unit_path(tmp_path, monkeypatch):
    path = tmp_path / "yuki-conductor.service"
    monkeypatch.setattr(daemon_linux, "UNIT_PATH", path)
    return path


@pytest.fixture
def systemctl_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        daemon_linux, "_systemctl", lambda *args, check=True: calls.append(args)
    )
    return calls


def test_dispatcher_picks_linux_adapter(monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "linux")
    assert daemon._impl() is daemon_linux


def test_generated_unit_runs_the_daemon_and_restarts_it(monkeypatch):
    monkeypatch.setattr(daemon_linux, "find_uv", lambda: "/opt/uv")
    unit = daemon_linux._generate_unit()
    proj = daemon_linux.project_dir()
    assert f"ExecStart=/opt/uv run --project {proj} yuki-conductor run" in unit
    assert "Restart=always" in unit
    assert f"StandardOutput=append:{daemon_linux.LOG_FILE}" in unit
    assert "WantedBy=default.target" in unit


def test_install_writes_unit_and_enables_it(unit_path, systemctl_calls, monkeypatch):
    monkeypatch.setattr(daemon_linux, "find_uv", lambda: "/opt/uv")
    daemon_linux.handle_daemon("install")
    assert unit_path.read_text() == daemon_linux._generate_unit()
    assert systemctl_calls == [
        ("daemon-reload",),
        ("enable", "--now", daemon_linux.UNIT_NAME),
    ]


def test_uninstall_disables_and_removes_unit(unit_path, systemctl_calls):
    unit_path.write_text("[Unit]\n")
    daemon_linux.handle_daemon("uninstall")
    assert not unit_path.exists()
    assert systemctl_calls[0] == ("disable", "--now", daemon_linux.UNIT_NAME)


def test_restart_without_unit_exits(unit_path, systemctl_calls):
    with pytest.raises(SystemExit) as exc:
        daemon_linux.handle_daemon("restart")
    assert exc.value.code == 1
    assert systemctl_calls == []


def test_spawn_detached_restart_requires_unit(unit_path):
    with pytest.raises(RuntimeError, match="not installed"):
        daemon_linux.spawn_detached_restart()


def test_spawn_detached_restart_queues_a_nonblocking_restart(
    unit_path, systemctl_calls, monkeypatch
):
    """The restart is delayed so the response gets out, and handed to systemd
    with --no-block so it survives systemd killing this process."""
    unit_path.write_text("[Unit]\n")
    timers = []

    class FakeTimer:
        def __init__(self, delay, fn):
            self.delay, self.fn = delay, fn

        def start(self):
            timers.append(self)

    monkeypatch.setattr(daemon_linux.threading, "Timer", FakeTimer)

    daemon_linux.spawn_detached_restart()

    assert len(timers) == 1
    assert timers[0].delay == daemon_linux.RESTART_DELAY_SECONDS
    assert systemctl_calls == []
    timers[0].fn()
    assert systemctl_calls == [("restart", "--no-block", daemon_linux.UNIT_NAME)]
