"""Tests for the process-level runtime orchestrator."""

import threading
from unittest.mock import patch

from yuki_conductor.config import ChatApp


class FakeReceiver:
    def __init__(self, name: str) -> None:
        self.name = name
        self.platform = type("P", (), {"name": name})()
        self.started = False

    def start(self) -> None:
        self.started = True


def _patch_block_forever():
    """Replace `threading.Event().wait()` so `runtime.start()` returns."""
    return patch.object(threading.Event, "wait", lambda self, timeout=None: None)


def test_no_apps_starts_only_web_and_cron():
    with (
        _patch_block_forever(),
        patch("yuki_conductor.runtime.chat_apps", return_value=set()),
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
        patch("yuki_conductor.runtime.chat_apps", return_value={ChatApp.SLACK_SOCKET}),
        patch("yuki_conductor.runtime._build_receiver", return_value=fake) as build,
        patch("yuki_conductor.web_server.start_web_server"),
        patch("yuki_conductor.cron_scheduler.start_cron_scheduler") as cron_mock,
    ):
        from yuki_conductor.runtime import start

        start()

    build.assert_called_once_with(ChatApp.SLACK_SOCKET)
    assert fake.started is True
    assert cron_mock.call_args.kwargs["platforms_by_name"] == {"slack": fake.platform}


def test_multiple_apps_start_all_receivers():
    receivers = {
        ChatApp.SLACK_SOCKET: FakeReceiver("slack"),
        ChatApp.TEAMS_CLI: FakeReceiver("teams_cli"),
    }

    def _build(app):
        return receivers[app]

    with (
        _patch_block_forever(),
        patch(
            "yuki_conductor.runtime.chat_apps",
            return_value={ChatApp.SLACK_SOCKET, ChatApp.TEAMS_CLI},
        ),
        patch("yuki_conductor.runtime._build_receiver", side_effect=_build),
        patch("yuki_conductor.web_server.start_web_server"),
        patch("yuki_conductor.cron_scheduler.start_cron_scheduler") as cron_mock,
    ):
        from yuki_conductor.runtime import start

        start()

    assert all(r.started for r in receivers.values())
    platforms = cron_mock.call_args.kwargs["platforms_by_name"]
    assert set(platforms.keys()) == {"slack", "teams_cli"}
