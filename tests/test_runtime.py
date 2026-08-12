"""Tests for the process-level runtime orchestrator."""

import importlib.metadata
import threading
from unittest.mock import MagicMock, patch


class FakeReceiver:
    def __init__(self, name: str) -> None:
        self.name = name
        self.platform = type("P", (), {"name": name})()
        self.started = False
        self.startup_complete = False

    def start(self) -> None:
        self.started = True

    def on_startup_complete(self) -> None:
        self.startup_complete = True

    def stop(self) -> None:
        pass


def _patch_block_forever():
    """Replace `threading.Event().wait()` so `runtime.start()` returns."""
    return patch.object(threading.Event, "wait", lambda self, timeout=None: None)


def test_no_apps_starts_only_web_and_cron():
    with (
        _patch_block_forever(),
        patch("yuki_conductor.runtime.chat_apps", return_value=[]),
        patch("yuki_conductor.web_server.start_web_server") as web_mock,
        patch("yuki_conductor.cron_scheduler.start_cron_scheduler") as cron_mock,
    ):
        from yuki_conductor.runtime import start

        start()

    web_mock.assert_called_once()
    cron_mock.assert_called_once()
    assert cron_mock.call_args.kwargs["platforms_by_name"] == {}


def test_slack_only_builds_slack_receiver():
    fake = FakeReceiver("slack")
    with (
        _patch_block_forever(),
        patch("yuki_conductor.runtime.chat_apps", return_value=["slack_socket"]),
        patch("yuki_conductor.runtime._build_receiver", return_value=fake) as build,
        patch("yuki_conductor.web_server.start_web_server"),
        patch("yuki_conductor.cron_scheduler.start_cron_scheduler") as cron_mock,
    ):
        from yuki_conductor.runtime import start

        start()

    # First arg is the name string; remaining are stores
    assert build.call_args[0][0] == "slack_socket"
    assert fake.started is True
    assert fake.startup_complete is True
    assert cron_mock.call_args.kwargs["platforms_by_name"] == {"slack": fake.platform}


def test_entry_point_plugin_discovery():
    """Plugins registered via entry points are discovered by _build_receiver."""
    fake = FakeReceiver("my_plugin")

    mock_ep = MagicMock(spec=importlib.metadata.EntryPoint)
    mock_ep.name = "my_plugin"
    mock_ep.load.return_value = lambda **kw: fake

    with patch(
        "importlib.metadata.entry_points",
        return_value=[mock_ep],
    ):
        from yuki_conductor.runtime import _build_receiver
        from yuki_conductor.store import ModelStore, SessionStore

        result = _build_receiver("my_plugin", SessionStore(), ModelStore())

    assert result is fake
    mock_ep.load.assert_called_once()


def test_open_log_handler_retries_then_gives_up(tmp_path, capsys):
    """A locked log must not kill startup — the restart notification still matters."""
    from yuki_conductor.runtime import _open_log_handler

    log = tmp_path / "daemon.log"
    with patch("logging.FileHandler", side_effect=PermissionError(13, "locked")):
        handler = _open_log_handler(log, attempts=3, delay=0)

    assert handler is None
    assert "locked by another process" in capsys.readouterr().err


def test_open_log_handler_succeeds_after_transient_lock(tmp_path):
    from yuki_conductor.runtime import _open_log_handler

    log = tmp_path / "daemon.log"
    sentinel = MagicMock()
    with patch(
        "logging.FileHandler",
        side_effect=[PermissionError(13, "locked"), sentinel],
    ):
        assert _open_log_handler(log, attempts=3, delay=0) is sentinel


def test_start_sends_restart_notification():
    fake = FakeReceiver("teams_mcp")
    with (
        _patch_block_forever(),
        patch("yuki_conductor.runtime.chat_apps", return_value=["teams_mcp"]),
        patch("yuki_conductor.runtime._build_receiver", return_value=fake),
        patch("yuki_conductor.runtime._get_commit_short", return_value="abc1234"),
        patch("yuki_conductor.web_server.start_web_server"),
        patch("yuki_conductor.cron_scheduler.start_cron_scheduler"),
    ):
        fake.platform.send_notification = MagicMock()
        from yuki_conductor.runtime import start

        start()

    fake.platform.send_notification.assert_called_once()
    assert "abc1234" in fake.platform.send_notification.call_args[0][0]
