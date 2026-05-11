"""Process-level orchestrator for yuki-conductor.

Reads the enabled chat apps from `CHAT_APPS`, starts each receiver
non-blocking, then starts the web server and cron scheduler. Blocks
forever.
"""

import importlib.metadata
import logging
import threading

from yuki_conductor.config import chat_apps
from yuki_conductor.messaging import ChatAppReceiver, MessagingPlatform
from yuki_conductor.store import ModelStore, SessionStore

logger = logging.getLogger(__name__)


def _build_receiver(
    name: str, session_store: SessionStore, model_store: ModelStore
) -> ChatAppReceiver:
    if name == "slack_socket":
        from yuki_conductor.slack_app import SlackSocketReceiver

        return SlackSocketReceiver()

    # Discover from installed entry-point plugins
    eps = importlib.metadata.entry_points(group="yuki_conductor.chat_plugins")
    for ep in eps:
        if ep.name == name:
            factory = ep.load()
            return factory(session_store=session_store, model_store=model_store)

    available = ["slack_socket"] + [ep.name for ep in eps]
    raise RuntimeError(
        f"Unknown chat app {name!r}; available: {', '.join(available)}"
    )


def start() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    apps = chat_apps()
    logger.info(
        "Starting yuki-conductor with CHAT_APPS="
        + ",".join(apps or ["(none)"])
    )

    session_store = SessionStore()
    model_store = ModelStore()

    receivers: list[ChatAppReceiver] = []
    platforms_by_name: dict[str, MessagingPlatform] = {}
    for app in apps:
        rec = _build_receiver(app, session_store, model_store)
        receivers.append(rec)
        platforms_by_name[rec.platform.name] = rec.platform

    from yuki_conductor.cron_scheduler import start_cron_scheduler
    from yuki_conductor.web_server import start_web_server

    start_web_server()
    start_cron_scheduler(platforms_by_name=platforms_by_name)

    for rec in receivers:
        rec.start()

    for rec in receivers:
        try:
            rec.on_startup_complete()
        except Exception:
            logger.warning(
                f"on_startup_complete failed for {rec.name}", exc_info=True
            )

    if not receivers:
        logger.info("No chat apps enabled (CHAT_APPS empty); web + cron only")

    threading.Event().wait()
