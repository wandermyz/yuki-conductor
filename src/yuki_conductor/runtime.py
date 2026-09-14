"""Process-level orchestrator for yuki-conductor.

Resolves enabled channels from the plugin registry (or the `CHANNELS`
override), starts each receiver non-blocking, then starts the web server and
cron scheduler. Blocks forever.
"""

import logging
import subprocess
import sys
import threading
import time

from yuki_conductor.config import CLAUDE_WORKING_DIR, channels, channels_override
from yuki_conductor.messaging import ChatAppReceiver, MessagingPlatform
from yuki_conductor.plugins import discover_plugins, find_channel, load_factory
from yuki_conductor.store import ModelStore, SessionStore

logger = logging.getLogger(__name__)


def _get_commit_short() -> str:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=CLAUDE_WORKING_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout.strip()
            or "unknown"
        )
    except Exception:
        return "unknown"


def _broadcast_restart_notification(
    platforms: dict[str, "MessagingPlatform"],
) -> None:
    """Send a daemon-restarted notification to every registered platform."""
    commit = _get_commit_short()
    text = f"yuki-conductor daemon restarted (commit `{commit}`)."
    for name, platform in platforms.items():
        try:
            platform.send_notification(text)
            logger.info("Restart notification sent to %s", name)
        except Exception:
            logger.warning("Failed to send restart notification to %s", name, exc_info=True)


def _build_receiver(
    name: str,
    session_store: SessionStore,
    model_store: ModelStore,
    descriptors=None,
) -> ChatAppReceiver:
    """Construct the receiver for a channel name.

    Slack is described in the registry like any other plugin, so there is one
    construction path: resolve the channel's ``module:attr`` factory and call
    it. The builtin factory is a receiver class taking no stores.
    """
    if descriptors is None:
        descriptors = discover_plugins()

    found = find_channel(name, descriptors)
    if found is None:
        available = sorted(
            c.name for d in descriptors for c in d.channels
        )
        raise RuntimeError(
            f"Unknown channel {name!r}; available: {', '.join(available) or '(none)'}"
        )

    desc, channel = found
    factory = load_factory(channel.factory)
    if desc.builtin:
        return factory()
    return factory(session_store=session_store, model_store=model_store)


def _open_log_handler(log_file, attempts: int = 20, delay: float = 0.5):
    """Open the rotating stdout log, retrying while a previous daemon holds it.

    On Windows a still-running old daemon keeps an exclusive handle on the log
    file; opening it raises PermissionError. Retry briefly, then give up on the
    file handler rather than killing the whole process — stdout logging and the
    restart notification still work without it.
    """
    for attempt in range(attempts):
        try:
            return logging.FileHandler(str(log_file), encoding="utf-8")
        except PermissionError:
            if attempt == attempts - 1:
                print(
                    f"WARNING: cannot open {log_file} (locked by another process); "
                    "continuing with stdout logging only",
                    file=sys.stderr,
                    flush=True,
                )
                return None
            time.sleep(delay)


def start() -> None:
    from yuki_conductor.config import LOG_FILE

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # stdout
    root.addHandler(logging.StreamHandler())
    root.handlers[-1].setFormatter(logging.Formatter(fmt))
    # file
    fh = _open_log_handler(LOG_FILE)
    if fh is not None:
        fh.setFormatter(logging.Formatter(fmt))
        root.addHandler(fh)

    from yuki_conductor.plugin_config import ensure_file

    ensure_file()

    descriptors = discover_plugins()
    apps = channels()
    source = "CHANNELS override" if channels_override() is not None else "plugin registry"
    logger.info(
        "Channels enabled (%s): %s", source, ", ".join(apps) or "(none)"
    )
    for desc in descriptors:
        if desc.status != "ok":
            logger.warning(
                "Plugin %r unavailable (%s): %s", desc.name, desc.status, desc.error
            )
        elif not desc.enabled:
            logger.info("Plugin %r installed but disabled", desc.name)

    session_store = SessionStore()
    model_store = ModelStore()

    receivers: list[ChatAppReceiver] = []
    platforms_by_name: dict[str, MessagingPlatform] = {}
    for app in apps:
        try:
            rec = _build_receiver(app, session_store, model_store, descriptors)
        except Exception:
            # A broken plugin must not take the daemon down with it; the web UI
            # surfaces the failure via /api/plugins.
            logger.error("Channel %r failed to start", app, exc_info=True)
            continue
        receivers.append(rec)
        platforms_by_name[rec.platform.name] = rec.platform

    from yuki_conductor.cron_scheduler import start_cron_scheduler
    from yuki_conductor.web_server import mount_static, set_receivers, start_web_server

    set_receivers(receivers)
    web_app = start_web_server()
    start_cron_scheduler(platforms_by_name=platforms_by_name)

    for rec in receivers:
        if hasattr(rec, "register_api_routes"):
            rec.register_api_routes(web_app)

    mount_static(web_app)

    for rec in receivers:
        rec.start()

    for rec in receivers:
        try:
            rec.on_startup_complete()
        except Exception:
            logger.warning(
                f"on_startup_complete failed for {rec.name}", exc_info=True
            )

    _broadcast_restart_notification(platforms_by_name)

    if not receivers:
        logger.info("No channels enabled; web + cron only")

    threading.Event().wait()
