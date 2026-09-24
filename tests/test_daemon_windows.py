"""Tests for the Windows daemon adapter.

Windows has no yuki-conductor-managed daemon: yuki-watcher supervises the
process, so the subcommand is unsupported and restart is a self-exit.
"""

import pytest

from yuki_conductor import daemon_windows


@pytest.mark.parametrize(
    "action", ["install", "uninstall", "restart", "status", "log"]
)
def test_every_daemon_action_is_unsupported(action, capsys):
    with pytest.raises(SystemExit) as exc:
        daemon_windows.handle_daemon(action)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "not supported on Windows" in err
    assert "yuki-watcher" in err


def test_spawn_detached_restart_exits_the_process(monkeypatch):
    """Restart = exit; yuki-watcher relaunches. No detached helper involved."""
    timers = []

    class FakeTimer:
        def __init__(self, delay, fn):
            self.delay, self.fn = delay, fn

        def start(self):
            timers.append(self)

    monkeypatch.setattr(daemon_windows.threading, "Timer", FakeTimer)
    exited = []
    monkeypatch.setattr(daemon_windows.os, "_exit", exited.append)

    daemon_windows.spawn_detached_restart()

    assert len(timers) == 1
    # The response must get out before the process dies.
    assert timers[0].delay == daemon_windows.RESTART_DELAY_SECONDS
    assert not exited
    timers[0].fn()
    assert exited == [0]
