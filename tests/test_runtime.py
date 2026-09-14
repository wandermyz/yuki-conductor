"""Tests for the process-level runtime orchestrator."""

import threading
from unittest.mock import MagicMock, patch

import pytest


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
        patch("yuki_conductor.runtime.channels", return_value=[]),
        patch("yuki_conductor.plugin_config.ensure_file"),
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
        patch("yuki_conductor.runtime.channels", return_value=["slack_socket"]),
        patch("yuki_conductor.plugin_config.ensure_file"),
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
    """A plugin's channel factory is resolved through the registry descriptor."""
    fake = FakeReceiver("my_plugin")

    from yuki_conductor.plugins import Channel, PluginDescriptor

    desc = PluginDescriptor(
        name="my_plugin",
        source="entry_point",
        enabled=True,
        channels=[Channel(name="my_plugin", factory="some_pkg:create_receiver")],
    )

    with patch(
        "yuki_conductor.runtime.load_factory", return_value=lambda **kw: fake
    ) as load:
        from yuki_conductor.runtime import _build_receiver
        from yuki_conductor.store import ModelStore, SessionStore

        result = _build_receiver("my_plugin", SessionStore(), ModelStore(), [desc])

    assert result is fake
    load.assert_called_once_with("some_pkg:create_receiver")


def test_builtin_factory_called_without_stores():
    """The builtin Slack receiver class takes no stores, unlike plugin factories."""
    fake = FakeReceiver("slack")
    from yuki_conductor.plugins import Channel, PluginDescriptor

    desc = PluginDescriptor(
        name="slack",
        source="builtin",
        builtin=True,
        enabled=True,
        channels=[Channel(name="slack_socket", factory="mod:Cls")],
    )
    factory = MagicMock(return_value=fake)
    with patch("yuki_conductor.runtime.load_factory", return_value=factory):
        from yuki_conductor.runtime import _build_receiver
        from yuki_conductor.store import ModelStore, SessionStore

        assert _build_receiver("slack_socket", SessionStore(), ModelStore(), [desc]) is fake
    factory.assert_called_once_with()


def test_unknown_channel_lists_available():
    from yuki_conductor.plugins import Channel, PluginDescriptor
    from yuki_conductor.runtime import _build_receiver
    from yuki_conductor.store import ModelStore, SessionStore

    desc = PluginDescriptor(name="p", channels=[Channel(name="known", factory="m:f")])
    with pytest.raises(RuntimeError, match="known"):
        _build_receiver("missing", SessionStore(), ModelStore(), [desc])


def test_broken_channel_does_not_kill_startup():
    """One plugin blowing up must leave web + cron (and other channels) running."""
    good = FakeReceiver("good")

    def build(name, *a, **kw):
        if name == "bad":
            raise RuntimeError("boom")
        return good

    with (
        _patch_block_forever(),
        patch("yuki_conductor.runtime.channels", return_value=["bad", "good"]),
        patch("yuki_conductor.runtime.discover_plugins", return_value=[]),
        patch("yuki_conductor.runtime._build_receiver", side_effect=build),
        patch("yuki_conductor.plugin_config.ensure_file"),
        patch("yuki_conductor.web_server.start_web_server"),
        patch("yuki_conductor.cron_scheduler.start_cron_scheduler") as cron_mock,
    ):
        from yuki_conductor.runtime import start

        start()

    assert good.started is True
    assert cron_mock.call_args.kwargs["platforms_by_name"] == {"good": good.platform}


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
    fake = FakeReceiver("example_plugin")
    with (
        _patch_block_forever(),
        patch("yuki_conductor.runtime.channels", return_value=["example_plugin"]),
        patch("yuki_conductor.plugin_config.ensure_file"),
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
