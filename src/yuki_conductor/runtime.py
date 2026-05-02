"""Process-level orchestrator for yuki-conductor.

Reads the enabled chat apps from `CHAT_APPS`, starts each receiver
non-blocking, then starts the web server and cron scheduler. Blocks
forever.
"""

import logging
import threading

from yuki_conductor.config import ChatApp, chat_apps
from yuki_conductor.messaging import ChatAppReceiver, MessagingPlatform

logger = logging.getLogger(__name__)


def _build_receiver(app: ChatApp) -> ChatAppReceiver:
    if app is ChatApp.SLACK_SOCKET:
        from yuki_conductor.slack_app import SlackSocketReceiver

        return SlackSocketReceiver()
    if app is ChatApp.TEAMS_CLI:
        from yuki_conductor.messaging.teams_cli_platform import TeamsCliReceiver

        return TeamsCliReceiver()
    raise RuntimeError(f"Unknown chat app: {app!r}")


def start() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    apps = chat_apps()
    logger.info(
        "Starting yuki-conductor with CHAT_APPS="
        + ",".join(sorted(a.value for a in apps) or ["(none)"])
    )

    receivers: list[ChatAppReceiver] = []
    platforms_by_name: dict[str, MessagingPlatform] = {}
    for app in apps:
        rec = _build_receiver(app)
        receivers.append(rec)
        platforms_by_name[rec.platform.name] = rec.platform

    from yuki_conductor.cron_scheduler import start_cron_scheduler
    from yuki_conductor.web_server import start_web_server

    start_web_server()
    start_cron_scheduler(platforms_by_name=platforms_by_name)

    for rec in receivers:
        rec.start()

    if not receivers:
        logger.info("No chat apps enabled (CHAT_APPS empty); web + cron only")

    threading.Event().wait()
